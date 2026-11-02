#!/usr/bin/env python3
"""Real FFmpeg checks: every edit encodes afresh; cancelled results stay private."""
from dataclasses import asdict, replace
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import Settings
from vmd_av_preview import Clip
from vmd_comparison import ComparisonFrames, WipeState, png_rgb


class RecordingClip(Clip):
    def __init__(self, *args, **kwargs):
        self.commands = []
        super().__init__(*args, **kwargs)

    def run(self, command, **kwargs):
        self.commands.append(tuple(command))
        return super().run(command, **kwargs)


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    # Even explicitly requesting an old cache location must have no effect.
    os.environ['VMD_PREVIEW_CACHE_DIR'] = str(root / 'forbidden-cache')
    ffmpeg, ffprobe, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    captions = root / 'captions.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:03,000\nFirst caption\n\n'
                        '2\n00:00:03,000 --> 00:00:04,000\nSecond caption\n')
    source = root / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=4',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=4', '-i', str(captions),
        '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:s', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-c:a', 'ac3', '-ac', '6', '-b:a', '640k', '-c:s', 'srt', str(source)], check=True)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    info = core.probe_media(ffprobe, source, False)
    metadata = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)]))
    settings = Settings(encoder='x264', filter_backend='cpu', scaler='lanczos', hw_decode=False,
                        quality='high', system_load='balanced', encode_speed='fast', audio_bitrate=192)
    clips, comparisons, results = [], [], []

    def build(label, selected=settings, audio=(0, 1), subs=(0,), previous=None):
        clip = RecordingClip(source, asdict(selected), None, audio, subs, 192, 108, 'iterm2',
                             source_info=info, source_metadata=metadata, previous=previous)
        clips.append(clip)
        assert not clip.frames and not clip.thumbnails and not clip.audio_pcm
        assert all(not lane.waveform and not lane.cues for lane in clip.audio_lanes + clip.subtitle_lanes)
        assert len(clip.audio_lanes) == 2 and len(clip.subtitle_lanes) == 1
        assert [lane.ordinal for lane in clip.audio_lanes if lane.enabled] == list(audio)
        clip.thread.join(timeout=90)
        assert clip.ready(), clip.error or clip.state
        assert clip.work == dict(video_encoded=True, audio_encoded=list(audio))
        assert not clip.playing and clip.clock is None
        assert any('-c:v' in command and 'libx264' in command for command in clip.commands)
        assert sum('-c:a' in command and 'aac' in command for command in clip.commands) == len(audio)
        assert not any('previous.mkv' in str(part) for command in clip.commands for part in command)
        assert all(str(older.path) not in command for older in clips[:-1] for command in clip.commands)
        probe = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(clip.path)]))
        assert sum(s['codec_type'] == 'audio' for s in probe['streams']) == len(audio)
        assert sum(s['codec_type'] == 'subtitle' for s in probe['streams']) == len(subs)
        raw = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(clip.path), '-map', '0:v:0',
            '-vf', f'fps={clip.frame_fps},scale=192:108:flags=lanczos', '-t', str(clip.duration),
            '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        assert png_rgb(clip.frames[0], 192, 108) == raw
        digest = hashlib.sha256(b''.join(clip.frames)).hexdigest()
        results.append(dict(case=label, generation=clip.video_key, frame_sha256=digest, **clip.work))
        return clip, digest

    try:
        first, reference = build('initial')
        first.held = 1.25
        comparison = ComparisonFrames(first)
        comparisons.append(comparison)
        comparison.thread.join(timeout=40)
        assert comparison.ready(), comparison.error
        wipe = WipeState(fraction=.37)
        filtered, changed = build('denoise strong', replace(settings, denoise='strong'), previous=first)
        assert changed != reference, 'Filter was not applied to fresh frames'
        assert abs(filtered.position() - 1.25) < .001
        restored, digest = build('return to original settings', previous=filtered)
        assert digest == reference and restored.video_key not in (first.video_key, filtered.video_key)
        no_subs, digest = build('remove subtitles', subs=(), previous=restored)
        assert digest == reference
        one_audio, digest = build('remove one audio track', audio=(1,), subs=(), previous=no_subs)
        assert digest == reference
        silent, digest = build('remove all audio', audio=(), subs=(), previous=one_audio)
        assert digest == reference
        reduced, _ = build('change audio bitrate', replace(settings, audio_bitrate=128), previous=silent)
        rebuilt = ComparisonFrames(reduced)
        comparisons.append(rebuilt)
        rebuilt.thread.join(timeout=40)
        assert rebuilt.ready(), rebuilt.error
        assert rebuilt.key != comparison.key and rebuilt.original.filename != comparison.original.filename
        assert rebuilt.original.frame(0) == comparison.original.frame(0)
        assert wipe.fraction == .37
        # Changes made while an older generation is still pending retain time,
        # but none of that generation's media or asynchronous output.
        gate = threading.Event()
        class Delayed(RecordingClip):
            def _prepare(self):
                gate.wait(10)
                super()._prepare()
        pending = Delayed(source, asdict(replace(settings, sharpen='strong')), None, (0,), (), 192, 108,
                          'iterm2', source_info=info, source_metadata=metadata, previous=reduced)
        clips.append(pending)
        newest, digest = build('edit again while old job is pending', previous=pending)
        assert abs(newest.position() - 1.25) < .001 and digest == reference
        before = newest.frames
        pending.close(wait=False)
        gate.set()
        pending.thread.join(timeout=15)
        assert not pending.ready() and not pending.frames and not pending.commands
        assert newest.frames is before and newest.ready()
        # Also cancel an FFmpeg process already encoding in real time.
        class SlowClip(RecordingClip):
            def run(self, command, **kwargs):
                if '-c:v' in command and 'libx264' in command:
                    command = list(command)
                    command[command.index('-i'):command.index('-i')] = ['-re']
                return super().run(command, **kwargs)
        obsolete = SlowClip(source, asdict(replace(settings, denoise='strong')), None, (0,), (), 192, 108,
                            'iterm2', source_info=info, source_metadata=metadata, previous=newest)
        clips.append(obsolete)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            process = obsolete.process
            if process and '-re' in process.args and process.poll() is None:
                break
            time.sleep(.02)
        else:
            raise AssertionError(obsolete.error or 'Live encoder never started')
        obsolete.close(wait=False)
        final, digest = build('edit cancels an active encoder', previous=obsolete)
        obsolete.thread.join(timeout=15)
        assert not obsolete.thread.is_alive() and not obsolete.ready() and not obsolete.frames
        assert process.poll() is not None and final.ready() and digest == reference
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
        assert not (root / 'forbidden-cache').exists()
        (root / 'validation.json').write_text(json.dumps(dict(cases=results, source_unchanged=True,
            audio_and_subtitle_controls_during_build=True, position_preserved=True,
            every_video_and_selected_audio_encoded_again=True, cancelled_output_discarded=True,
            physical_terminal_tested=False), indent=2) + '\n')
        print(root / 'validation.json')
    finally:
        for comparison in comparisons:
            comparison.close()
        for clip in clips:
            clip.close()


if __name__ == '__main__':
    main()
