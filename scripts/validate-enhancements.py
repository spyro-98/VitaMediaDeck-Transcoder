#!/usr/bin/env python3
"""Real filters, preview position, settings propagation and fixed UI footprints."""
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os
import subprocess
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
import vmd_workflow as workflow
from vitamediadeck_tui import Settings, TerminalApp, BUILTIN_PRESETS
from vmd_av_preview import Clip
from vmd_preview_state import video_settings
import vmd_editor_ui as ui
import vmd_estimates as estimates
import vmd_ui


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ['SDL_AUDIODRIVER'] = 'dummy'
    ffmpeg, ffprobe, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    source = root / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30:duration=8',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=8',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source)], check=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    info = core.probe_media(ffprobe, source, False)
    caps = core.discover_capabilities(ffmpeg)
    metadata = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)]))
    results = []
    for name, preset in BUILTIN_PRESETS.items():
        parsed = core.parse_args(preset.command(Path(core.__file__), source, root / 'preset.mkv')[3:])
        assert core.video_options(parsed) == core.video_options(preset), name
        assert core.bitrate_plan(960, 544, Fraction(30), parsed.quality, parsed.content_tune)[0] > 0
    assert BUILTIN_PRESETS['Film Preserve Grain'].encoder == 'x264'
    # Smoke-test every selectable picture filter and compare real decoded pixels.
    frames = {}
    base_options = {**core.video_options({}), 'filter_backend': 'cpu', 'scaler': 'lanczos'}
    cases = [('default', base_options)]
    for field in ('denoise', 'sharpen', 'deband', 'scaler'):
        cases += [(f'{field}-{value}', {**base_options, field: value})
                  for value in core.VIDEO_OPTION_CHOICES[field] if value != base_options[field] and value not in ('auto', 'apple')]
    for name, options in cases:
        chain = core.filter_chain(info, 960, 544, Fraction(30), 'x264', caps, 'mobius', options)
        payload = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(source), '-an', '-sn',
            '-vf', chain + ',select=eq(n\\,5)', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        assert len(payload) == 960 * 544 * 3
        frames[name] = hashlib.sha256(payload).hexdigest()
        if name != 'default':
            assert frames[name] != frames['default'], name
        results.append(dict(filter=name, pixels='960x544 RGB', status='passed'))
    missing = replace(caps, filters=caps.filters - {'hqdn3d'})
    try:
        core.filter_chain(info, 960, 544, Fraction(30), 'x264', missing, 'mobius', {'denoise': 'light'})
        raise AssertionError('Missing filter silently ignored')
    except core.TranscodeError as exc:
        assert 'hqdn3d' in str(exc) and 'unavailable' in str(exc)
    # HDR uses the matching resize backend and still includes selected filters.
    for scaler in core.VIDEO_OPTION_CHOICES['scaler']:
        if scaler in ('auto', 'apple'):
            continue
        chain = core.filter_chain(replace(info, hdr=True), 960, 544, Fraction(30), 'x264', caps,
                                  'hable', {'scaler': scaler, 'denoise': 'light'})
        assert ('filter=spline36' if scaler == 'spline' else 'filter=' + scaler) in chain
        assert 'hqdn3d' in chain and 'tonemap=tonemap=hable' in chain
    assert '23.976 fps (24000/1001)' == vmd_ui.fps_label('24000/1001')
    for exact in (Fraction(24), Fraction(30), Fraction(60), Fraction(24000, 1001), Fraction(30000, 1001)):
        assert core.normalize_fps(exact) == exact
    settings = Settings(encoder='x264', max_fps='60')
    for ceiling, effective in [('60', 30), ('24000/1001', Fraction(24000, 1001)), ('12', 12)]:
        settings.max_fps = ceiling
        plan = estimates.output_plan(settings, info, metadata, [0])
        assert plan['fps'] == effective
        effects = estimates.parameter_effects(settings, info, plan)
        assert vmd_ui.fps_label(ceiling) in effects['max_fps'][0]
        assert ('FPS kept' in effects['max_fps'][2]) == (ceiling == '60')
    # Every non-numeric value keeps the same parameter/page geometry.
    snapshots = {}
    app = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS)
    for _, field, choices in TerminalApp.SETTING_ROWS:
        if not choices or field in ('audio_bitrate',):
            continue
        for value in choices:
            selected = Settings(**{field: value})
            app._av_effects = estimates.parameter_effects(selected, info, estimates.output_plan(selected, info, metadata, [0]))
            for width, height in ((16, 6), (30, 12), (42, 30), (80, 40)):
                pages = ui.parameter_pages(app, selected, width, height, 'video')
                locations = [(p, row, kind, at) for p, (lines, _) in enumerate(pages)
                             for row, (_, kind, at) in enumerate(lines)]
                if (width, height) in snapshots:
                    assert snapshots[width, height] == locations, (field, value, width)
                snapshots[width, height] = locations
    for width in (50, 75, 150):
        layer = ui.TextLayer()
        layer.resize(width + 4, 8)
        layer.begin()
        ui.preview_controls(SimpleNamespace(color=lambda _: 0), layer, 2, 2, width)
        text = '\n'.join(''.join(char for char, _ in row) for row in layer.rows)
        assert all(key in text for key in ('Space', 'Z Fit/Vita', 'J', '[ ]', 'G', 'Home/End'))
    full = Settings(encoder='x264', quality='maximum', content_tune='film-grain', encode_speed='fast',
                    denoise='light', sharpen='light', deband='light', scaler='bicubic', max_fps='12', hw_decode=False)
    args = core.parse_args(full.command(Path(core.__file__), source, root / 'full.mkv')[3:])
    assert core.video_options(args) == core.video_options(full)
    child = core.with_batch_settings(core.parse_args([str(source)]), asdict(full))
    reparsed = core.parse_args(core.batch_child_command(child, source, root / 'batch.mkv')[2:])
    assert core.video_options(reparsed) == core.video_options(full) and reparsed.quality == 'maximum'
    command = full.command(Path(core.__file__), source, root / 'full.mkv')
    with (root / 'full.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    full_output = workflow.output_file(root / 'full.mkv')
    output = core.probe_media(ffprobe, full_output, False)
    assert abs(float(output.fps) - 12) < .001
    saved = json.loads(workflow.recovery_path(full_output).read_text())
    assert core.video_options(saved['settings']) == core.video_options(full)
    log = (root / 'full.log').read_text()
    assert all(part in log for part in ('hqdn3d=', 'unsharp=', 'deband=', 'flags=bicubic', '-preset fast', '-crf 16'))
    results.append(dict(full_cli='maximum/grain/filters/12fps', batch_and_resume_options='preserved', status='passed'))
    # Rebuilding or remuxing preserves the frame position and stays stopped.
    clips = []
    def build(selected, previous=None, audio=(0,), index=None):
        clip = Clip(source, asdict(selected), index, audio, [], 48, 26, 'unicode',
                    source_info=info, source_metadata=metadata, previous=previous)
        clips.append(clip)
        if previous and index == previous.index:
            assert abs(clip.position() - previous.position()) < .05
            assert not clip.frames and not clip.thumbnails
        clip.thread.join(timeout=120)
        assert clip.ready(), clip.error or clip.state
        assert not clip.playing
        return clip
    try:
        initial = Settings(encoder='x264', hw_decode=False)
        first = build(initial)
        first.held = 2.35
        altered = replace(initial, sharpen='light')
        changed = build(altered, first)
        assert changed.work['video_encoded'] and abs(changed.position() - 2.35) < .001
        assert video_settings(asdict(initial)) != video_settings(asdict(altered))
        changed.held = 3.1
        changed_again = build(replace(altered, max_fps='12'), changed)
        assert changed_again.frame_fps == 12 and abs(changed_again.position() - 3.1) < .001
        no_audio = build(replace(altered, max_fps='12'), changed_again, audio=())
        assert no_audio.work['video_encoded'] and abs(no_audio.position() - 3.1) < .001
        no_audio.play()
        assert no_audio.position() >= 3.1
        no_audio.stop()
        changed_again.play()
        clock = changed_again.clock
        assert abs(clock.offset - 3.1) < 1 / 48000
        assert '-ss' not in clock.process.args
        with wave.open(clock.process.args[-1], 'rb') as resumed:
            assert resumed.getframerate() == 48000 and resumed.getnchannels() == 2
            held_sample = round(clock.offset * 48000)
            assert resumed.readframes(resumed.getnframes()) == changed_again.audio_pcm[changed_again.audio_choice][held_sample * 4:]
        changed_again.stop()
        moved = build(initial, changed_again, index=30)
        assert moved.position() == 0
        results.append(dict(preview_position='2.35s and 3.1s held across video/fps/audio edits',
                            new_sample='starts at zero', playback='manual, resumes held position', status='passed'))
    finally:
        for clip in clips:
            clip.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    (root / 'validation.json').write_text(json.dumps(dict(cases=results, source_unchanged=True,
        fixed_parameter_geometry=True, physical_vita_tested=False), indent=2) + '\n')
    print(root / 'validation.json')


if __name__ == '__main__':
    main()
