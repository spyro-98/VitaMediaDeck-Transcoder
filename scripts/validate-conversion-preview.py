#!/usr/bin/env python3
"""Frozen conversion previews, independent queue progress and audio envelopes."""
from array import array
from dataclasses import asdict
from fractions import Fraction
import curses
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import TerminalApp, Settings
import vmd_av_preview as av
from vmd_conversion_timeline import MovieTimeline
import vmd_workflow as workflow


def envelope_checks():
    def signal(frequency, amplitude=.4, stereo=False):
        values = array('h')
        for sample in range(48000):
            value = round(32767 * amplitude * math.sin(2 * math.pi * frequency * sample / 48000))
            values.extend((value, -value) if stereo else (value,))
        if sys.byteorder != 'little':
            values.byteswap()
        return values.tobytes()
    low = av.audio_waveform(signal(200))
    high = av.audio_waveform(signal(6000))
    phase = av.audio_waveform(signal(6000, stereo=True))
    assert low == high == phase and set(high) >= {'▇'}, (low, high, phase)
    assert av.audio_waveform(bytes(96000)) == '·' * 48
    mixed = bytes(24000) + signal(6000)[:24000] + signal(6000, .001)[:24000] + bytes(24000)
    wave = av.audio_waveform(mixed)
    assert wave[:12] == wave[-12:] == '·' * 12 and wave[12] > wave[24]


