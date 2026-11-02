#!/usr/bin/env python3
"""Exercise replacement safety, support layout, UI authorization and recovery."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
import io
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import TerminalApp, Settings
import vmd_workflow as workflow


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    ffmpeg, _, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    source = root / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x180:rate=30:duration=1', '-f', 'lavfi', '-i',
                    'sine=sample_rate=48000:duration=1', '-c:v', 'libx264',
                    '-preset', 'ultrafast', '-c:a', 'aac', str(source)], check=True)
    source_bytes = source.read_bytes()
    options = ['--encoder', 'x264', '--filter-backend', 'cpu', '--scaler', 'lanczos',
               '--no-hw-decode', '--system-load', 'low']

    def run(arguments, code=0):
        text = io.StringIO()
        with redirect_stdout(text), redirect_stderr(text):
            actual = core.main([*map(str, arguments), *options])
        with (root / 'validation.log').open('a') as log:
            log.write(text.getvalue())
        assert actual == code, text.getvalue()[-4000:]
        return text.getvalue()

    output = workflow.output_file(root / 'single' / 'movie.mkv')
    output.parent.mkdir(parents=True)
    output.write_bytes(b'old output')
    run([source, output], 2)
    assert output.read_bytes() == b'old output'
    assert list(output.parent.iterdir()) == [output]
    run([source, output, '--overwrite'])  # Includes cover extraction/attachment.
    support = workflow.media_support_directory(output)
    assert support == output.parent
    assert {item.name for item in support.iterdir()} == {output.name, 'resume.json', 'report.json'}
    assert set(output.parent.parent.iterdir()) == {output.parent}
    old_bytes, old_identity = output.read_bytes(), workflow.fingerprint(output)
    with patch.object(core, 'validate_output', side_effect=core.TranscodeError('injected validation failure')):
        run([source, output, '--overwrite'], 2)
    assert output.read_bytes() == old_bytes and workflow.fingerprint(output) == old_identity
    state = workflow.read_json(workflow.recovery_path(output))
    assert state['entries'][0]['previous_output'] == old_identity
    assert not any(path.exists() for path in core.partial_output_paths(output))
    # An old, perfectly valid output must not satisfy a failed replacement.
    assert 'SKIPPED:' not in run([source, output, '--resume-state', workflow.recovery_path(output)])
    assert workflow.fingerprint(output) != old_identity
    assert 'SKIPPED:' in run([source, output, '--resume-state', workflow.recovery_path(output)])
    run([source, source, '--overwrite'], 2)
    assert source.read_bytes() == source_bytes
    requested = root / 'requested.mkv'
    run([source, requested, '--no-cover'])
    assert not requested.exists() and workflow.output_file(requested).is_file()
    assert not any(path.name == workflow.SUPPORT_FOLDER for path in workflow.output_file(requested).parent.rglob('*'))
    for name in ('video.partial.mkv', 'repaired.partial.mkv', 'covered.partial.mkv', 'resume.mkv'):
        candidate = workflow.output_file(root / name)
        assert candidate not in core.partial_output_paths(candidate)
        assert workflow.output_file(candidate) == candidate
    # Older checkpoints remain readable without moving an existing result.
    legacy_output = root / 'legacy.mkv'
    legacy_output.write_bytes(output.read_bytes())
    legacy_state = workflow.read_json(workflow.recovery_path(output))
    legacy_state.pop('layout', None)
    legacy_state.pop('application', None)
    legacy_state['output_path'] = str(legacy_output)
    legacy_state['entries'][0]['output'] = str(legacy_output)
    legacy_path = root / 'legacy.resume.json'
    workflow.atomic_json(legacy_path, legacy_state)
    assert 'SKIPPED:' in run([source, legacy_output, '--resume-state', legacy_path])
    assert legacy_output.is_file() and not workflow.output_file(legacy_output).exists()

    inputs, outputs = root / 'episodes', root / 'converted'
    for season in ('season1', 'season2'):
        (inputs / season).mkdir(parents=True)
        shutil.copyfile(source, inputs / season / 'episode.mkv')
    sources = core.batch_video_sources(inputs)
    targets = [core.batch_output_path(inputs, outputs, item) for item in sources]
    targets[-1].parent.mkdir(parents=True)
    targets[-1].write_bytes(b'previous episode')
    run([inputs, outputs, '--no-cover'], 2)
    assert not targets[0].exists() and targets[-1].read_bytes() == b'previous episode'
    run([inputs, outputs, '--no-cover', '--overwrite'])
    assert workflow.recovery_path(outputs, True).is_file()
    for target in targets:
        media = workflow.media_support_directory(target, outputs)
        assert (media / 'transcode.log').is_file()
        assert not any(path.exists() for path in core.partial_output_paths(target, media))
        assert {item.name for item in target.parent.iterdir()} == {target.name, 'resume.json', 'report.json', 'transcode.log'}
    assert not any(path.name == workflow.SUPPORT_FOLDER for path in outputs.rglob('*')
                   if path.parent != outputs)
    assert run([inputs, outputs, '--resume-state', workflow.recovery_path(outputs, True)]).count('SKIPPED:') == 2
    identities = [workflow.fingerprint(target) for target in targets]
    real_emit = core.emit_progress
    def interrupt_after_skip(phase, status, *args, **kwargs):
        if phase == 'complete' and kwargs.get('batch_index') == 1:
            raise KeyboardInterrupt
        return real_emit(phase, status, *args, **kwargs)
    with patch.object(core, 'emit_progress', interrupt_after_skip):
        run([inputs, outputs, '--resume-state', workflow.recovery_path(outputs, True)], 130)
    assert run([inputs, outputs, '--resume-state', workflow.recovery_path(outputs, True)]).count('SKIPPED:') == 2
    assert [workflow.fingerprint(target) for target in targets] == identities
    assert run([inputs, outputs, '--resume-state', workflow.recovery_path(outputs, True)]).count('SKIPPED:') == 2
    previous_episode = targets[0].read_bytes()
    targets[0].unlink()
    targets[0].symlink_to(sources[1])
    run([inputs, outputs, '--no-cover', '--overwrite'], 2)
    assert sources[1].read_bytes() == source_bytes
    targets[0].unlink()
    targets[0].write_bytes(previous_episode)
    # Legacy automatic checkpoints are discovered; only known partials removed.
    legacy = (output.parent.parent / output.name).with_suffix('.resume.json')
    legacy.write_text(json.dumps(state))
    current = workflow.recovery_path(output)
    saved_checkpoint = current.read_bytes()
    current.unlink()
    assert workflow.find_recovery(output) == legacy
    current.write_bytes(saved_checkpoint)
    assert workflow.find_recovery(output) == current
    partial = output.with_name(f'.{output.stem}.partial.mkv')
    partial.write_bytes(b'interrupted')
    core.cleanup_partial_output(output)
    assert not partial.exists() and output.is_file()
    hidden = workflow.output_file(inputs / 'generated-copy.mkv')
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b'not a source')
    workflow.atomic_json(hidden.parent / 'resume.json', {'application': 'VitaMediaDeck Transcoder', 'layout': workflow.LAYOUT})
    assert core.batch_video_sources(inputs) == sources

    def app_for(input_path, output_path):
        app = TerminalApp.__new__(TerminalApp)
        app.input_path, app.output_path = input_path, output_path
        app.settings = Settings(encoder='x264', filter_backend='cpu', scaler='lanczos', hw_decode=False)
        app.worker = app.process = app.resume_state_path = app.active_entries = None
        app.batch_sources = sources if input_path.is_dir() else []
        app.batch_selection_index, app.batch_edit_source, app.tab = 0, None, 2
        app.batch_enabled, app.batch_settings = {}, {}
        app.selected_audio_tracks, app.selected_subtitle_tracks = {0}, set()
        app.logs, app.events, app.suspended_processes = [], queue.Queue(), []
        app.script, app.status = Path(core.__file__), ''
        app.drain_events = lambda: None
        return app

    class Worker:
        def __init__(self, **kwargs): self.command = kwargs['args'][0]
        def start(self): pass
        def is_alive(self): return False

    # Cancelling authorizes no output change and creates no support artifacts.
    fresh_output = workflow.output_file(root / 'ui' / 'existing.mkv')
    fresh_output.parent.mkdir(parents=True)
    fresh_output.write_bytes(b'unchanged')
    app = app_for(source, fresh_output)
    app.confirm = lambda _: False
    app.start_conversion()
    assert app.worker is None and list(fresh_output.parent.iterdir()) == [fresh_output]
    assert fresh_output.read_bytes() == b'unchanged'
    prompts = []
    app.confirm = lambda message: prompts.append(message) or True
    with patch('vitamediadeck_tui.threading.Thread', Worker):
        app.start_conversion()
    assert len(prompts) == 1 and '--overwrite' in app.worker.command
    assert app.settings.overwrite is False  # Authorization applies only to this run.
    batch = app_for(inputs, outputs)
    batch.batch_enabled[sources[1]] = False
    batch.confirm = lambda message: prompts.append(message) or True
    with patch('vitamediadeck_tui.threading.Thread', Worker):
        batch.start_conversion()
    assert '1 existing' in prompts[-1]
    plan = workflow.read_json(batch.batch_plan_path)
    assert plan['entries'][0]['settings']['overwrite'] is True
    assert plan['entries'][1]['settings']['overwrite'] is False
    # Explicit CLI authorization wins over a stored plan's default false.
    command_args = core.parse_args([str(inputs), str(outputs), '--overwrite'])
    assert core.with_batch_settings(command_args, {'overwrite': False}).overwrite
    assert source.read_bytes() == source_bytes
    workflow.atomic_json(root / 'validation.json', {
        'overwrite': 'declined, accepted, failed validation and input protection checked',
        'recovery': 'failed replacement redone; completed outputs skipped twice; legacy paths readable',
        'storage': 'single and nested equal-name episodes isolated; no sidecar clutter or leftover partials',
        'ui': 'cancel creates nothing; replacement is per run; excluded items ignored',
    })
    print(root / 'validation.json')


if __name__ == '__main__':
    main()
