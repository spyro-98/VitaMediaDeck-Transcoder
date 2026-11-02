"""Short converted AV clips driven by FFplay's actual audio master clock."""
from __future__ import annotations

from dataclasses import dataclass, replace
from array import array
from pathlib import Path
import json
import io
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
import uuid
import zlib

import vitamediadeck_transcoder as core
import vmd_live_preview as live
import vmd_preview as graphics
import vmd_editor_ui as editor_ui
import vmd_estimates as estimates
from vmd_preview_state import fingerprint
from vmd_conversion_timeline import MovieTimeline
import vmd_filter_catalog
import vmd_comparison


def png(rgb, width, height, compression=6):
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data) & 0xffffffff)
    scanlines = b''.join(b'\0' + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(scanlines, compression)) + chunk(b'IEND', b'')


def text(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def srt_cues(value):
    pattern = r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)\s*\n(.*?)(?=\n\s*\n|\Z)'
    cues = []
    for match in re.finditer(pattern, value.replace('\r', ''), re.S):
        a = match.groups()
        start = int(a[0]) * 3600 + int(a[1]) * 60 + int(a[2]) + int(a[3]) / 1000
        end = int(a[4]) * 3600 + int(a[5]) * 60 + int(a[6]) + int(a[7]) / 1000
        caption = re.sub(r'<[^>]*>|\{[^}]*\}', '', a[8]).replace('\n', ' ')
        cues.append((start, end, text(caption)))
    return cues


def cue_at(cues, position):
    return ' / '.join(caption for start, end, caption in cues if start <= position < end)