def editor_checks(root):
    inputs, outputs = root / 'inputs', root / 'outputs'
    inputs.mkdir()
    sources = [inputs / name for name in ('one.mkv', 'two.mkv')]
    for source in sources:
        source.touch()
    info = SimpleNamespace(audio_stream_count=0, subtitle_codecs=[], duration=40, width=1920, height=1080,
        fps=Fraction(24), video_codec='h264', audio_bit_rates=(), hdr=False)
    metadata = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080}]}
    results = []
    for backend in ('iterm2', 'kitty', 'unicode', 'sixel'):
        for initially_ready in (True, False):
            builds, movies, frames = [], [], []
            class Worker:
                def __init__(self, **kwargs): self.alive = False
                def start(self): self.alive = True
                def is_alive(self): return self.alive
            class Clip:
                def __init__(self, source, settings, index, audio, subtitles, width, height, backend, **kwargs):
                    assert not app.worker or not app.worker.is_alive(), 'Rebuilt preview during conversion'
                    self.source, self.settings = source, settings
                    self.start, self.duration, self.held, self.frame_fps = 2, 6, .5, 24
                    self.display_fps, self.encoder = 24, 'x264'
                    payload = av.png(b'\0\0\0', 1, 1) if backend in ('kitty', 'iterm2') else 'frame'
                    self.frames, self.thumbnails = (payload,), (payload,) * kwargs['thumbnail_count']
                    self.audio, self.audio_choice, self.audio_lanes, self.subtitle_lanes = [], 0, [], []
                    self.error, self.state, self.clock, self.closed = None, 'Ready', None, threading.Event()
                    self.playing, self.paused = True, False
                    builds.append(self)
                def ready(self): return not self.closed.is_set() and (initially_ready or len(builds) > 1)
                def stop(self): self.playing, self.paused = False, False
                def position(self): return self.held
                def toggle(self):
                    assert not app.worker or not app.worker.is_alive(), 'Preview playback during conversion'
                    self.playing = not self.playing
                def close(self, wait=True):
                    self.stop()
                    self.closed.set()
            class Timeline:
                def __init__(self, source, settings, info, count, width, height, backend, encode, **kwargs):
                    self.source, self.placeholders_only = source, kwargs['placeholders_only']
                    self.count = count
                    self.duration = info.duration if info else kwargs.get('duration')
                    movies.append(self)
                def activate(self):
                    if app.worker and app.worker.is_alive():
                        assert self.placeholders_only, 'Source extraction while conversion runs'
                def render(self, ratio): return (av.png(b'\0\0\0', 1, 1),) * self.count, (ratio,) * self.count
                def close(self, wait=False): pass
            class Screen:
                def __init__(self):
                    self.width, self.height = 180, 50
                    self.keys = iter([ord('t'), ord('t'), ord(' '), ord('l'), ord(']'), ord('z'),
                                      'resize', 'next', ord(' '), ord('j'), 'finish', ord(' '), ord('q')])
                def getmaxyx(self): return self.height, self.width
                def timeout(self, value): pass
                def clearok(self, value): pass
                def untouchwin(self): pass
                def erase(self): pass
                def refresh(self): pass
                def getch(self):
                    if app.worker and app.worker.is_alive():
                        assert len(builds) == 1 and not builds[0].playing
                        assert app.preview_subject()[0] == sources[0]
                        assert app.preview_subject()[1].quality == 'high'
                        if initially_ready:
                            assert frames and frames[-1] == builds[0].frames[0]
                        else:
                            assert not frames, 'Unfinished preview was presented as a valid result'
                    key = next(self.keys)
                    if key == 'resize':
                        self.width, self.height = 140, 38
                        return curses.KEY_RESIZE
                    if key == 'next':
                        app.live.batch_source, app.live.batch_index = 'two.mkv', 2
                        app.live.local_ratio, app.live.media_duration = 0, None
                        return -1
                    if key == 'finish':
                        app.worker.alive = False
                        app.live.phase = 'complete'
                        return -1
                    return key
            def paint(app, payload, row, column, columns, rows, image_id=31415, **kwargs):
                if image_id == 31415:
                    frames.append(payload)
            with patch.object(TerminalApp, '_configure_screen'):
                app = TerminalApp(Screen(), inputs, outputs, Settings(quality='high'))
            app.batch_sources, app.batch_metadata = sources, {source: {'duration': 40, '_media_info': info} for source in sources}
            app.batch_metadata[sources[1]] = {'duration': 20, '_media_info': SimpleNamespace(**{**vars(info), 'duration': 20})}
            app.batch_settings[sources[0]] = Settings(quality='compact')
            app.tab = 2  # Global settings, deliberately different from per-media.
            app.color = lambda _: 0
            app.preview_backend = backend
            app.drain_events = lambda: None
            signature = sources[0].stat()
            key = (sources[0], signature.st_size, signature.st_mtime_ns, 'ffmpeg', 'ffprobe', False)
            app._av_probe, app._av_original = (key, info), (key[:5], metadata)
            real_start = app.start_conversion
            def start():
                real_start()
                app.live.batch_source, app.live.batch_total = 'one.mkv', 2
                app.live.phase, app.live.phase_label = 'transcode', 'ENCODE'
            app.start_conversion = start
            app.handle_key = lambda _: setattr(app, 'running', False)
            output = io.StringIO()
            with patch.object(av, 'Clip', Clip), patch.object(av, 'MovieTimeline', Timeline), \
                    patch.object(av, 'paint', paint), patch('vitamediadeck_tui.threading.Thread', Worker), \
                    patch('sys.stdout', output), patch('subprocess.Popen', side_effect=AssertionError('Unexpected media process')):
                av.run_view(app, primary=True)
            assert 'Space/V Locked' in output.getvalue() and 'Frozen preview' in output.getvalue()
            assert any(movie.source == sources[1] and movie.placeholders_only and movie.duration == 20 for movie in movies)
            assert len(builds) >= 2  # Normal regeneration is available again afterwards.
            results.append(dict(backend=backend, initially_ready=initially_ready,
                                retained='same frame; no rebuild/playback during encode or episode change'))
    return results


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    envelope_checks()
    editors = editor_checks(root)
    ffmpeg, ffprobe, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    source = root / 'tones.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=1.5',
        '-f', 'lavfi', '-i', 'sine=frequency=200:sample_rate=48000:duration=1.5',
        '-f', 'lavfi', '-i', 'sine=frequency=6000:sample_rate=48000:duration=1.5',
        '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-c:a', 'pcm_s16le', str(source)], check=True)
    settings = Settings(encoder='x264', filter_backend='cpu', scaler='lanczos', hw_decode=False)
    clip = av.Clip(source, asdict(settings), None, [0], [], 96, 54, 'iterm2')
    clip.thread.join(timeout=40)
    try:
        assert clip.ready(), clip.error
        assert all(lane.waveform and max(lane.waveform) >= '▅' for lane in clip.audio_lanes)
        assert clip.audio_lanes[1].enabled is False
    finally:
        clip.close()
    info = core.probe_media(ffprobe, source, False)
    with patch('subprocess.Popen', side_effect=AssertionError('Deferred preview launched FFmpeg')):
        deferred = av.Clip(source, asdict(settings), None, [], [], 96, 54, 'iterm2', defer_build=True)
        deferred.thread.join(timeout=5)
        assert deferred.deferred and not deferred.ready() and not deferred.frames
        deferred.close()
    with patch('subprocess.Popen', side_effect=AssertionError('Placeholder timeline launched FFmpeg')):
        timeline = MovieTimeline(source, asdict(settings), info, 6, 96, 54, 'iterm2', av.png, placeholders_only=True)
        timeline.thread.join(timeout=10)
        assert not timeline.error and all(value is None for value in timeline.rgb)
        dark, _ = timeline.render(0)
        full, _ = timeline.render(1)
        assert all(a != b for a, b in zip(dark, full))
        timeline.close(wait=True)
    warm = MovieTimeline(source, asdict(settings), info, 6, 96, 54, 'iterm2', av.png)
    warm.thread.join(timeout=30)
    assert not warm.error and all(warm.rgb)
    warm.close(wait=True)
    workflow.atomic_json(root / 'validation.json', dict(editors=editors,
        waveform='48 kHz; 200/6000 Hz and opposite-phase channels retained; true silence marked separately',
        timeline='no media subprocesses while conversion runs; dark placeholders illuminate with encoded progress',
        physical_terminal='Automated render/control checks; live visual/audio observation remains separate'))
    print(root / 'validation.json')


if __name__ == '__main__':
    main()
