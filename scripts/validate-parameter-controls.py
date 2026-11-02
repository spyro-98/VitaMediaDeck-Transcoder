#!/usr/bin/env python3
"""Ordered controls, high defaults and responsive preset/queue viewports."""
import curses
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import TerminalApp, Settings, BUILTIN_PRESETS
import vmd_editor_ui as ui


def main():
    for key in (ord('q'), ord('Q')):
        for accepted in (False, True):
            prompts = []
            idle = SimpleNamespace(process=None, worker=None, running=True,
                confirm=lambda message, **labels: prompts.append((message, labels)) or accepted)
            TerminalApp.handle_key(idle, key)
            assert idle.running is not accepted and len(prompts) == 1
            assert prompts[0][1] == {'accept_label': 'EXIT', 'reject_label': 'STAY'}
    for save, quit_confirmed in ((False, False), (False, True), (True, False), (True, True)):
        prompts, cancelled, saved = [], [], []
        answers = iter((save, quit_confirmed))
        active = SimpleNamespace(process=SimpleNamespace(poll=lambda: None), worker=None, running=True, logs=[],
            confirm=lambda message, **labels: prompts.append((message, labels)) or next(answers),
            save_resume_state=lambda: saved.append(True) or Path('/tmp/resume.json'),
            cancel_conversion=lambda: cancelled.append(True))
        TerminalApp.handle_key(active, ord('Q'))
        assert active.running is not quit_confirmed
        assert len(saved) == int(save) and len(cancelled) == int(quit_confirmed) and len(prompts) == 2
    settings = Settings()
    assert settings.quality == 'maximum' and settings.audio_bitrate == core.AAC_OUTPUT_MAX_KBPS
    assert Fraction(settings.max_fps) == 60 and settings.system_load == 'full'
    assert settings.denoise == settings.sharpen == settings.deband == 'off'
    assert settings.hw_decode and not settings.force_hdr and not settings.overwrite
    cli = core.parse_args(['/tmp/source.mkv'])
    assert (cli.quality, cli.audio_bitrate, cli.system_load) == (settings.quality, settings.audio_bitrate, settings.system_load)
    app = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS, active_settings=lambda: settings,
                          _av_plan={'tracks': [{'source_bps': 640000}]})
    ordered = {'quality': ('compact', 'balanced', 'high', 'maximum'),
               'system_load': ('low', 'balanced', 'full'),
               'encode_speed': ('fast', 'medium', 'slow', 'veryslow'),
               'denoise': core.VIDEO_OPTION_CHOICES['denoise'], 'sharpen': ('off', 'light', 'medium'),
               'deband': ('off', 'light', 'medium'), 'hw_decode': (False, True),
               'force_hdr': (False, True), 'overwrite': (False, True)}
    for attribute, values in ordered.items():
        app.setting_index = next(i for i, (_, name, _) in enumerate(app.SETTING_ROWS) if name == attribute)
        setattr(settings, attribute, values[0])
        for expected in (*values[1:], values[-1], values[-1]):
            TerminalApp.handle_settings_key(app, curses.KEY_RIGHT)
            assert getattr(settings, attribute) == expected, attribute
        for expected in (*reversed(values[:-1]), values[0], values[0]):
            TerminalApp.handle_settings_key(app, curses.KEY_LEFT)
            assert getattr(settings, attribute) == expected, attribute
    app.setting_index = 3
    for original in ('0.5', '12', '23.5', '23.976', '24000/1001', '25', '29.970', '59.95', '60'):
        for key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            settings.max_fps = original
            TerminalApp.handle_settings_key(app, key)
            changed = Fraction(settings.max_fps)
            assert changed <= Fraction(original) if key == curses.KEY_LEFT else changed >= Fraction(original)
    app.setting_index = 4
    for current in (1, 95, 192, 576):
        app._av_plan = {'tracks': [{'source_bps': 95000}]}
        for key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            settings.audio_bitrate = current
            TerminalApp.handle_settings_key(app, key)
            assert settings.audio_bitrate <= current if key == curses.KEY_LEFT else settings.audio_bitrate >= current
            assert core.audio_bitrate_target(settings.audio_bitrate, 95000) <= 95

    presets = [(name, False, preset) for name, preset in BUILTIN_PRESETS.items()]
    presets += [(f'Custom {index:02}', True, Settings()) for index in range(15)]
    menu = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS, input_path=Path('/tmp/source.mkv'),
        color=lambda value: value, preset_index=0, presets=SimpleNamespace(rows=lambda: presets), _av_pages={})
    for width, height in ((18, 10), (28, 22), (40, 40), (60, 80)):
        visible = []
        for index in (0, len(presets) // 2, len(presets) - 1):
            menu.preset_index = index
            layer = ui.TextLayer()
            layer.resize(width + 8, height + 4)
            layer.begin()
            ui.presets_panel(menu, layer, Settings(), 1, 2, height, width)
            assert all(char == ' ' for row in layer.rows for char, _ in row[width + 2:])
            if height >= 12:
                count = min(len(presets), max(2, (height - 4) * 2 // 3))
                visible.append(count)
                body = [''.join(char for char, _ in row) for row in layer.rows[4:4 + count]]
                assert len(body) == count and sum(bool(row.strip()) for row in body) == count
        if height >= 40:
            assert min(visible) >= 20
            assert len(set(visible)) == 1  # Selecting another preset never moves the separator.
        if height == 80:
            assert visible[0] == len(presets)

    queue_root = Path('/tmp/queue')
    sources = [queue_root / f'Season 1/Episode {i:03}.mkv' for i in range(60)]
    queue_app = SimpleNamespace(input_path=queue_root, batch_sources=sources, batch_enabled={}, color=lambda value: value)
    for width, height in ((18, 3), (28, 16), (40, 40), (60, 80)):
        for index in (0, 30, 59):
            queue_app.batch_selection_index = index
            layer = ui.TextLayer()
            layer.resize(width + 8, height + 4)
            layer.begin()
            ui.queue_panel(queue_app, layer, 1, 2, height, width)
            assert all(char == ' ' for row in layer.rows for char, _ in row[width + 2:])
            if height >= 16:
                text = ''.join(''.join(char for char, _ in row) for row in layer.rows)
                assert f'Episode {index:03}.mkv'.replace(' ', '') in ''.join(text.split())
    print('Passed: exit confirmation, high defaults, monotonic/clamped arrows, rational FPS, dynamic AAC caps, full-height preset/queue lists.')


if __name__ == '__main__':
    main()
