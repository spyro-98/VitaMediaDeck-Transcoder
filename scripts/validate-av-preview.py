#!/usr/bin/env python3
"""Synthetic AV-editor regression, with FFplay's silent SDL audio driver."""
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import os
import subprocess
import sys
import time
import struct
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import Settings
from vmd_av_preview import Clip, cue_at


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ['SDL_AUDIODRIVER'] = 'dummy'
    ffmpeg, _, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    subtitle = root / 'captions.srt'
    # The first cue crosses the seek point. Without preview timestamp clipping
    # Matroska shifts AV by two seconds and validation reports 8.006s, not 6s.
    subtitle.write_text('1\n00:00:00,000 --> 00:00:03,000\nFirst cue\n\n'
                        '2\n00:00:04,000 --> 00:00:05,000\nSecond cue\n\n'
                        '3\n00:00:06,000 --> 00:00:09,000\nEnd cue\n', encoding='utf-8')
    source = root / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=10',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=10',
        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:duration=10',
        '-i', str(subtitle), '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:s',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-c:s', 'srt', str(source)], check=True)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    results = []
    for audio, subs in [([0, 1], [0]), ([1], []), ([], [])]:
        clip = Clip(source, asdict(Settings(encoder='x264', hw_decode=False)), None, audio, subs, 48, 26, 'unicode')
        try:
            clip.thread.join(timeout=60)
            assert clip.ready(), clip.error or clip.state
            assert clip.stats and clip.stats['video_bps'] > 0
            assert len(clip.stats['audio_bps']) == len(audio)
            assert clip.stats['file_bytes'] == clip.path.stat().st_size
            assert clip.stats['duration'] == clip.duration
            # Payload bitrate excludes muxing overhead and retained captions.
            assert (clip.stats['video_bps'] + sum(clip.stats['audio_bps'])) * clip.duration / 8 <= clip.stats['file_bytes']
            assert abs(clip.start - 2) < .001 and abs(clip.duration - 6) < .001
            info = core.probe_media(clip.ffprobe, clip.path, False)
            assert info.audio_stream_count == len(audio) and len(info.subtitle_codecs) == len(subs)
            assert sum(lane.enabled for lane in clip.audio_lanes) == len(audio)
            assert sum(lane.enabled for lane in clip.subtitle_lanes) == len(subs)
            assert cue_at(clip.subtitle_lanes[0].cues, .5) == 'First cue'
            assert cue_at(clip.subtitle_lanes[0].cues, 1.5) == ''
            assert cue_at(clip.subtitle_lanes[0].cues, 2.5) == 'Second cue'
            assert cue_at(clip.subtitle_lanes[0].cues, 5.5) == 'End cue'
            assert clip.subtitle_lanes[0].cues[-1][1] <= clip.duration
            payload = json.loads(subprocess.check_output([clip.ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(clip.path)]))
            video = next(item for item in payload['streams'] if item.get('codec_type') == 'video')
            assert abs(float(video.get('start_time') or 0)) < .1
            assert abs(core.stream_duration(video) - 6) < .1
            assert float(payload['format']['duration']) <= 6.1
            if audio:
                clip.play()
                deadline = time.monotonic() + 5
                while clip.position() < .4 and time.monotonic() < deadline:
                    time.sleep(.05)
                assert clip.position() >= .4, clip.state
                old_clock = clip.clock
                old_pid = old_clock.process.pid
                clip.toggle()
                frozen = clip.position()
                assert clip.clock is None and old_clock.process.poll() is not None
                time.sleep(.15)
                assert abs(clip.position() - frozen) < .02
                clip.toggle()
                assert clip.clock.process.pid != old_pid and '-ss' not in clip.clock.process.args
                seek = clip.clock.offset
                assert abs(seek - frozen) < .001
                with wave.open(clip.clock.process.args[-1], 'rb') as resumed:
                    assert resumed.getframerate() == 48000 and resumed.getnchannels() == 2
                    assert resumed.readframes(resumed.getnframes()) == clip.audio_pcm[clip.audio_choice][round(frozen * 48000) * 4:]
                clock_deadline = time.monotonic() + 3
                while clip.clock.value is None and time.monotonic() < clock_deadline:
                    time.sleep(.01)
                assert clip.clock.value is not None and clip.clock.value >= frozen - .001
                deadline = time.monotonic() + 5
                while clip.position() < frozen + .3 and time.monotonic() < deadline:
                    time.sleep(.05)
                assert frozen + .3 <= clip.position() < frozen + 2, clip.state
                for _ in range(2):
                    clock = clip.clock
                    clip.toggle()
                    position = clip.position()
                    assert clock.process.poll() is not None and clip.clock is None
                    time.sleep(.15)
                    assert clip.position() == position
                    clip.toggle()
                    deadline = time.monotonic() + 5
                    while clip.position() < position + .15 and time.monotonic() < deadline:
                        time.sleep(.05)
                    assert clip.position() >= position + .15, clip.state
            results.append({'audio': audio, 'subtitles': subs, 'status': 'passed'})
        finally:
            clip.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    clip = Clip(source, asdict(Settings(encoder='x264', hw_decode=False)), None, [0], [0], 960, 544,
                'iterm2', thumbnail_count=18, thumbnail_width=256, thumbnail_height=144)
    try:
        clip.thread.join(timeout=90)
        assert clip.ready(), clip.error or clip.state
        assert len(clip.thumbnails) == 18
        assert struct.unpack('>II', clip.frames[0][16:24]) == (960, 544)
        assert all(struct.unpack('>II', payload[16:24]) == (256, 144) for payload in clip.thumbnails)
        assert not clip.playing and clip.clock is None
        results.append({'native_video': '960x544', 'timeline': '18 PNGs at 256x144', 'autoplay': False, 'status': 'passed'})
    finally:
        clip.close()
    surround = root / 'source-surround.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0', '-map', '0:a:1',
        '-c:v', 'copy', '-c:a', 'ac3', '-ac', '6', '-b:a', '640k', '-sn', str(surround)], check=True)
    clip = Clip(surround, asdict(Settings(encoder='x264', hw_decode=False, audio_bitrate=192)), None, [0, 1], [], 48, 26, 'unicode')
    try:
        clip.thread.join(timeout=60)
        assert clip.ready(), clip.error or clip.state
        assert clip.audio_rates == (192, 192)
        before = json.loads(subprocess.check_output([clip.ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(surround)]))
        after = json.loads(subprocess.check_output([clip.ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(clip.path)]))
        inputs = [s for s in before['streams'] if s.get('codec_type') == 'audio']
        outputs = [s for s in after['streams'] if s.get('codec_type') == 'audio']
        assert len(inputs) == len(outputs) == 2
        assert all(s['channels'] == 6 and core.stream_bit_rate(s) == 640000 for s in inputs)
        assert all(s['channels'] == 2 and s['codec_name'] == 'aac' and int(s['sample_rate']) == 48000 for s in outputs)
        assert len(clip.stats['audio_bps']) == 2 and sum(clip.stats['audio_bps']) > 0
        results.append({'source_audio': '2 x AC3 640 kb/s, 6 channels',
                        'output_audio': '2 x AAC stereo 48 kHz, planned 192 kb/s each / 384 combined', 'status': 'passed'})
    finally:
        clip.close()
    # Exercise the newly available rates through the real preview pipeline,
    # and check AAC access units fit the decoder's 1536-byte ES buffer.
    for rate in (384, core.AAC_OUTPUT_MAX_KBPS):
        clip = Clip(surround, asdict(Settings(encoder='x264', hw_decode=False, audio_bitrate=rate)),
                    None, [0, 1], [], 48, 26, 'unicode')
        try:
            clip.thread.join(timeout=60)
            assert clip.ready(), clip.error or clip.state
            assert clip.audio_rates == (rate, rate)
            payload = json.loads(subprocess.check_output([clip.ffprobe, '-v', 'error', '-select_streams', 'a',
                '-show_streams', '-show_packets', '-of', 'json', str(clip.path)]))
            assert len(payload['streams']) == 2
            assert all(s['profile'] == 'LC' and s['channels'] == 2 and int(s['sample_rate']) == 48000
                       for s in payload['streams'])
            largest = max(int(p['size']) for p in payload['packets'])
            assert largest <= core.VITA_AAC_MAX_ES_BYTES, largest
            results.append({'aac_target_per_stereo_track': rate, 'largest_packet_bytes': largest,
                            'physical_vita_tested': False, 'status': 'passed'})
        finally:
            clip.close()
    ass = root / 'source-ass.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(source), '-map', '0', '-c', 'copy', '-c:s', 'ass', str(ass)], check=True)
    for subtitles in ([0], []):
        clip = Clip(ass, asdict(Settings(encoder='x264', hw_decode=False)), None, [0], subtitles, 48, 26, 'unicode')
        try:
            clip.thread.join(timeout=60)
            assert clip.ready(), clip.error or clip.state
            assert cue_at(clip.subtitle_lanes[0].cues, .5) == 'First cue'
            assert cue_at(clip.subtitle_lanes[0].cues, 1.5) == ''
            assert cue_at(clip.subtitle_lanes[0].cues, 2.5) == 'Second cue'
            assert clip.subtitle_lanes[0].cues[-1][1] <= 6
            results.append({'subtitle_codec': 'ass', 'subtitles': subtitles, 'status': 'passed'})
        finally:
            clip.close()
    (root / 'validation.json').write_text(json.dumps({
        'cases': results, 'source_unchanged': True,
        'clock': 'real FFplay master clock via dummy SDL audio; pause/resume checked',
        'device_playback': 'speaker and terminal visual checks remain separate',
    }, indent=2) + '\n', encoding='utf-8')
    print(root / 'validation.json')


if __name__ == '__main__':
    main()
