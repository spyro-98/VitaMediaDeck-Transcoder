#!/usr/bin/env python3
"""Host media contracts and before/after descriptions for the six Vita presets."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import BUILTIN_PRESETS, Settings, TerminalApp
import vmd_editor_ui as ui
import vmd_ui
import vmd_estimates as estimates
import vmd_workflow as workflow


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    ffmpeg, ffprobe, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    source = root / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x180:rate=60:duration=1', '-f', 'lavfi', '-i',
                    'sine=sample_rate=48000:duration=1', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-c:a', 'aac', '-b:a', '192k', str(source)], check=True)
    info = core.probe_media(ffprobe, source, False)
    metadata = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)]))
    context = dict(info=info, metadata=metadata, audio=[0], source_size=source.stat().st_size)
    app = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS, input_path=source)
    current = Settings(quality='high', audio_bitrate=192, system_load='balanced')
    current_snapshot = asdict(current)
    names = [name for name in BUILTIN_PRESETS if name.startswith(('Vita HW -', 'Vita SW -'))]
    assert len(names) == 6 and all('Custom settings' not in vmd_ui.preset_hint(name) for name in BUILTIN_PRESETS)
    reports = []
    for index, name in enumerate(names):
        preset = BUILTIN_PRESETS[name]
        different = ui.preset_differences(app, current, preset)
        assert different and all(' → ' in line for line, _ in different)
        assert not ui.preset_differences(app, preset, preset)
        before = asdict(preset)
        plan = estimates.output_plan(preset, **context)
        assert asdict(preset) == before
        assert plan['fps'] == (60 if name in names[:2] else 30)
        # Isolate the media contract from host-specific GPU availability;
        # production HW presets retain automatic encoder/filter selection.
        tested = Settings.from_dict(before)
        tested.encoder, tested.filter_backend, tested.scaler = 'x264', 'cpu', 'lanczos'
        tested.hw_decode, tested.cover_mode = False, 'none'
        tested.encode_speed, tested.system_load = 'fast', 'low'
        output = workflow.output_file(root / f'preset-{index}.mkv')
        command = tested.command(Path(core.__file__), source, output)
        with (root / f'preset-{index}.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        payload = core.validate_output(ffprobe, output, 1, 0, plan['fps'], False, info.duration, expected_attachment_count=0)
        video = next(stream for stream in payload['streams'] if stream.get('codec_type') == 'video')
        if name.startswith('Vita SW'):
            assert preset.encoder == 'x264' and preset.content_tune == 'vita-sw'
            assert video['has_b_frames'] == 0
            log = (root / f'preset-{index}.log').read_text()
            assert '-tune film,fastdecode' in log and '-bf 0' in log and '-refs 1' in log
        else:
            assert preset.encoder == 'auto'
        saved = workflow.read_json(workflow.recovery_path(output))
        assert saved['settings']['content_tune'] == preset.content_tune
        reports.append(dict(name=name, fps=float(plan['fps']), video_budget=plan['target_bps'],
                            audio_ceiling=preset.audio_bitrate, b_frames=video['has_b_frames'],
                            description=vmd_ui.preset_hint(name), differences=[line for line, _ in different]))
    assert reports[0]['video_budget'] > reports[1]['video_budget'] > reports[2]['video_budget']
    assert reports[3]['video_budget'] > reports[4]['video_budget'] > reports[5]['video_budget']
    selected = BUILTIN_PRESETS[names[5]]
    assert asdict(current) == current_snapshot  # Browsing did not apply a preset.
    # Render every page in the narrow sidebar; descriptions and comparisons
    # must remain inside their rectangle, with real FPS instead of fractions alone.
    app.color = lambda _: 0
    app.preset_index, app._av_pages = 5, {}
    app.presets = SimpleNamespace(rows=lambda: [(name, False, BUILTIN_PRESETS[name]) for name in names])
    app._av_estimate_context = context
    all_text = []
    for page in range(30):
        app._av_pages['presets'] = page
        layer = ui.TextLayer()
        layer.resize(50, 48)
        layer.begin()
        ui.presets_panel(app, layer, current, 2, 2, 40, 38)
        all_text.append('\n'.join(''.join(char for char, _ in row) for row in layer.rows))
        assert all(char == ' ' for row in layer.rows for char, _ in row[40:])
    combined = '\n'.join(all_text)
    assert all(token in combined for token in ('current → preset', 'Quality:', 'File budget:', 'Video budget:', 'Effective FPS:'))
    workflow.atomic_json(root / 'validation.json', {'presets': reports,
        'scope': 'Real host encodes, source cadence/caps, H.264/AAC validation, saved settings and paginated comparison; physical Vita playback pending.'})
    print(root / 'validation.json')


if __name__ == '__main__':
    main()