def audio_waveform(pcm, columns=48):
    """RMS envelope in dBFS, without downsampling or phase-cancelling channels."""
    samples = array('h')
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    import sys
    if sys.byteorder != 'little':
        samples.byteswap()
    bars = []
    for cell in range(columns):
        segment = samples[cell * len(samples) // columns:(cell + 1) * len(samples) // columns]
        power = sum(value * value for value in segment) / max(1, len(segment))
        if power == 0:
            bars.append('·')
        else:
            db = 10 * math.log10(power / (32768.0 ** 2))
            level = max(0, min(7, round((db + 60) / 60 * 7)))
            bars.append('▁▂▃▄▅▆▇█'[level])
    return ''.join(bars)


def sample_statistics(packets, streams, duration, file_bytes):
    """Packet payload rates exclude muxing overhead; file bytes include it."""
    totals = {}
    for packet in packets:
        try:
            index, size = int(packet['stream_index']), int(packet['size'])
        except (KeyError, TypeError, ValueError):
            continue
        if size > 0:
            totals[index] = totals.get(index, 0) + size
    video = next((stream for stream in streams if stream.get('codec_type') == 'video'), None)
    if not video or duration <= 0 or not totals.get(video['index']):
        return None
    return dict(duration=duration, file_bytes=file_bytes, video_bps=totals[video['index']] * 8 / duration,
        audio_bps=[totals.get(stream['index'], 0) * 8 / duration for stream in streams if stream.get('codec_type') == 'audio'])


class AudioClock:
    """Read FFplay -stats (M-A), rather than guessing when its device starts."""
    def __init__(self, path, stream, duration, offset=0):
        player = shutil.which('ffplay')
        if not player:
            raise ValueError('Synchronized AV preview requires ffplay on PATH.')
        self.lock = threading.Lock()
        self.value = None
        self.observed = 0
        self.closing = threading.Event()
        self.duration = duration
        self.offset = offset
        self.error = ''
        # The input is an accurately trimmed PCM excerpt. FFplay's input -ss
        # can seek back to a Matroska keyframe and replay audio from zero.
        self.process = subprocess.Popen([player, '-nodisp', '-autoexit', '-vn', '-sn', '-stats',
            '-loglevel', 'info', '-ast', f'a:{stream}', str(path)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={**os.environ, 'AV_LOG_FORCE_NOCOLOR': '1'})
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        buffer = b''
        tail = bytearray()
        while True:
            data = self.process.stderr.read(1)
            if not data:
                break
            buffer += data
            if data in (b'\r', b'\n'):
                line = buffer.decode(errors='replace')
                match = re.search(r'\s*(-?\d+\.\d+)\s+M-A:', line)
                if match:
                    with self.lock:
                        self.value = self.offset + max(0, float(match[1]))
                        self.observed = time.monotonic()
                else:
                    tail.extend(buffer)
                    del tail[:-3000]
                buffer = b''
        code = self.process.wait()
        if code and not self.closing.is_set():
            self.error = text(tail.decode(errors='replace'))[-300:] or 'Audio playback failed.'

    def position(self):
        with self.lock:
            if self.value is None:
                return None
            delta = min(.12, time.monotonic() - self.observed)
            return min(self.duration, self.value + delta)

    def close(self):
        self.closing.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.thread.join(timeout=1)
        self.process.stderr.close()


@dataclass
class Lane:
    ordinal: int
    label: str
    enabled: bool
    cues: list
    waveform: str = ''


def track_lanes(metadata, audio, subtitles, info=None):
    result = []
    for kind, prefix, selected, total in [('audio', 'A', audio, getattr(info, 'audio_stream_count', 0)),
                                        ('subtitle', 'S', subtitles, len(getattr(info, 'subtitle_codecs', ())))]:
        streams = [stream for stream in metadata.get('streams', []) if stream.get('codec_type') == kind]
        lanes = []
        for ordinal in range(max(total, len(streams))):
            stream = streams[ordinal] if ordinal < len(streams) else {}
            tags = stream.get('tags', {})
            label = text(f"{prefix}{ordinal + 1} {editor_ui.display_language(tags.get('language', 'und'))} {tags.get('title') or str(stream.get('codec_name', '')).upper()}")
            lanes.append(Lane(ordinal, label, ordinal in selected, []))
        result.append(lanes)
    return result


class Clip:
    def __init__(self, source, settings, index, audio, subtitles, width, height, backend,
                 thumbnail_count=6, thumbnail_width=None, thumbnail_height=None,
                 source_info=None, source_metadata=None, previous=None, defer_build=False):
        self.source, self.settings = source, settings.copy()
        self.audio = sorted(audio)
        self.subtitles = sorted(subtitles)
        self.width, self.height, self.backend = width, height, backend
        self.thumbnail_count = thumbnail_count
        self.thumbnail_width = thumbnail_width or (192 if backend in ('kitty', 'iterm2') else 12)
        self.thumbnail_height = thumbnail_height or (108 if backend in ('kitty', 'iterm2') else 6)
        self.display_fps = 3 if backend == 'sixel' else 10
        self.index = index
        self.source_signature = fingerprint(source)
        self.info, self.metadata = source_info, source_metadata
        self.work = dict(video_encoded=False, audio_encoded=[])
        self.state = 'Preparing converted AV clip...'
        self.error = None
        self.frames, self.thumbnails = [], []
        self.video_key = uuid.uuid4().hex
        self.audio_lanes, self.subtitle_lanes = track_lanes(source_metadata or {}, self.audio, self.subtitles, source_info)
        self.duration, self.start, self.movie_duration = 0, 0, 0
        self.encoder = ''
        self.stats = None
        self.clock = None
        self.playing = False
        self.paused = False
        self.silent_started = 0
        self.held = 0
        self.resume_source_time = None
        self.frame_fps = self.display_fps
        self.audio_choice = 0
        self.audio_pcm = []
        self.source_rates = {}
        if previous and previous.source_signature == self.source_signature and previous.index == index:
            self.held = previous.position()
            self.resume_source_time = (previous.resume_source_time if not previous.ready() and
                                       previous.resume_source_time is not None else previous.start + self.held)
            if previous.audio:
                listening = previous.audio[previous.audio_choice]
                self.audio_choice = self.audio.index(listening) if listening in self.audio else 0
        self.process = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.directory = tempfile.TemporaryDirectory(prefix='vmd-av-clip-')
        self.path = Path(self.directory.name) / 'preview.mkv'
        self.deferred = defer_build
        if defer_build:
            self.state = 'Conversion running · preview generation deferred'
        self.thread = threading.Thread(target=(lambda: None) if defer_build else self._prepare, daemon=True)
        self.thread.start()

    def _rates(self, info):
        rates = []
        for ordinal in self.audio:
            source_bps = info.audio_bit_rates[ordinal] or self.source_rates.get(ordinal)
            if source_bps is None:
                self.state = f'Measuring source audio A{ordinal + 1}...'
                result = self.run([self.ffprobe, '-v', 'error', '-select_streams', f'a:{ordinal}',
                    '-show_entries', 'packet=size', '-of', 'csv=p=0', str(self.source)])
                total = 0
                for line in result.stdout.decode(errors='replace').splitlines():
                    try:
                        total += int(line.split(',', 1)[0])
                    except ValueError:
                        continue
                duration = info.audio_durations[ordinal] or info.duration
                source_bps = int(total * 8 / duration) if total and duration and result.returncode == 0 else None
            if not source_bps:
                raise ValueError(f'Cannot determine the bitrate of audio A{ordinal + 1}.')
            self.source_rates[ordinal] = source_bps
            rates.append(core.audio_bitrate_target(self.settings['audio_bitrate'], source_bps))
        return tuple(rates)

    def _build_components(self, info, fps, rates):
        s, directory = self.settings, Path(self.directory.name)
        caps = core.discover_capabilities(self.ffmpeg)
        self.capabilities = caps
        opts = core.video_options(s)
        if s['encoder'] == 'auto' or 'auto' in (opts['filter_backend'], opts['scaler']):
            import vmd_processing
            self.state = 'Calibrating CPU/GPU processing · video progress stays at zero'
            self.processing_plan, self.processing_report = vmd_processing.select(self.ffmpeg, self.source, info, caps,
                s['encoder'], s['hw_decode'] and not info.hdr, fps, s['quality'], s['content_tune'], s['tone_map'],
                Path(s['vaapi_device']), s['system_load'], opts, cancel_event=self.closed)
            opts = self.processing_plan.filter_options
        self.processing_options = opts
        video_path = directory / 'video.mkv'
        self.state = 'Converting video preview...'
        self.encoder = ''
        errors = []
        planned = getattr(self, 'processing_plan', None)
        encoders = [planned.key] if planned else core.encoder_candidates(s['encoder'], caps, Path(s['vaapi_device']))
        for encoder in encoders:
            modes = [True, False] if s['hw_decode'] and not info.hdr and encoder != 'vaapi' else [False]
            if planned:
                modes = [planned.hw_decode]
            for hardware in modes:
                command = core.build_command(self.ffmpeg, self.source, str(video_path), replace(info, duration=self.duration),
                    caps, encoder, hardware, 960, 544, fps, s['quality'], s['content_tune'], s['audio_bitrate'],
                    s['tone_map'], Path(s['vaapi_device']), False, audio_tracks=[], subtitle_tracks=[],
                    system_load=s['system_load'], preserve_extras=False, video_options=opts)
                at = command.index('-i')
                command[at:at] = ['-ss', f'{self.start:.9f}']
                command[-1:-1] = ['-t', f'{self.duration:.9f}', '-map_chapters', '-1', '-avoid_negative_ts', 'disabled', '-loglevel', 'error']
                result = self.run(command)
                if result.returncode == 0 and not core.filter_output_failed(result.stderr):
                    self.encoder = encoder
                    break
                errors.append(result.stderr.decode(errors='replace')[-300:])
            if self.encoder:
                break
        if not self.encoder:
            raise ValueError('Video preview encoding failed: ' + ' / '.join(errors))
        core.validate_output(self.ffprobe, video_path, 0, 0, fps, False, self.duration, expected_attachment_count=0)
        self.work['video_encoded'] = True
        command = [self.ffmpeg, '-hide_banner', '-y', '-loglevel', 'error', '-i', str(video_path)]
        for ordinal, rate in zip(self.audio, rates):
            audio_path = directory / f'audio-{ordinal}.mka'
            self.state = f'Converting audio A{ordinal + 1}...'
            threads = core.resource_thread_limit(s['system_load'])
            options = ['-threads', str(threads), '-filter_threads', str(max(1, min(4, threads // 2)))] if threads else []
            result = self.run([self.ffmpeg, '-hide_banner', '-y', '-loglevel', 'error', *options,
                '-ss', f'{self.start:.9f}', '-i', str(self.source), '-map', f'0:{info.audio_stream_indices[ordinal]}',
                '-vn', '-sn', '-dn', '-c:a', core.VITA_AUDIO_CODEC, '-profile:a', 'aac_low',
                '-ac', str(core.VITA_AUDIO_MAX_CHANNELS), '-ar', str(core.VITA_AUDIO_SAMPLE_RATE),
                '-b:a', f'{rate}k', '-af', core.audio_filter(self.duration), '-t', f'{self.duration:.9f}',
                '-map_metadata', '-1', '-avoid_negative_ts', 'disabled', str(audio_path)])
            if result.returncode:
                raise ValueError('Audio preview failed: ' + text(result.stderr.decode(errors='replace'))[-300:])
            self.work['audio_encoded'].append(ordinal)
            command += ['-i', str(audio_path)]
        original_input = 1 + len(self.audio)
        command += ['-ss', f'{self.start:.9f}', '-i', str(self.source), '-map', '0:v:0']
        for at in range(len(self.audio)):
            command += ['-map', f'{at + 1}:a:0']
        for ordinal in self.subtitles:
            command += ['-map', f'{original_input}:s:{ordinal}']
        command += ['-map', f'{original_input}:t?', '-c', 'copy']
        for at, ordinal in enumerate(self.subtitles):
            if info.subtitle_codecs[ordinal] in ('mov_text', 'text', 'webvtt'):
                command += [f'-c:s:{at}', 'srt']
        if self.subtitles:
            limit = f'{self.duration:.9f}/TB'
            clock = (r'setts=pts=max(PTS\,0):dts=max(DTS\,0):duration='
                     r'max(0\,min(DURATION+min(PTS\,0)\,' + limit + r'-max(PTS\,0)))')
            command += ['-bsf:s', clock]
        command += ['-map_metadata', str(original_input), '-map_chapters', '-1', '-t', f'{self.duration:.9f}',
                    '-avoid_negative_ts', 'disabled', '-dn', str(self.path)]
        self.state = 'Assembling freshly converted video and audio...'
        result = self.run(command)
        if result.returncode:
            raise ValueError('Preview track assembly failed: ' + text(result.stderr.decode(errors='replace'))[-300:])

    def _decode_video(self):
        self.state = 'Decoding converted video frames...'
        result = self.run([self.ffmpeg, '-v', 'error', '-i', str(self.path), '-map', '0:v:0',
            '-vf', f'fps={self.display_fps},scale={self.width}:{self.height}:flags=lanczos', '-t', str(self.duration),
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        size = self.width * self.height * 3
        if result.returncode or len(result.stdout) < size:
            raise ValueError('Cannot decode the converted preview clip.')
        frames = []
        for offset in range(0, len(result.stdout) - size + 1, size):
            if self.closed.is_set():
                raise ValueError('Preview cancelled.')
            rgb = result.stdout[offset:offset + size]
            frames.append(png(rgb, self.width, self.height) if self.backend in ('kitty', 'iterm2')
                          else getattr(graphics, self.backend)(rgb, self.width, self.height))
        tw, th = self.thumbnail_width, self.thumbnail_height
        result = self.run([self.ffmpeg, '-v', 'error', '-i', str(self.path), '-map', '0:v:0',
            '-vf', f'fps={self.thumbnail_count / self.duration:.9f}:start_time=0,scale={tw}:{th}:flags=lanczos',
            '-frames:v', str(self.thumbnail_count), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        thumb_size = tw * th * 3
        if result.returncode or len(result.stdout) < thumb_size:
            raise ValueError('Cannot decode timeline thumbnails.')
        thumbnails = []
        for offset in range(0, len(result.stdout) - thumb_size + 1, thumb_size):
            if self.closed.is_set():
                raise ValueError('Preview cancelled.')
            rgb = result.stdout[offset:offset + thumb_size]
            thumbnails.append(png(rgb, tw, th) if self.backend in ('kitty', 'iterm2') else getattr(graphics, self.backend)(rgb, tw, th))
        thumbnails.extend([thumbnails[-1]] * max(0, self.thumbnail_count - len(thumbnails)))
        self.frames, self.thumbnails = tuple(frames), tuple(thumbnails)
        self.frame_fps = self.display_fps

    def run(self, command, timeout=90):
        with self.lock:
            if self.closed.is_set():
                raise ValueError('Preview cancelled.')
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.process = process
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ValueError('Preview generation timed out.')
        if self.closed.is_set():
            raise ValueError('Preview cancelled.')
        return subprocess.CompletedProcess(command, process.returncode, out, err)

    def _prepare(self):
        # Coalesce rapid parameter changes before starting external encoders.
        if self.closed.wait(.25):
            self.directory.cleanup()
            return
        try:
            s = self.settings
            ffmpeg, ffprobe, _ = core.resolve_media_tools(s['ffmpeg'], s['ffprobe'])
            self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
            info = core.probe_media(ffprobe, self.source, s['force_hdr'])
            self.info = info
            result = self.run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(self.source)])
            self.metadata = json.loads(result.stdout)
            self.audio_lanes, self.subtitle_lanes = track_lanes(self.metadata, self.audio, self.subtitles, info)
            if not info.duration:
                raise ValueError('AV preview requires a known source duration.')
            if self.audio and not shutil.which('ffplay'):
                raise ValueError('Install ffplay to preview synchronized audio and video.')
            self.movie_duration = info.duration
            fps = core.target_fps(info.fps, core.fraction_from_text(s['max_fps']) or core.MAX_FPS)
            self.output_fps = float(fps)
            self.display_fps = min(3 if self.backend == 'sixel' else 30, float(fps))
            self.start = live.sample_time(info.duration, float(fps), self.index)
            self.anchor = self.start
            # A final sample must still be long enough to start the audio
            # device. Show the ending window instead of a 150 ms fragment.
            if self.index is not None and info.duration - self.start < .5:
                self.start = max(0, info.duration - min(6.0, info.duration))
            self.duration = min(6.0, info.duration - self.start)
            if self.resume_source_time is not None:
                self.held = max(0, min(self.duration, self.resume_source_time - self.start))
            rates = self._rates(info)
            self.audio_rates = rates
            self._build_components(info, fps, rates)
            validated = core.validate_output(ffprobe, self.path, len(self.audio), len(self.subtitles), fps,
                                             False, self.duration, expected_attachment_count=info.attachment_count)
            video = next(item for item in validated['streams'] if item.get('codec_type') == 'video')
            tolerance = max(.1, 2 / float(fps))
            if abs(float(video.get('start_time') or 0)) > tolerance:
                raise ValueError('Preview video timestamps do not start at zero.')
            if abs((core.stream_duration(video) or self.duration) - self.duration) > tolerance:
                raise ValueError('Preview video extends beyond the requested clip window.')
            self.state = 'Measuring converted preview bitrates...'
            packets = self.run([ffprobe, '-v', 'error', '-show_packets', '-show_entries',
                                'packet=stream_index,size', '-of', 'json', str(self.path)])
            if packets.returncode == 0:
                try:
                    self.stats = sample_statistics(json.loads(packets.stdout).get('packets', []),
                        validated['streams'], self.duration, self.path.stat().st_size)
                except (ValueError, TypeError):
                    pass  # Missing optional measurements must not prevent playback.
            self._decode_video()
            # Decode the converted AAC once, rather than seeking compressed
            # audio on every resume. PCM slices retain sample-accurate timing.
            for at in range(len(self.audio)):
                decoded = self.run([ffmpeg, '-v', 'error', '-i', str(self.path), '-map', f'0:a:{at}',
                    '-af', 'aresample=async=1:first_pts=0,apad', '-t', str(self.duration),
                    '-ac', '2', '-ar', '48000', '-f', 's16le', '-'])
                if decoded.returncode or not decoded.stdout:
                    raise ValueError('Cannot prepare synchronized preview audio.')
                self.audio_pcm.append(decoded.stdout)
            self.state = 'Reading audio and subtitle lanes...'
            metadata = self.metadata
            source_origin = float(metadata.get('format', {}).get('start_time') or 0)
            audios = [v for v in metadata['streams'] if v.get('codec_type') == 'audio']
            subs = [v for v in metadata['streams'] if v.get('codec_type') == 'subtitle']
            for ordinal, stream in enumerate(audios):
                tags = stream.get('tags', {})
                label = text(f"A{ordinal + 1} {tags.get('language', 'und')} {tags.get('title', stream.get('codec_name', ''))}")
                lane = self.audio_lanes[ordinal]
                if lane.enabled:
                    pcm = self.audio_pcm[self.audio.index(ordinal)]
                else:
                    raw = self.run([ffmpeg, '-v', 'error', '-ss', str(self.start), '-i', str(self.source),
                        '-map', f'0:a:{ordinal}', '-t', str(self.duration), '-ar', '48000', '-f', 's16le', '-'])
                    pcm = raw.stdout if raw.returncode == 0 else b''
                lane.waveform = audio_waveform(pcm) if pcm else ''
            for ordinal, stream in enumerate(subs):
                tags = stream.get('tags', {})
                label = text(f"S{ordinal + 1} {tags.get('language', 'und')} {tags.get('title', stream.get('codec_name', ''))}")
                lane = self.subtitle_lanes[ordinal]
                lane.cues = []
                source = self.path if lane.enabled else self.source
                at = self.subtitles.index(ordinal) if lane.enabled else ordinal
                seek = [] if lane.enabled else ['-copyts', '-ss', str(max(0, self.start - 30))]
                limit = ['-t', str(self.duration)] if lane.enabled else ['-to', str(source_origin + self.start + self.duration)]
                result = self.run([ffmpeg, '-v', 'error', *seek, '-i', str(source), '-map', f'0:s:{at}',
                    *limit, '-avoid_negative_ts', 'disabled', '-c:s', 'srt', '-f', 'srt', '-'])
                if result.returncode == 0:
                    origin = 0 if lane.enabled else source_origin + self.start
                    lane.cues = [(max(0, begin - origin), min(self.duration, end - origin), caption)
                                 for begin, end, caption in srt_cues(result.stdout.decode(errors='replace'))
                                 if end > origin and begin < origin + self.duration]
                else:
                    # Bitmap captions cannot be converted to text without OCR.
                    packets = self.run([ffprobe, '-v', 'error', '-select_streams', f's:{ordinal}',
                        '-read_intervals', f'{self.start}%+{self.duration}', '-show_packets',
                        '-show_entries', 'packet=pts_time,duration_time', '-of', 'json', str(self.source)])
                    if packets.returncode == 0:
                        for packet in json.loads(packets.stdout).get('packets', []):
                            try:
                                begin = float(packet['pts_time']) - self.start
                            except (KeyError, TypeError, ValueError):
                                continue
                            end = begin + (core.duration_seconds(packet.get('duration_time')) or .1)
                            lane.cues.append((begin, end, f"[{stream.get('codec_name', 'bitmap')} graphic subtitle event]"))
            self.state = 'Ready · freshly converted AV clip · Space plays'
        except Exception as exc:
            if not self.closed.is_set():
                self.error = str(exc)
                self.state = 'Preview error'
        finally:
            if self.closed.is_set():
                self.directory.cleanup()

    def ready(self):
        return not self.closed.is_set() and not self.thread.is_alive() and not self.error and bool(self.frames)

    def _start_audio(self):
        playback = Path(self.directory.name) / 'playback.wav'
        first_sample = round(self.held * 48000)
        with wave.open(str(playback), 'wb') as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(self.audio_pcm[self.audio_choice][first_sample * 4:])
        self.clock = AudioClock(playback, 0, self.duration, offset=first_sample / 48000)

    def play(self):
        if not self.ready():
            return
        self.stop()
        if self.held >= self.duration - .05:
            self.held = 0
        if self.audio:
            self._start_audio()
        self.silent_started = time.monotonic() - self.held
        self.playing, self.paused = True, False
        self.state = 'Playing converted AV clip'

    def position(self):
        if not self.playing or self.paused:
            return self.held
        if self.clock:
            position = self.clock.position()
            if self.clock.error:
                self.state = 'Audio error: ' + self.clock.error
                self.playing = False
                return self.held
            if position is None:
                self.state = 'Waiting for audio device clock...'
                if self.clock.process.poll() is not None:
                    self.state = 'Audio device stopped before reporting its clock.'
                    self.playing = False
                return self.held
            self.held = max(self.held, position)
            self.state = 'Playing converted AV clip'
            if self.clock.process.poll() is not None:
                self.playing = False
                self.state = 'Clip ended · Space replays'
        else:
            self.held = min(self.duration, time.monotonic() - self.silent_started)
            if self.held >= self.duration:
                self.playing = False
                self.state = 'Clip ended · Space replays (all audio excluded)'
        return self.held

    def toggle(self):
        if not self.playing:
            self.play()
            return
        self.held = self.position()
        if not self.paused:
            if self.clock:
                self.clock.close()
                self.clock = None
        elif self.audio:
            self._start_audio()
        else:
            self.silent_started = time.monotonic() - self.held
        self.paused = not self.paused
        self.state = 'Paused' if self.paused else 'Playing converted AV clip'

    def stop(self):
        if self.clock:
            self.clock.close()
            self.clock = None
        self.playing = self.paused = False
        if self.ready():
            self.state = 'Stopped · Space plays converted clip'

    def close(self, wait=True):
        self.closed.set()
        self.stop()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
        if wait:
            self.thread.join(timeout=3)
        if not self.thread.is_alive() and hasattr(self, 'directory'):
            self.directory.cleanup()


def selection(app, settings, info):
    if app.input_path and app.input_path.is_dir():
        audio = getattr(settings, 'audio_tracks', None)
        subtitles = getattr(settings, 'subtitle_tracks', None)
        return (list(range(info.audio_stream_count)) if audio is None else [n for n in audio if n < info.audio_stream_count],
                list(range(len(info.subtitle_codecs))) if subtitles is None else [n for n in subtitles if n < len(info.subtitle_codecs)])
    return sorted(app.selected_audio_tracks), sorted(app.selected_subtitle_tracks)


def paint(app, payload, row, column, columns, rows, image_id=31415, stream=None, pixels=None, natural=False):
    if app.preview_backend == 'kitty':
        data = graphics.kitty(payload, columns, rows, image_id=image_id, natural=natural)
    elif app.preview_backend == 'iterm2':
        data = graphics.iterm2(payload, columns, rows, pixels=pixels)
    else:
        data = payload
    import sys
    output = stream if stream is not None else sys.stdout
    output.write('\x1b7')
    if app.preview_backend == 'unicode':
        for offset, line in enumerate(data.split('\r\n')):
            output.write(f'\x1b[{row + offset + 1};{column + 1}H' + line)
    else:
        output.write(f'\x1b[{row + 1};{column + 1}H' + data)
    output.write('\x1b8')
    if stream is None:
        output.flush()


def clear_images(app):
    import sys
    if app.preview_backend == 'kitty':
        for image in (31415, *range(31500, 31500 + getattr(app, '_av_thumb_count', 6))):
            sys.stdout.write(f'\x1b_Ga=d,d=I,i={image},q=2\x1b\\')
        sys.stdout.flush()


def run_view(app, primary=False):
    """AV inspector/editor: source is read-only; only conversion options change."""
    import curses
    import sys
    from dataclasses import asdict
    clip = None
    identity = None
    painted = None
    movie, movie_key, film_painted = None, None, None
    progress_session, progress_dismissed = None, False
    filter_support, filter_query = False, ''
    focus, lane_cursor, offset = 'overview', 0, 0
    vita_size = False
    geometry = None
    message = ''
    layer = editor_ui.TextLayer(app.color(9))
    wipe = getattr(app, '_av_wipe', None)
    if wipe is None:
        wipe = app._av_wipe = vmd_comparison.WipeState()
    wipe.dragging = False
    comparison, comparison_key = None, None
    was_converting = False
    mouse = vmd_comparison.MouseInput(curses, sys.stdout)
    if app.audio_preview:
        app.audio_preview.stop()
    app.screen.timeout(16)
    try:
        while app.running:
            app.drain_events()
            live_state = getattr(app, 'live', None)
            active_conversion = bool(app.worker and app.worker.is_alive())
            if active_conversion and not was_converting:
                if clip:
                    clip.stop()
                    if not clip.ready():
                        clip.close(wait=False)
                    clip.state = 'Frozen during conversion'
                if app.audio_preview:
                    app.audio_preview.stop()
            if not active_conversion and was_converting and clip and (getattr(clip, 'deferred', False) or
                    getattr(clip, 'closed', threading.Event()).is_set()):
                identity = None
            was_converting = active_conversion
            if active_conversion and comparison and not comparison.ready():
                comparison.close(wait=False)
                comparison, comparison_key = None, None
            if live_state and live_state.started_at != progress_session:
                progress_session, progress_dismissed = live_state.started_at, False
            full_timeline = bool(live_state and live_state.started_at and
                                 (active_conversion or not progress_dismissed and live_state.phase in ('complete', 'error', 'cancelled')))
            source, settings = app.preview_subject()
            height, width = app.screen.getmaxyx()
            if layer.resize(width, height):
                painted = None
                film_painted = None
                clear_images(app)
            if source is None or not source.is_file():
                if comparison:
                    comparison.close(wait=False)
                    comparison, comparison_key = None, None
                if movie:
                    movie.close()
                    movie, movie_key, film_painted = None, None, None
                if clip:
                    clip.close(wait=False)
                    clip, identity, painted = None, None, None
                clear_images(app)
                app.screen.erase()
                app.add(0, 2, 'VitaMediaDeck · AV editor', app.color(4), width - 4)
                app.add(2, 2, 'I Select a video or folder · Q Exit', app.color(4), width - 4)
                app.add(5, 2, 'Video preview', app.color(8), width - 4)
                app.add(max(7, height - 10), 2, 'Timeline · select a source to discover frames and tracks', app.color(8), width - 4)
                app.add(max(8, height - 8), 2, 'Audio tracks                         Subtitles', app.color(8), width - 4)
                app.add(height - 2, 2, app.status, app.color(8), width - 4)
                app.screen.refresh()
                layer.invalidate()
                key = app.screen.getch()
                key, _ = mouse.decode(app.screen, key)
                if key == getattr(curses, 'KEY_MOUSE', -999):
                    continue
                if key in (ord('i'), ord('I')):
                    mouse.call(app.choose_input)
                    focus = 'overview'
                elif key in (ord('q'), ord('Q')):
                    mouse.call(app.handle_key, key)
                elif key == 27 and not primary:
                    break
                continue
            if height < 22 or width < 76:
                clear_images(app)
                app.screen.erase()
                app.add(1, 2, 'Resize to at least 76 columns x 22 rows. Q exits; the preview returns on resize.', width=width - 4)
                app.screen.refresh()
                layer.invalidate()
                key = app.screen.getch()
                key, _ = mouse.decode(app.screen, key)
                if key == getattr(curses, 'KEY_MOUSE', -999):
                    continue
                if key in (ord('q'), ord('Q')):
                    mouse.call(app.handle_key, key)
                    return
                if key == 27 and not primary:
                    return
                continue
            # Read small metadata once per source/settings-relevant fingerprint.
            source_stat = source.stat()
            probe_key = (source, source_stat.st_size, source_stat.st_mtime_ns, settings.ffmpeg, settings.ffprobe, settings.force_hdr)
            if not hasattr(app, '_av_probe') or app._av_probe[0] != probe_key:
                try:
                    _, probe, _ = core.resolve_media_tools(settings.ffmpeg, settings.ffprobe)
                    app._av_probe = (probe_key, core.probe_media(probe, source, settings.force_hdr))
                except (OSError, ValueError, core.TranscodeError) as exc:
                    from types import SimpleNamespace
                    app._av_probe = (probe_key, SimpleNamespace(audio_stream_count=0, subtitle_codecs=(), duration=None))
                    message = str(exc)
            info = app._av_probe[1]
            raw_key = probe_key[:5]
            if not hasattr(app, '_av_original') or app._av_original[0] != raw_key:
                try:
                    _, probe, _ = core.resolve_media_tools(settings.ffmpeg, settings.ffprobe)
                    raw = json.loads(core.run_capture([probe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)], 'source overview'))
                    app._av_original = (raw_key, raw)
                except (OSError, ValueError, core.TranscodeError):
                    app._av_original = (raw_key, {})
            original = app._av_original[1]
            audio, subtitles = selection(app, settings, info)
            app._av_source_values = editor_ui.source_values(info, original, audio)
            if not hasattr(app, '_av_baselines'):
                app._av_baselines = {}
            app._av_baselines.setdefault(source, asdict(settings))
            count = max(info.audio_stream_count, len(info.subtitle_codecs), 1)
            workspace = editor_ui.workspace_geometry(width, height, count)
            visible, thumb_rows = workspace['visible'], workspace['thumb_rows']
            panel_top, content_top = workspace['panel_top'], workspace['content_top']
            video_rows = workspace['panel_rows']
            # Left quarter: wrapped parameters. Right: aspect-fitted video.
            sidebar_width = (width - 4) // 4
            video_left = sidebar_width + 5
            video_width = width - video_left - 3
            cell_width, cell_height, measured = graphics.cell_size()
            columns, rows, pixels, natural = editor_ui.preview_geometry(video_width, video_rows, cell_width, cell_height, vita_size)
            image_top = content_top + (video_rows - rows) // 2
            image_left = video_left + (video_width - columns) // 2
            timeline_top, lanes_top = workspace['timeline_top'], workspace['lanes_top']
            slots = editor_ui.timeline_geometry(width - 4, thumb_rows, cell_width, cell_height)
            native = app.preview_backend in ('kitty', 'iterm2')
            pw = 960 if native else columns if app.preview_backend == 'unicode' else min(960, pixels[0])
            ph = 544 if native else rows * 2 if app.preview_backend == 'unicode' else max(2, round(pw * 544 / 960))
            tw = max(192, min(320, round(slots[0][1] * cell_width))) if native else slots[0][1] if app.preview_backend == 'unicode' else max(12, int(min(slots[0][1] * cell_width, thumb_rows * cell_height * 960 / 544)))
            th = max(2, round(tw * 544 / 960)) if app.preview_backend != 'unicode' else thumb_rows * 2
            layout = (width, height, image_top, image_left, columns, rows, vita_size, pixels)
            if geometry != layout:
                clear_images(app)
                geometry, painted = layout, None
                layer.invalidate()
                film_painted = None
            stat = source.stat()
            if full_timeline:
                timeline_source, timeline_info = source, info
                if app.input_path.is_dir() and live_state.batch_source:
                    timeline_source = app.input_path / live_state.batch_source
                    if timeline_source != source:
                        timeline_info = app.batch_metadata.get(timeline_source, {}).get('_media_info')
                timeline_stat = timeline_source.stat() if timeline_source.is_file() else stat
                wanted_movie = (timeline_source, timeline_stat.st_size, timeline_stat.st_mtime_ns,
                                len(slots), tw, th, app.preview_backend, active_conversion)
                if wanted_movie != movie_key:
                    if movie:
                        movie.close()
                    movie = MovieTimeline(timeline_source, asdict(settings), timeline_info, len(slots), tw, th,
                        app.preview_backend, png, defer=True, placeholders_only=active_conversion,
                        duration=live_state.media_duration)
                    movie_key, film_painted = wanted_movie, None
                if active_conversion or clip and clip.ready() and not clip.playing:
                    movie.activate()
            elif movie:
                movie.close()
                movie, movie_key, film_painted = None, None, None
            requested = (source, stat.st_mtime_ns, json.dumps(asdict(settings), sort_keys=True),
                         app.preview_samples.get(source), tuple(audio), tuple(subtitles), pw, ph, len(slots), tw, th)
            if identity != requested and (not active_conversion or clip is None):
                previous = clip
                if app.audio_preview and app.audio_preview.active:
                    app.audio_preview.stop()
                app._av_thumb_count = len(slots)
                clip = Clip(source, asdict(settings), app.preview_samples.get(source), audio, subtitles, pw, ph, app.preview_backend,
                            thumbnail_count=len(slots), thumbnail_width=tw, thumbnail_height=th,
                            source_info=info, source_metadata=original, previous=previous, defer_build=active_conversion)
                if previous:
                    previous.close(wait=False)
                if comparison:
                    comparison.close(wait=False)
                comparison, comparison_key = None, None
                clear_images(app)
                layer.invalidate()
                film_painted = None
                identity, painted = requested, None
            app._av_estimate_context = dict(info=info, metadata=original, audio=audio, source_size=stat.st_size,
                                           source_audio_rates=getattr(clip, 'source_rates', None),
                                           processing=getattr(clip, 'processing_options', None),
                                           processing_report=getattr(clip, 'processing_report', None),
                                           capabilities=getattr(clip, 'capabilities', None))
            if focus == 'audio':
                app._av_audio_cursor = lane_cursor
            forecast_key = (requested, stat.st_size, clip.encoder, getattr(clip, 'audio_rates', None),
                            id(getattr(clip, 'stats', None)), id(getattr(clip, 'processing_report', None)),
                            id(getattr(clip, 'processing_options', None)), clip.ready(), bool(clip.error), getattr(app, '_av_audio_cursor', None))
            if getattr(app, '_av_forecast_key', None) != forecast_key:
                app._av_plan = estimates.output_plan(settings, **app._av_estimate_context,
                    audio_rates=getattr(clip, 'audio_rates', None), sample=getattr(clip, 'stats', None), encoder=clip.encoder,
                    sample_pending=not clip.ready() and not clip.error)
                app._av_effects = estimates.parameter_effects(settings, info, app._av_plan, getattr(app, '_av_audio_cursor', None))
                app._av_forecast_key = forecast_key
            position = clip.position()
            current = min(max(0, int(position * getattr(clip, 'frame_fps', clip.display_fps))), max(0, len(clip.frames) - 1))
            wanted_comparison = vmd_comparison.clip_key(clip)
            if wipe.enabled and wanted_comparison and comparison_key != wanted_comparison and not active_conversion:
                comparison_key = wanted_comparison
                previous_comparison = comparison
                try:
                    comparison = vmd_comparison.ComparisonFrames(clip)
                except (OSError, ValueError) as exc:
                    comparison = None
                    app.logs.append('Comparison preview: ' + str(exc))
                if previous_comparison:
                    previous_comparison.close(wait=False)
            comparison_visible = bool(wipe.enabled and comparison and comparison.ready() and
                                      comparison.key == wanted_comparison)
            show_frame = bool(clip.ready() and clip.frames)
            layer.begin()
            focus_title = next((name for key, name in editor_ui.SECTIONS if key == focus), focus)
            focus_label = f' Focus: {focus_title} '
            layer.add(0, 2, f'VitaMediaDeck / Transcoder · {core.APP_VERSION}', app.color(9) | curses.A_BOLD,
                      max(1, width - len(focus_label) - 6))
            layer.add(0, max(30, width - len(focus_label) - 2), focus_label, app.color(2) | curses.A_BOLD, len(focus_label))
            source_title = (f'Converting: {live_state.batch_source} | Frozen preview: {source.name}'
                            if active_conversion and live_state and live_state.batch_source else source.name)
            layer.add(1, 2, source_title, app.color(3) | curses.A_BOLD, width - 4)
            layer.add(3, 2, '─' * (width - 4), app.color(8), width - 4)
            editor_ui.navigation(app, layer, focus, 4, width, app.input_path.is_dir())
            editor_ui.preview_controls(app, layer, 5, 2, width - 4, frozen=active_conversion)
            left_title = 'Filter support' if filter_support and focus == 'video' else focus_title
            editor_ui.frame(app, layer, panel_top, 1, video_rows + 2, sidebar_width + 2, left_title,
                            True)
            mode = 'Vita 960×544' if vita_size else 'Fit window'
            if vita_size and not natural:
                mode += ' · reduced to fit'
            if vita_size and (not measured or app.preview_backend == 'unicode'):
                mode += ' · approximate'
            if comparison_visible:
                mode += ' · Original | Processed'
            elif wipe.enabled:
                mode += (' · Recalculating all preview media' if not clip.ready() else
                         ' · Processed; preparing original' if not comparison or not comparison.error else
                         ' · Comparison unavailable (9 Log)')
            else:
                mode += ' · Compare OFF'
            if active_conversion:
                mode = 'Frozen preview sample · playback after conversion'
            editor_ui.frame(app, layer, panel_top, sidebar_width + 4, video_rows + 2, width - sidebar_width - 6,
                            f'Preview · {mode}')
            if clip.error:
                if getattr(app, '_av_logged_preview_error', None) != clip.error:
                    app.logs.append('Preview error: ' + clip.error)
                    app._av_logged_preview_error = clip.error
            label, attribute, choices = app.SETTING_ROWS[app.setting_index]
            sidebar_lines = []
            if focus == 'overview':
                editor_ui.paged_lines(app, layer, editor_ui.overview_lines(source, info, original), focus,
                                      content_top, 2, video_rows, sidebar_width - 1)
            elif focus == 'review':
                lines = editor_ui.review_lines(source, settings, info, original, audio, subtitles, app._av_baselines[source], getattr(clip, 'audio_rates', None), app._av_plan)
                if app.input_path.is_dir():
                    kept = sum(app.batch_enabled.get(path, True) for path in app.batch_sources)
                    lines = [('Batch scope', 'heading'), (f'T converts {kept}/{len(app.batch_sources)} queued files.', 'value'),
                             ('Size and track comparisons below refer to the selected media:', 'muted'),
                             (str(source.relative_to(app.input_path)), 'value')] + lines
                lines += [('Preview encoder', 'heading'), (clip.encoder or 'Still preparing', 'value')]
                lines += [('Output path', 'heading'), (str(app.output_path), 'muted')]
                editor_ui.paged_lines(app, layer, lines, focus, content_top, 2, video_rows, sidebar_width - 1, stable=True)
            elif focus == 'presets':
                editor_ui.presets_panel(app, layer, settings, content_top, 2, video_rows, sidebar_width - 1)
            elif focus == 'media' and not app.input_path.is_dir():
                sidebar_lines = ['1 file selected', source.name, '', 'I selects a file or folder.', 'Select a folder for a batch queue.']
            elif focus == 'log':
                lines = list(app.logs) or ['No conversion log yet.']
                wrapped_log = [part for line in lines for part in editor_ui.wrapped(line, sidebar_width - 2)]
                app.scroll['Log'] = min(app.scroll['Log'], max(0, len(wrapped_log) - video_rows + 4))
                end = len(wrapped_log) - app.scroll['Log']
                sidebar_lines = ['Conversion log', '↑↓ Scroll · Tab Focus', ''] + wrapped_log[max(0, end - video_rows + 4):end]
            if sidebar_lines:
                wrapped_lines = [part for line in sidebar_lines for part in editor_ui.wrapped(line, sidebar_width - 2)]
                for row, line in enumerate(wrapped_lines[:video_rows]):
                    layer.add(content_top + row, 2, line, app.color(9) if row == 0 else app.color(8), sidebar_width - 1)
            elif focus == 'media' and app.input_path.is_dir():
                editor_ui.queue_panel(app, layer, content_top, 2, video_rows, sidebar_width - 1)
            elif focus in ('audio', 'subtitles'):
                editor_ui.track_panel(app, layer, settings, original, audio, subtitles, focus, lane_cursor,
                                      content_top, 2, video_rows, sidebar_width - 1, app._av_plan, position, clip)
            elif focus == 'video' and filter_support:
                editor_ui.paged_lines(app, layer, vmd_filter_catalog.lines(app._av_plan, settings, filter_query),
                                      'filters', content_top, 2, video_rows, sidebar_width - 1, stable=True)
            elif focus in editor_ui.PARAMETER_SECTIONS:
                editor_ui.sidebar(app, layer, settings, content_top, 2, video_rows, sidebar_width, focus)
            folder_field = app.input_path.is_dir() and (focus in editor_ui.PARAMETER_SECTIONS or focus == 'audio')
            detail = message or ('S Apply bitrate to folder' if folder_field and focus == 'audio' else
                                 'S Apply selected field to folder' if folder_field else
                                 'Space starts playback · changes prepare a paused preview')
            if focus == 'media' and app.input_path.is_dir():
                record = app.batch_metadata.get(source, {})
                detail = str(record.get('error') or record.get('log') or record.get('status') or detail)
            if app.audio_preview and app.audio_preview.active:
                detail = f'Solo audio excerpt (video stopped): {app.audio_preview.status}'
            if focus in ('audio', 'subtitles'):
                detail = 'Enter includes or excludes the selected track. X excludes all; E includes all.'
            status = message or (app.status if app.worker and app.worker.is_alive() else detail)
            if clip.error and not active_conversion:
                summary = 'Preview error (9 Log): ' + clip.error
            elif full_timeline:
                stage = 'Paused' if app.paused else live_state.phase_label
                summary = f'{stage} · {editor_ui.clock_text(live_state.media_seconds)} / {editor_ui.clock_text(live_state.media_duration or movie.duration)} · {live_state.detail}'
                if live_state.batch_source:
                    summary += ' · ' + live_state.batch_source
                if not clip.ready():
                    summary += ' | Preview: ' + clip.state
            else:
                preview_state = clip.state if not clip.ready() else clip.state.split(' · ')[0]
                summary = f'{preview_state} · {clip.start + position:.2f}/{info.duration or 0:.2f}s · {clip.encoder} | {status}'
            summary = editor_ui.stable_rows(summary, width - 4, 1)[0]
            layer.add(2, 2, summary.ljust(width - 4), app.color(5 if clip.error else 4 if not clip.ready() else 8), width - 4)
            timeline_title = (f' Full video · {live_state.local_ratio * 100:.1f}% encoded' +
                              (f' · File {live_state.batch_index}/{live_state.batch_total} · Queue {live_state.overall_ratio * 100:.1f}%' if live_state.batch_total else '') + ' '
                              if full_timeline else f' Timeline · {len(slots)} frames · sample {"20% default" if app.preview_samples.get(source) is None else str(app.preview_samples[source] + 1) + "/100"} ')
            layer.add(timeline_top, 2, timeline_title.ljust(width - 4, '─'), app.color(10), width - 4)
            layer.add(timeline_top, 2, timeline_title, app.color(10) | curses.A_BOLD, width - 4)
            strip_width = width - 4
            if full_timeline:
                editor_ui.progress_line(app, layer, timeline_top + thumb_rows + 2, 2, strip_width, live_state.local_ratio)
            else:
                playhead = min(strip_width - 1, int(position / max(.01, clip.duration) * strip_width))
                layer.add(timeline_top + thumb_rows + 2, 2, '─' * playhead + '▼' + '─' * (strip_width - playhead - 1), app.color(7), strip_width)
            for i, (left, tile_columns) in enumerate(slots):
                tick = editor_ui.clock_text(i * (live_state.media_duration or movie.duration or 0) / len(slots)) if full_timeline else f'{i * clip.duration / len(slots):.1f}s'
                layer.add(timeline_top + thumb_rows + 1, 2 + left, tick, app.color(8), tile_columns)
            if full_timeline:
                end_label = editor_ui.clock_text(live_state.media_duration or movie.duration)
                layer.add(timeline_top + thumb_rows + 1, width - len(end_label) - 2, end_label, app.color(8), len(end_label))
            half = (width - 6) // 2
            for separator_row in range(lanes_top, lanes_top + visible + 1):
                layer.add(separator_row, half + 3, '│', app.color(8), 1)
            for column, title, active in ((2, f'{"▶ " if focus == "audio" else ""}Audio · {len(audio)}/{info.audio_stream_count} included · Level (dBFS) · Enter toggles', focus == 'audio'),
                                          (half + 4, f'{"▶ " if focus == "subtitles" else ""}Subtitles · {len(subtitles)}/{len(info.subtitle_codecs)} included · Enter toggles', focus == 'subtitles')):
                style = app.color(2 if active else 10)
                layer.add(lanes_top, column, title.ljust(half, '─'), style, half)
                layer.add(lanes_top, column, title, style | curses.A_BOLD, half)
            if focus not in editor_ui.PARAMETER_SECTIONS:
                offset = max(0, min(lane_cursor - visible + 1, max(0, count - visible)))
            for row in range(visible):
                ordinal = offset + row
                for column, lanes, kind in ((2, clip.audio_lanes, 'audio'), (half + 4, clip.subtitle_lanes, 'subtitles')):
                    if ordinal >= len(lanes):
                        continue
                    lane = lanes[ordinal]
                    selected = focus == kind and ordinal == lane_cursor
                    style = (app.color(9) if lane.enabled else app.color(8) | curses.A_DIM)
                    if selected:
                        style = app.color(2) | curses.A_BOLD
                    if kind == 'audio':
                        wave_length = max(4, min(48, half - 35))
                        wave = ''.join(lane.waveform[min(len(lane.waveform) - 1, i * len(lane.waveform) // wave_length)] for i in range(wave_length)) if lane.waveform else ''
                        if clip.duration and wave:
                            at = min(len(wave) - 1, int(position / clip.duration * len(wave)))
                            wave = wave[:at] + '│' + wave[at + 1:]
                        listening = '*' if lane.enabled and clip.audio and clip.audio[clip.audio_choice] == ordinal else ' '
                        value = f'{"▶" if selected else listening} [{"Included" if lane.enabled else "Excluded"}] {lane.label[:30]} {wave}'
                    else:
                        value = f'{"▶ " if selected else ""}[{"Included" if lane.enabled else "Excluded"}] {lane.label[:24]} | {cue_at(lane.cues, position)}'
                    layer.add(lanes_top + 1 + row, column, value.ljust(half), style, half)
            controls = 'Tab/Shift-Tab Sections · PgUp/PgDn Page · Space Play/Pause · [ ] Sample · G Jump · Z Size'
            if focus in ('audio', 'subtitles'):
                controls = 'Tab/Shift-Tab Sections · ↑↓ Track · Enter Toggle · X None · E All · Space Play/Pause'
            elif focus in editor_ui.PARAMETER_SECTIONS:
                controls = 'Tab/Shift-Tab Sections · ↑↓ Select · ←→ Change · Enter Edit · PgUp/PgDn Page · Space Play/Pause'
            if width < 110:
                if focus in ('audio', 'subtitles'):
                    controls = 'Tab/Shift-Tab · ↑↓ · Enter Toggle · X None E All · Space Play/Pause'
                elif focus in editor_ui.PARAMETER_SECTIONS:
                    controls = 'Tab/Shift-Tab Menu · ↑↓ Field · ←→ Value · Enter Edit · Space Play/Pause'
                else:
                    controls = 'Tab/Shift-Tab · PgUp/Dn · Space Play/Pause · [ ] Sample · Z Size'
            if filter_support and focus == 'video':
                controls = 'H Back · / Search all filters · ↑↓ or PgUp/PgDn · Space Play/Pause'
            if active_conversion:
                controls = 'Preview frozen · P pauses conversion · A aborts · playback available when conversion finishes'
            actions = ('I Input · O Output · U Update · N Save preset · P Pause conversion · A Abort · T Convert · Q Exit'
                       if width >= 110 else 'I In O Out U Update N Save P EncodePause A Abort T Convert Q Exit')
            if focus != 'review':
                actions = actions.replace('T Convert', 'T Review')
            layer.add(height - 2, 2, controls.ljust(width - 4), app.color(10), width - 4)
            layer.add(height - 1, 2, actions, app.color(8), width - 4)
            buffer = io.StringIO()
            if full_timeline:
                film, film_tokens = movie.render(live_state.local_ratio)
            else:
                film = clip.thumbnails if show_frame else ()
                film_tokens = tuple(id(payload) for payload in film)
            protected = ([(image_top, image_left, columns, rows)] if show_frame else []) + [
                (timeline_top + 1, 2 + left, tile_columns, thumb_rows) for left, tile_columns in slots[:len(film)]]
            if layer.clear_pending:
                film_painted, painted = None, None
            layer.present(buffer, protected)
            for i, payload in enumerate(film):
                if film_painted is None or i >= len(film_painted) or film_tokens[i] != film_painted[i]:
                    left, tile_columns = slots[i]
                    paint(app, payload, timeline_top + 1, 2 + left, tile_columns, thumb_rows, 31500 + i, stream=buffer)
            film_painted = film_tokens
            if show_frame:
                frame_token = (id(clip.frames), current, id(comparison) if comparison_visible else None,
                               round(wipe.fraction * clip.width) if comparison_visible else None)
                if painted != frame_token:
                    payload = clip.frames[current]
                    if comparison_visible:
                        try:
                            payload = comparison.frame(current, wipe.fraction)
                        except (ValueError, OSError, zlib.error) as exc:
                            comparison.error = str(exc)
                            comparison.complete.clear()
                    paint(app, payload, image_top, image_left, columns, rows, stream=buffer,
                          pixels=pixels if native else None, natural=natural and app.preview_backend == 'kitty')
                    painted = frame_token
            if comparison and comparison.error and getattr(app, '_av_comparison_error', None) != comparison.error:
                app.logs.append('Comparison preview: ' + comparison.error)
                app._av_comparison_error = comparison.error
            # Present one complete update; text never touches image rectangles.
            sys.stdout.write('\x1b[?2026h' + buffer.getvalue() + '\x1b[?2026l')
            sys.stdout.flush()
            app.screen.clearok(False)
            app.screen.untouchwin()
            key = app.screen.getch()
            key, mouse_event = mouse.decode(app.screen, key)
            if key == getattr(curses, 'KEY_MOUSE', -999):
                try:
                    _, mouse_x, mouse_y, _, buttons = mouse_event if mouse_event is not None else curses.getmouse()
                    wipe.mouse(mouse_x, mouse_y, buttons, (image_top, image_left, columns, rows), curses)
                except curses.error:
                    pass
                continue
            if wipe.key(key):
                if not wipe.enabled and comparison and not comparison.ready():
                    comparison.close(wait=False)
                    comparison, comparison_key = None, None
                continue
            if key in (ord('q'), ord('Q')) and primary:
                clip.stop()
                clear_images(app)
                app.screen.erase()
                app.screen.refresh()
                mouse.call(app.handle_key, key)
                painted = None
                layer.invalidate()
                if not app.running:
                    break
                continue
            if not primary and key in (27, ord('q'), ord('Q'), ord('v'), ord('V')):
                break
            if key == -1:
                continue
            running = app.worker and app.worker.is_alive()
            if key in (ord('h'), ord('H')):
                filter_support = not (filter_support and focus == 'video')
                focus = editor_ui.activate_section(app, focus, 'video')
                app._av_pages = {**getattr(app, '_av_pages', {}), 'filters': 0}
                continue
            if filter_support and focus == 'video':
                if key == 27:
                    filter_support = False
                    continue
                if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
                    editor_ui.move_page(app, 'filters', -1 if key in (curses.KEY_UP, curses.KEY_PPAGE) else 1)
                    continue
                if key == ord('/'):
                    clip.stop()
                    clear_images(app)
                    value = mouse.call(app.prompt, 'Filter name or family (empty = all)', filter_query)
                    if value is not None:
                        filter_query = value.strip()
                    film_painted, painted = None, None
                    layer.invalidate()
                    app._av_pages = {**getattr(app, '_av_pages', {}), 'filters': 0}
                    continue
                if key in (10, 13, ord('+'), ord('-'), curses.KEY_LEFT, curses.KEY_RIGHT):
                    continue
            if not running and key in (ord(' '), ord('v'), ord('V'), ord('['), ord(']'), ord('<'), ord('>'), ord('g'), ord('G'), ord('+'), ord('='), ord('-'), curses.KEY_LEFT, curses.KEY_RIGHT):
                progress_dismissed = True
            if primary and key == 27:
                focus = editor_ui.activate_section(app, focus, 'video')
                continue
            if ord('1') <= key <= ord('9'):
                filter_support = False
                focus = editor_ui.activate_section(app, focus, editor_ui.SECTIONS[key - ord('1')][0])
                lane_cursor = 0
                message = ''
                app._av_pages = {**getattr(app, '_av_pages', {}), focus: 0}
                continue
            if key in (ord('z'), ord('Z')):
                clip.stop()
                vita_size = not vita_size
                continue
            if key in (ord('i'), ord('I'), ord('o'), ord('O'), ord('u'), ord('U'), ord('n'), ord('N')):
                if running and key not in (ord('n'), ord('N')):
                    message = 'Conversion running: input, output and analysis stay fixed until it finishes.'
                    continue
                if running and key not in (ord('n'), ord('N')):
                    message = 'Conversion running: input/output paths are frozen.'
                    continue
                clip.stop()
                clear_images(app)
                app.screen.erase()
                app.screen.refresh()
                if key in (ord('i'), ord('I')):
                    mouse.call(app.choose_input)
                    focus = 'overview'
                elif key in (ord('o'), ord('O')):
                    mouse.call(app.edit_output)
                elif key in (ord('u'), ord('U')):
                    app.probe_current()
                    if hasattr(app, '_av_probe'):
                        del app._av_probe
                    identity = None
                else:
                    name = mouse.call(app.prompt, 'Preset name', '')
                    if name:
                        app.presets.save(name, settings)
                        message = f'Preset saved: {name}'
                painted = None
                layer.invalidate()
                continue
            if key in (ord('p'), ord('P'), ord('a'), ord('A')):
                if key in (ord('p'), ord('P')):
                    app.toggle_pause()
                else:
                    app.cancel_conversion()
                message = app.status
                continue
            if running and ((focus in editor_ui.PARAMETER_SECTIONS and key in (ord('+'), ord('='), ord('-'), curses.KEY_RIGHT, curses.KEY_LEFT, 10, 13))
                            or (focus in ('audio', 'subtitles', 'presets') and key in (10, 13))
                            or (focus in ('audio', 'subtitles') and key in (ord('x'), ord('X'), ord('e'), ord('E'), ord('+'), ord('='), ord('-'), curses.KEY_DC, getattr(curses, 'KEY_BACKSPACE', 263), 127, 8))):
                message = 'Conversion running: settings and exclusions are frozen until it finishes.'
                continue
            if running and focus == 'media' and key in (10, 13, ord('+'), ord('='), ord('-')):
                message = 'Conversion running: queue is frozen.'
                continue
            if running and key in (ord('l'), ord('L'), ord('j'), ord('J'), ord('['), ord(']'), ord('<'), ord('>'),
                    ord('g'), ord('G'), ord('{'), ord('}'), ord('z'), ord('Z'), curses.KEY_HOME, curses.KEY_END):
                message = 'Preview stays frozen during conversion; playback returns when it finishes.'
                continue
            if focus == 'media' and not app.input_path.is_dir() and key in (10, 13, curses.KEY_UP, curses.KEY_DOWN, ord('+'), ord('='), ord('-'), ord('r'), ord('R')):
                message = 'Single file selected. Use I to choose a folder for a batch queue.'
                continue
            if key in (ord(' '), ord('v'), ord('V')):
                if running:
                    message = 'Preview stays frozen during conversion; playback returns when it finishes.'
                    continue
                if app.audio_preview and app.audio_preview.active:
                    app.audio_preview.stop()
                clip.toggle()
            elif key in (ord('l'), ord('L'), ord('{'), ord('}')):
                if key in (ord('l'), ord('L')):
                    clip.stop()
                    if focus == 'audio' and lane_cursor < info.audio_stream_count:
                        app.audio_track_indices[source] = lane_cursor
                app.handle_audio_key(key)
                context = app.audio_context()
                if context:
                    _, _, ordinal, sample, (start, length), _ = context
                    message = f'Solo audio {sample + 1}/10 · track {ordinal + 1} · {start:.2f}s + {length:.2f}s · L listens (video stopped)'
            elif key in (ord('t'), ord('T')):
                if focus != 'review':
                    focus = editor_ui.activate_section(app, focus, 'review')
                    message = 'Review output and retained tracks · T starts conversion'
                else:
                    clip.stop()
                    mouse.call(app.start_conversion)
                    painted, film_painted = None, None
                    layer.invalidate()
                    if not primary:
                        break
            elif key in (ord('g'), ord('G')):
                clip.stop()
                clear_images(app)
                app.screen.erase()
                app.screen.refresh()
                value = mouse.call(app.prompt, 'Preview sample (1-100)', str((app.preview_samples.get(source) or 0) + 1))
                if value is not None:
                    try:
                        chosen = int(value)
                        if not 1 <= chosen <= 100:
                            raise ValueError('Choose sample 1-100.')
                        app.preview_samples[source] = chosen - 1
                    except ValueError:
                        message = 'Choose a sample number from 1 to 100.'
                painted = None
                layer.invalidate()
            elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE) and focus in editor_ui.PARAMETER_SECTIONS:
                editor_ui.next_page(app, -1 if key == curses.KEY_PPAGE else 1)
            elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE) and focus in ('overview', 'review', 'presets', 'audio', 'subtitles'):
                editor_ui.move_page(app, focus, -1 if key == curses.KEY_PPAGE else 1)
            elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE) and app.input_path.is_dir():
                step = -1 if key == curses.KEY_PPAGE else 1
                app.batch_selection_index = max(0, min(len(app.batch_sources) - 1, app.batch_selection_index + step))
                if app.batch_edit_source is not None:
                    app.batch_edit_source = app.batch_sources[app.batch_selection_index]
            elif key in (9, getattr(curses, 'KEY_BTAB', 353)):
                sections = [key for key, _ in editor_ui.SECTIONS]
                step = 1 if key == 9 else -1
                selected_focus = sections[(sections.index(focus) + step) % len(sections)] if focus in sections else 'overview'
                focus = editor_ui.activate_section(app, focus, selected_focus)
                lane_cursor = 0
            elif key in (ord('j'), ord('J')) and clip.audio:
                was_playing = clip.playing and not clip.paused
                clip.stop()
                if app.audio_preview and app.audio_preview.active:
                    app.audio_preview.stop()
                clip.audio_choice = (clip.audio_choice + 1) % len(clip.audio)
                app.audio_track_indices[source] = clip.audio[clip.audio_choice]
                if was_playing:
                    clip.play()
            elif key in (ord('['), ord(']'), ord('<'), ord('>'), curses.KEY_HOME, curses.KEY_END):
                at = app.preview_samples.get(source)
                at = 20 if at is None else at
                if key in (curses.KEY_HOME, curses.KEY_END):
                    at = 0 if key == curses.KEY_HOME else 99
                else:
                    at += {ord('['): -1, ord(']'): 1, ord('<'): -10, ord('>'): 10}[key]
                app.preview_samples[source] = max(0, min(99, at))
            elif focus == 'presets':
                presets = app.presets.rows()
                if presets and key in (curses.KEY_UP, curses.KEY_DOWN):
                    app.preset_index = (app.preset_index + (-1 if key == curses.KEY_UP else 1)) % len(presets)
                    app._av_pages = {**getattr(app, '_av_pages', {}), 'presets': 0}
                elif presets and key in (10, 13):
                    from dataclasses import asdict
                    chosen = type(settings).from_dict(asdict(presets[app.preset_index][2]))
                    if app.input_path.is_dir():
                        app.batch_settings[source] = chosen
                        app.batch_edit_source = source
                    else:
                        app.settings = chosen
                        if chosen.audio_tracks is not None:
                            app.selected_audio_tracks = set(chosen.audio_tracks).intersection(range(info.audio_stream_count))
                        if chosen.subtitle_tracks is not None:
                            app.selected_subtitle_tracks = set(chosen.subtitle_tracks).intersection(range(len(info.subtitle_codecs)))
                elif presets and key in (ord('d'), ord('D')):
                    clip.stop()
                    clear_images(app)
                    app.screen.erase()
                    app.screen.refresh()
                    mouse.call(app.handle_presets_key, key)
                    painted = None
                    layer.invalidate()
            elif focus == 'media' and key in (curses.KEY_UP, curses.KEY_DOWN):
                app.batch_selection_index = max(0, min(len(app.batch_sources) - 1, app.batch_selection_index + (-1 if key == curses.KEY_UP else 1)))
            elif focus == 'media' and key in (10, 13):
                app.batch_enabled[source] = not app.batch_enabled.get(source, True)
            elif focus == 'media' and key in (ord('r'), ord('R')) and not running:
                app.reset_batch_source_settings(source)
                if app.batch_edit_source == source:
                    app.batch_edit_source = None
                message = 'Selected media now uses global defaults.'
            elif focus == 'media' and key in (ord('+'), ord('='), ord('-')):
                at = app.batch_selection_index
                to = max(0, min(len(app.batch_sources) - 1, at + (-1 if key == ord('-') else 1)))
                app.batch_sources[at], app.batch_sources[to] = app.batch_sources[to], app.batch_sources[at]
                app.batch_selection_index = to
            elif focus == 'log' and key in (curses.KEY_UP, curses.KEY_DOWN):
                app.scroll['Log'] = max(0, app.scroll['Log'] + (1 if key == curses.KEY_UP else -1))
            elif focus in ('overview', 'review') and key in (curses.KEY_UP, curses.KEY_DOWN):
                editor_ui.move_page(app, focus, -1 if key == curses.KEY_UP else 1)
            elif focus == 'audio' and key in (ord('s'), ord('S')) and app.input_path.is_dir():
                if running:
                    message = 'Conversion running: settings are frozen.'
                    continue
                app.batch_edit_source = source
                previous_tab, previous_index = app.tab, app.setting_index
                app.tab, app.setting_index = 2, 4
                app.handle_settings_key(ord('g'))
                app.tab, app.setting_index = previous_tab, previous_index
                message = app.status
            elif focus == 'audio' and key in (ord('+'), ord('='), ord('-')):
                clip.stop()
                if app.input_path.is_dir():
                    app.batch_edit_source = source
                previous_tab, previous_index = app.tab, app.setting_index
                app.tab, app.setting_index = 2, 4
                app.handle_settings_key(curses.KEY_LEFT if key == ord('-') else curses.KEY_RIGHT)
                app.tab, app.setting_index = previous_tab, previous_index
            elif focus in editor_ui.PARAMETER_SECTIONS:
                if key == curses.KEY_UP:
                    editor_ui.next_field(app, -1, focus)
                elif key == curses.KEY_DOWN:
                    editor_ui.next_field(app, 1, focus)
                elif key in (ord('s'), ord('S')) and app.input_path.is_dir():
                    if running:
                        message = 'Conversion running: settings are frozen.'
                        continue
                    app.batch_edit_source = source
                    previous_tab = app.tab
                    app.tab = 2
                    app.handle_settings_key(ord('g'))
                    app.tab = previous_tab
                    message = app.status
                elif key in (ord('+'), ord('='), ord('-'), curses.KEY_RIGHT, curses.KEY_LEFT, 10, 13):
                    clip.stop()
                    if choices is None and key in (10, 13):
                        clear_images(app)
                        app.screen.erase()
                        app.screen.refresh()
                    if app.input_path.is_dir():
                        app.batch_edit_source = source
                        settings = app.active_settings()
                    previous_tab = app.tab
                    app.tab = 2
                    setting_key = curses.KEY_LEFT if key in (ord('-'), curses.KEY_LEFT) else 10 if key in (10, 13) else curses.KEY_RIGHT
                    if key in (10, 13):
                        mouse.call(app.handle_settings_key, setting_key)
                    else:
                        app.handle_settings_key(setting_key)
                    app.tab = previous_tab
                    if choices is None:
                        painted = None
                        layer.invalidate()
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                limit = info.audio_stream_count if focus == 'audio' else len(info.subtitle_codecs)
                lane_cursor = max(0, min(max(0, limit - 1), lane_cursor + (-1 if key == curses.KEY_UP else 1)))
                app._av_pages = {**getattr(app, '_av_pages', {}), focus: 0}
            elif focus in ('audio', 'subtitles') and key in (10, 13, ord('x'), ord('X'), ord('e'), ord('E'), curses.KEY_DC, getattr(curses, 'KEY_BACKSPACE', 263), 127, 8):
                tracks = audio if focus == 'audio' else subtitles
                total = info.audio_stream_count if focus == 'audio' else len(info.subtitle_codecs)
                if lane_cursor < total:
                    updated = set(tracks)
                    if key in (ord('x'), ord('X')):
                        updated.clear()
                    elif key in (ord('e'), ord('E')):
                        updated = set(range(total))
                    elif key in (curses.KEY_DC, getattr(curses, 'KEY_BACKSPACE', 263), 127, 8):
                        updated.discard(lane_cursor)
                    else:
                        updated.symmetric_difference_update({lane_cursor})
                    clip.stop()
                    message = f'{"Audio" if focus == "audio" else "Subtitles"}: {len(updated)} of {total} tracks included in the output.'
                    if app.input_path.is_dir():
                        app.batch_edit_source = source
                        settings = app.active_settings()
                        setattr(settings, 'audio_tracks' if focus == 'audio' else 'subtitle_tracks', sorted(updated))
                    else:
                        target = app.selected_audio_tracks if focus == 'audio' else app.selected_subtitle_tracks
                        target.clear()
                        target.update(updated)
    except (OSError, ValueError, subprocess.SubprocessError, core.TranscodeError) as exc:
        app.status = f'AV preview: {exc}'
    finally:
        wipe.dragging = False
        if comparison:
            comparison.close()
        mouse.close()
        if movie:
            movie.close(wait=True)
        if clip:
            clip.close()
        clear_images(app)
        sys.stdout.write(graphics.clear(app.preview_backend))
        sys.stdout.flush()
        app.inline_rendered = None
        app.screen.clearok(True)
        app.screen.timeout(100)
