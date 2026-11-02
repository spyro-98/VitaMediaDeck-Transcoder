#!/usr/bin/env python3
"""Encoded-frame progress, real sequential files, and dark-to-lit movie timeline."""
from pathlib import Path
from fractions import Fraction
import io
import json
import os
import subprocess
import queue
import sys
import zlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import TerminalApp, LiveProgress, Settings
from vmd_conversion_timeline import MovieTimeline
from vmd_av_preview import png
import vmd_progress as progress
import vmd_editor_ui as ui
import vmd_processing as processing
import vmd_workflow as workflow


def app_for(folder, sources):
    app = TerminalApp.__new__(TerminalApp)
    app.input_path, app.batch_sources, app.batch_metadata = folder, sources, {}
    app.batch_weights, app.batch_edit_source, app.batch_selection_index = (0., 1., 1.), None, 0
    app.live, app.info, app.status, app.paused = LiveProgress(started_at=1), None, '', False
    app.progress_seconds = None
    return app


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    inputs, outputs = root / 'inputs', root / 'outputs'
    inputs.mkdir()
    sources = [inputs / 'one.mkv', inputs / 'two.mkv']
    ffmpeg, ffprobe, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    for source, length in zip(sources, (1.2, 2.4)):
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size=320x180:rate=30:duration={length}',
            '-f', 'lavfi', '-i', f'sine=duration={length}:sample_rate=48000', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-c:a', 'aac', str(source)], check=True)
    app = app_for(inputs, sources)
    context = dict(batch_index=1, batch_total=2, batch_source='one.mkv', batch_duration=40,
                   batch_completed_weight=0, batch_weight=40, batch_total_weight=200)
    def event(phase, state, **values):
        app.handle_machine_progress(dict(context, phase=phase, state=state, **values))
    for phase, state in [('analysis', 'start'), ('analysis', 'done'), ('preflight', 'start'), ('preflight', 'done')]:
        event(phase, state)
        assert app.live.local_ratio == app.live.overall_ratio == 0
    event('transcode', 'video_start', duration=40, video_fps=30)
    app.update_ffmpeg_stats('frame=0 fps=0 time=00:00:20.00 speed=3x')
    assert app.live.local_ratio == 0
    app.update_ffmpeg_stats('frame=1 fps=9 time=00:00:20.00 speed=3x')
    assert abs(app.live.local_ratio - 1 / 1200) < 1e-8
    event('transcode', 'progress', frame=600, duration=40, video_fps=30, progress=.99, media_seconds=9999)
    assert app.live.local_ratio == .5 and app.live.overall_ratio == .1
    event('transcode', 'fallback')
    assert app.live.local_ratio == 0 and app.live.overall_ratio == 0
    event('transcode', 'video_start', duration=40, video_fps=30)
    event('transcode', 'progress', frame=300, duration=40, video_fps=30)
    for phase, state in [('transcode', 'stage'), ('media_validation', 'start'), ('package', 'progress')]:
        event(phase, state, detail='AUDIO PASS', progress=.9)
        assert app.live.local_ratio == .25
    event('transcode', 'video_done')
    app.update_ffmpeg_stats('size=90kB time=00:00:10.00 speed=10x')
    assert app.live.local_ratio == 1 and app.live.media_seconds == 40
    context.update(batch_index=2, batch_source='two.mkv', batch_duration=160,
                   batch_completed_weight=40, batch_weight=160)
    event('analysis', 'start')
    assert app.live.local_ratio == 0 and app.live.overall_ratio == .2
    assert app.live.media_duration == 160 and app.batch_selection_index == 1
    event('transcode', 'video_start', duration=160, video_fps=30)
    event('transcode', 'progress', frame=2400, duration=160, video_fps=30)
    assert app.live.local_ratio == .5 and abs(app.live.overall_ratio - .6) < 1e-8
    event('queue', 'failed')
    assert app.live.local_ratio == .5 and app.live.overall_ratio == 1
    for value in ('nan', 'inf', -4, None):
        app.set_local_progress(value)
        assert app.live.local_ratio == .5
    # Replay actual machine events from two real successive conversions.
    command = [sys.executable, str(Path(core.__file__)), str(inputs), str(outputs), '--encoder', 'x264',
               '--no-hw-decode', '--no-cover', '--encode-speed', 'fast', '--filter-backend', 'cpu', '--scaler', 'lanczos']
    result = subprocess.run(command, text=True, capture_output=True, env={**os.environ, 'VMD_MACHINE_PROGRESS': '1'})
    (root / 'batch.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr[-1000:]
    app = app_for(inputs, sources)
    starts, video_done = [], []
    for line in result.stdout.splitlines():
        if not line.startswith(core.PROGRESS_PREFIX):
            continue
        payload = json.loads(line[len(core.PROGRESS_PREFIX):])
        app.handle_machine_progress(payload)
        if payload['phase'] in ('analysis', 'preflight'):
            assert app.live.local_ratio == 0, payload
        if payload['state'] == 'video_start':
            starts.append((app.live.batch_index, app.live.local_ratio))
        if payload['state'] == 'video_done':
            video_done.append(app.live.batch_index)
            assert app.live.local_ratio == 1
    assert starts == [(1, 0), (2, 0)] and video_done == [1, 2]
    assert app.live.overall_ratio == 1
    # A queued completion from the last worker cannot complete the next job.
    restarted = app_for(inputs, sources)
    restarted.input_path, restarted.output_path = sources[0], workflow.output_file(root / 'restart.mkv')
    restarted.settings, restarted.script = Settings(), Path(core.__file__)
    restarted.worker, restarted.process, restarted.logs = None, None, []
    restarted.events, restarted.suspended_processes = queue.Queue(), []
    restarted.resume_state_path = None
    restarted.selected_audio_tracks, restarted.selected_subtitle_tracks = set(), set()
    restarted.recovery_path = lambda: None
    restarted.inspect_output = restarted.save_resume_state = lambda: None
    restarted.planned_resume_entries = lambda: []
    restarted.live.local_ratio = restarted.live.overall_ratio = 1
    restarted.events.put(('done', 0))
    class Worker:
        def __init__(self, **kwargs): self.alive = False
        def start(self): self.alive = True
        def is_alive(self): return self.alive
    with patch('vitamediadeck_tui.threading.Thread', Worker):
        restarted.start_conversion()
        assert restarted.live.local_ratio == restarted.live.overall_ratio == 0
        assert restarted.events.empty() and restarted.batch_weights == (0, 1, 1)
        stamp = restarted.live.started_at
        restarted.start_conversion()
        assert restarted.live.started_at == stamp and 'already running' in restarted.status
    source = sources[1]
    info = core.probe_media(ffprobe, source, False)
    timeline = MovieTimeline(source, Settings().__dict__, info, 6, 192, 109, 'iterm2', png)
    try:
        timeline.thread.join(timeout=40)
        assert not timeline.error and all(timeline.rgb), timeline.error
        dark, _ = timeline.render(0)
        half, _ = timeline.render(.5)
        full, _ = timeline.render(1)
        assert all(dark[i] != half[i] for i in range(3))
        assert all(dark[i] == half[i] for i in range(3, 6))
        assert all(dark[i] != full[i] for i in range(6))
        assert timeline.positions[0] == 0 and timeline.positions[-1] > info.duration * .8
        assert timeline.render(1)[0] == full
    finally:
        timeline.close(wait=True)
    layer = ui.TextLayer()
    layer.resize(80, 8)
    colors = type('Colors', (), {'color': lambda _, n: n})()
    for ratio, expected in [(0, 0), (.5, 38), (1, 76)]:
        layer.begin()
        ui.progress_line(colors, layer, 2, 2, 76, ratio)
        assert sum(style == 7 for _, style in layer.rows[2]) == expected
    (root / 'validation.json').write_text(json.dumps(dict(real_files=2, movie_starts=starts,
        encoded_frame_progress=True, no_loading_credit=True, unequal_duration_weights=True,
        retry_resets=True, failure_does_not_fill_video=True, dark_half_full_timeline=True), indent=2) + '\n')
    print(root / 'validation.json')


if __name__ == '__main__':
    main()
