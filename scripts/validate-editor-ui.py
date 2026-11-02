#!/usr/bin/env python3
"""Check sidebar pagination and retained rendering without a graphical terminal."""
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from fractions import Fraction
from unittest.mock import patch
import curses
import io
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vitamediadeck_tui import Settings, TerminalApp, BUILTIN_PRESETS
import vmd_editor_ui as ui
import vmd_av_preview as av
import vmd_preview as graphics
import vmd_estimates as estimates
import vitamediadeck_transcoder as core


def retained_text():
    layer = ui.TextLayer()
    layer.resize(80, 24)
    layer.begin()
    layer.add(0, 2, 'Ready')
    first = io.StringIO()
    layer.present(first, [(3, 22, 54, 12)])
    assert '\x1b[2J' in first.getvalue()
    layer.begin()
    layer.add(0, 2, 'Playing')
    layer.add(4, 2, 'Quality: high')
    second = io.StringIO()
    layer.present(second, [(3, 22, 54, 12)])
    value = second.getvalue()
    assert '\x1b[2J' not in value and '\x1b[K' not in value and '\x1b[J' not in value
    # The row crossing a native image is limited to its two outside segments.
    assert '\x1b[5;1H' in value and '\x1b[5;23H' not in value
    layer.begin()
    third = io.StringIO()
    layer.present(third, [(3, 22, 54, 12)])
    assert 'Quality' not in third.getvalue() and '\x1b[5;1H' in third.getvalue()


def pagination():
    settings = Settings(ffmpeg='/a very long directory/' * 12 + 'ffmpeg')
    app = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS, setting_index=0, color=lambda _: 0)
    for width, height in [(16, 1), (16, 4), (40, 25), (80, 48)]:
        pages = ui.parameter_pages(app, settings, width, height)
        seen = set()
        for lines, fields in pages:
            assert len(lines) <= height
            assert all(len(line) <= width for line, _, _ in lines)
            seen.update(fields)
        assert seen == set(range(len(app.SETTING_ROWS)))
        assert not any(kind == 'description' for lines, _ in pages for _, kind, _ in lines)
    layer = ui.TextLayer()
    layer.resize(180, 50)
    layer.begin()
    ui.sidebar(app, layer, settings, 3, 2, 12, 44, 'video')
    initial = app._parameter_page
    ui.next_page(app, 1)
    layer.begin()
    ui.sidebar(app, layer, settings, 3, 2, 12, 44, 'video')
    assert app._parameter_page == initial + 1
    for index in ui.PARAMETER_SECTIONS['video']:
        app.setting_index = index
        layer.begin()
        ui.sidebar(app, layer, settings, 3, 2, 12, 44, 'video')
        assert index in app._parameter_pages[app._parameter_page][1]


def editor_loop():
    class Screen:
        def __init__(self, app, width, height):
            self.app, self.width, self.height = app, width, height
            self.keys = iter([-1, ord('4'), curses.KEY_NPAGE, curses.KEY_PPAGE, ord(' '), ord(' '), ord('j'),
                              curses.KEY_RIGHT, curses.KEY_DOWN, ord('z'), ord('t'), curses.KEY_NPAGE,
                              ord('t'), ord('1'), ord('2'), curses.KEY_DOWN, ord('3'), curses.KEY_DOWN,
                              ord('5'), curses.KEY_DOWN, 13, 13, ord('x'), ord('e'),
                              ord('6'), 13, ord('x'), ord('e'), ord('5'), ord('+'), ord('6'), 127,
                              ord('8'), 13, curses.KEY_DOWN, ord('9'), ord('h'), curses.KEY_NPAGE, 27, ord('h'), ord('h'),
                              ord('5'), ord('x'), ord('e'), 'mouse-press', 'mouse-move', ord('+'),
                              'mouse-release', ord('c'), ord('c'), ord('q')])
            self.reads = 0
        def getmaxyx(self): return self.height, self.width
        def timeout(self, value): pass
        def clearok(self, value): pass
        def untouchwin(self): pass
        def erase(self): pass  # Only quit/dialog transitions may call this.
        def refresh(self): pass
        def getch(self):
            self.reads += 1
            if self.reads <= 5:
                assert Clip.plays == 0, 'Editor started playback without Space'
            if self.reads == 8:
                assert Clip.plays == 1, 'Changing listening track started a paused clip'
            key = next(self.keys)
            if isinstance(key, str) and key.startswith('mouse-'):
                top, left, columns, rows = self.app._test_image_rect
                fraction = .23 if key == 'mouse-press' else .71
                mouse_x = left + round((columns - 1) * fraction)
                buttons = {'mouse-press': curses.BUTTON1_PRESSED, 'mouse-move': curses.REPORT_MOUSE_POSITION,
                           'mouse-release': curses.BUTTON1_RELEASED}[key]
                self.app._test_mouse = (0, mouse_x, top + rows // 2, 0, buttons)
                self.app._test_expected_fraction = (mouse_x - left) / max(1, columns - 1)
                return curses.KEY_MOUSE
            return key

    class Clip:
        plays = 0
        rectangles = []
        def __init__(self, source, settings, index, audio, subtitles, width, height, backend, **kwargs):
            self.rectangles.append((width, height))
            self.start, self.duration, self.display_fps, self.encoder = 2, 6, 24, 'x264'
            self.state, self.error, self.playing, self.paused, self.clock = 'Ready', None, False, False, None
            payload = av.png(b'\0\0\0', 1, 1) if backend in ('kitty', 'iterm2') else 'frame'
            self.frames, self.thumbnails = [payload], [payload] * kwargs.get('thumbnail_count', 6)
            self.audio, self.audio_choice = audio, 0
            self.audio_lanes = [av.Lane(i, f'A{i + 1}', i in audio, [], '──') for i in range(2)]
            self.subtitle_lanes = [av.Lane(0, 'S1', 0 in subtitles, [(0, 2, 'Caption')])]
            self.preparing = len(audio) != 2 or len(subtitles) != 1
            if self.preparing:
                self.state = 'Updating selected tracks'
            if not audio and not subtitles:
                self.error = 'Synthetic preview failure while keeping the last frame'
        def ready(self): return not self.preparing
        def position(self): return 0
        def play(self):
            Clip.plays += 1
            self.playing, self.paused = True, False
        def toggle(self):
            if not self.playing: self.play()
            else: self.paused = not self.paused
        def stop(self): self.playing = self.paused = False
        def close(self, wait=True): self.stop()

    class CheckedLayer(ui.TextLayer):
        def present(self, stream, protected=()):
            if protected:
                y, x, width, height = protected[0]
                app._test_image_rect = (y, x, width, height)
            for y, x, width, height in protected:
                assert all(not char.strip() for row in self.rows[y:y + height]
                           for char, _ in row[x:x + width]), 'UI text overlaps an image rectangle'
            if protected:
                toolbar = next(i for i, row in enumerate(self.rows)
                               if 'Space/V' in ''.join(char for char, _ in row))
                assert toolbar == 5 and toolbar < protected[0][0]
                if self.width >= 140:
                    toolbar_rows = len(ui.preview_control_lines(self.width - 4))
                    line = ' '.join(''.join(char for char, _ in row) for row in self.rows[toolbar:toolbar + toolbar_rows])
                    assert all(value in line for value in ('Z Fit/Vita', 'J Track', 'L Solo', '[ ]', 'G 1–100', 'Home/End', 'H Filters', 'C Compare', 'mouse drag'))
            return super().present(stream, protected)

    with tempfile.TemporaryDirectory(prefix='vmd-editor-ui-test-') as root:
        source = Path(root) / 'source.mkv'
        source.touch()
        settings = Settings()
        info = SimpleNamespace(audio_stream_count=2, subtitle_codecs=['srt'], duration=10,
                               width=1920, height=1080, fps=Fraction(24), video_codec='hevc', audio_bit_rates=(384000, 192000))
        stat = source.stat()
        probe_key = (source, stat.st_size, stat.st_mtime_ns, settings.ffmpeg, settings.ffprobe, settings.force_hdr)
        for width, height, backend in [(w, h, backend) for w, h in [(180, 50), (76, 22)]
                                        for backend in ('unicode', 'kitty', 'iterm2', 'sixel')]:
            Clip.plays = 0
            settings = Settings(audio_bitrate=192)
            app = SimpleNamespace(running=True, input_path=source, output_path=Path(root) / 'out.mkv',
                preview_backend=backend, preview_samples={}, audio_preview=None, _av_probe=(probe_key, info),
                SETTING_ROWS=TerminalApp.SETTING_ROWS, setting_index=0, selected_audio_tracks={0, 1},
                selected_subtitle_tracks={0}, status='Ready', worker=None, audio_track_indices={},
                tab=2, batch_edit_source=None, preset_index=0, logs=[], scroll={'Log': 0},
                presets=SimpleNamespace(rows=lambda: [(name, False, value) for name, value in BUILTIN_PRESETS.items()]),
                _av_original=(probe_key[:5], {'streams': [
                    {'codec_type': 'video', 'codec_name': 'hevc', 'width': 1920, 'height': 1080, 'bit_rate': '10000000'},
                    {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'sample_rate': '48000', 'bit_rate': '384000'},
                    {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2, 'sample_rate': '48000', 'bit_rate': '192000'},
                    {'codec_type': 'subtitle', 'codec_name': 'srt'}]}))
            app.screen = Screen(app, width, height)
            app.preview_subject = lambda: (source, settings)
            app.drain_events = lambda: None
            app.color = lambda _: 0
            app.handle_key = lambda _: setattr(app, 'running', False)
            app.handle_settings_key = lambda _: setattr(settings, 'audio_bitrate' if app.setting_index == 4 else 'quality', settings.audio_bitrate + 64 if app.setting_index == 4 else 'balanced')
            conversions = []
            app.start_conversion = lambda: conversions.append(True)
            output = io.StringIO()
            with patch.object(av, 'Clip', Clip), patch.object(ui, 'TextLayer', CheckedLayer), patch.object(sys, 'stdout', output), \
                    patch.object(curses, 'getmouse', lambda: app._test_mouse):
                av.run_view(app, primary=True)
            assert Clip.plays == 1
            assert len(conversions) == 1, 'T must open Review before the next T starts conversion'
            assert app.selected_audio_tracks == {0, 1} and app.selected_subtitle_tracks == set()
            assert settings.audio_bitrate == 320
            assert app._av_wipe.enabled and not app._av_wipe.dragging
            assert app._av_wipe.fraction == app._test_expected_fraction
            assert 'Included' in output.getvalue() and 'Excluded' in output.getvalue()
            assert 'Preview error (9 Log)' in output.getvalue()
            assert 'General' in output.getvalue() and 'Video' in output.getvalue() and 'Page ' in output.getvalue() and 'Focus:' in output.getvalue()
            assert 'Presets' in output.getvalue() and 'Vita 960' in output.getvalue()
            clears = output.getvalue().count('\x1b[2J')
            assert clears == 16, f'Expected old images cleared for every edited preview; got {clears}: {app.status}'
            if backend == 'unicode':
                assert Clip.rectangles[-1][0] <= (width - 4) * .75


def geometry_and_review():
    for width in (76, 100, 140, 180, 240):
        for height in (22, 24, 38, 50, 80):
            for tracks in (1, 2, 8, 100):
                layout = ui.workspace_geometry(width, height, tracks)
                commands = ui.preview_control_lines(width - 4)
                assert all(len(line) <= width - 4 for line in commands)
                assert 5 + len(commands) == layout['panel_top']
                assert layout['content_top'] > layout['panel_top'] > 4
                assert layout['timeline_top'] == layout['content_top'] + layout['panel_rows'] + 1
                assert layout['lanes_top'] + layout['visible'] == height - 3
    assert len(ui.preview_control_lines(236)) == 1
    for width in (72, 176, 480):
        slots = ui.timeline_geometry(width, 4)
        assert slots[0][0] == 0 and slots[-1][0] + slots[-1][1] == width - 1
        assert len(slots) >= 6
        assert sum(w for _, w in slots) >= width * .85
    assert len(ui.timeline_geometry(480, 4)) > 6
    cols, rows, pixels, natural = ui.preview_geometry(180, 60, 8, 16, True)
    assert pixels == (960, 544) and natural and cols == 120 and rows == 34
    _, _, small, natural = ui.preview_geometry(40, 10, 8, 16, True)
    assert small[0] <= 320 and small[1] <= 160 and not natural
    assert 'width=960px;height=544px' in graphics.iterm2(b'png', 120, 34, pixels=(960, 544))
    assert 'c=' not in graphics.kitty(b'png', 120, 34, natural=True)
    with tempfile.TemporaryDirectory() as root:
        source = Path(root) / 'film.mkv'
        with source.open('wb') as stream:
            stream.truncate(10_000_000)
        initial = asdict(Settings())
        settings = Settings(quality='compact', max_fps='30')
        info = SimpleNamespace(width=1920, height=1080, fps=Fraction(24), video_codec='hevc',
            duration=10, audio_bit_rates=(384000, 192000))
        metadata = {'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'bit_rate': '8000000'},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'bit_rate': '384000', 'channels': 6, 'sample_rate': '48000'},
            {'codec_type': 'audio', 'codec_name': 'aac', 'bit_rate': '192000'},
            {'codec_type': 'subtitle', 'codec_name': 'srt'}]}
        lines = ui.review_lines(source, settings, info, metadata, [0], [], initial, (192,))
        content = '\n'.join(line for line, _ in lines)
        assert 'Estimated reduction:' in content and 'Output/source:' in content
        assert 'App initial: maximum' in content and 'Now: compact' in content
        assert 'Audio retained: 1/2' in content and 'Subtitle retained: 0/1' in content
        assert '[Exclude]' in content and '[Keep]' in content and '384 kb/s' in content
        assert '48' in content and 'AAC-LC' in content


def contextual_effects():
    info = SimpleNamespace(width=1920, height=800, fps=Fraction(24000, 1001), video_codec='hevc',
                           duration=3600, audio_stream_count=2, audio_bit_rates=(640000, 640000), hdr=False)
    metadata = {'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'bit_rate': '1158000'},
        *[{'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'channel_layout': '5.1(side)',
           'sample_rate': '48000', 'bit_rate': '640000'} for _ in range(2)]]}
    settings = Settings(encoder='x264', quality='high', audio_bitrate=192)
    plan = estimates.output_plan(settings, info, metadata, [0, 1], source_size=1_000_000_000)
    assert plan['target_bps'] == 2_400_000 and plan['max_bps'] == 4_500_000
    assert plan['picture'] == (960, 400)
    assert plan['audio_kbps'] == 384 and all(track['target_kbps'] == 192 for track in plan['tracks'])
    assert plan['estimated_bytes'] == 1_252_800_000
    effects = estimates.parameter_effects(settings, info, plan)
    audio = ' '.join(' '.join(effects['audio_bitrate']).split())
    assert 'kb/s/ch* 106.7 → 96.0' in audio
    assert 'total 384 kb/s' in audio
    assert 'Channels 6 (5.1) → 2 stereo' in audio
    assert 'bits are shared' in audio and 'surround removed' in audio
    quality = ' '.join(effects['quality'])
    assert '2400 target' in quality and 'lossy re-encode' in quality.lower() and 'Encoder: x264' in quality
    assert 'no quality score' in quality
    assert 'inactive (SDR source)' in ' '.join(effects['tone_map'])
    settings.force_hdr = True
    forced = estimates.output_plan(settings, info, metadata, [0])
    assert 'inactive' not in estimates.parameter_effects(settings, info, forced)['tone_map'][0]
    settings.force_hdr = False
    settings.quality = 'compact'
    compact = estimates.output_plan(settings, info, metadata, [0])
    assert compact['target_bps'] < plan['target_bps'] and compact['audio_kbps'] == 192
    settings.quality, settings.content_tune = 'high', 'anime'
    assert estimates.output_plan(settings, info, metadata, [0])['target_bps'] == 2_200_000
    info.fps, settings.max_fps = Fraction(60), '30'
    thirty = estimates.output_plan(settings, info, metadata, [])
    assert thirty['fps'] == 30 and thirty['audio_kbps'] == 0
    assert '50% fewer frames' in ' '.join(estimates.parameter_effects(settings, info, thirty)['max_fps'])
    unknown = SimpleNamespace(**{**info.__dict__, 'duration': None, 'audio_bit_rates': (None, None)})
    missing = estimates.output_plan(settings, unknown, {}, [0])
    assert missing['estimated_bytes'] is None and not missing['tracks'][0]['resolved']
    sample = dict(duration=6, video_bps=1_000_000, audio_bps=[128000], file_bytes=900000)
    measured = estimates.output_plan(settings, info, metadata, [0], audio_rates=(128,), sample=sample)
    assert measured['audio_kbps'] == 128
    assert '1,000 kb/s (6.0s sample)' in ' '.join(estimates.sample_lines(measured))
    assert 'Sample file estimate:' in ' '.join(estimates.sample_lines(measured))
    assert 'Measured A1: 128 kb/s' in ' '.join(estimates.parameter_effects(settings, info, measured)['audio_bitrate'])
    no_fps = SimpleNamespace(duration=None, fps=Fraction(0), audio_stream_count=0)
    fallback = estimates.output_plan(settings, no_fps, {}, [])
    assert 'Source FPS unknown' in ' '.join(estimates.parameter_effects(settings, no_fps, fallback)['max_fps'])
    stats = av.sample_statistics([{'stream_index': 0, 'size': 6000}, {'stream_index': 1, 'size': 600},
        {'stream_index': 2, 'size': 100}, {'size': 'bad'}],
        [{'index': 0, 'codec_type': 'video'}, {'index': 1, 'codec_type': 'audio'}, {'index': 2, 'codec_type': 'subtitle'}], 6, 7000)
    assert stats['video_bps'] == 8000 and stats['audio_bps'] == [800]
    app = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS, _av_effects=effects)
    pages = ui.parameter_pages(app, Settings(), 20, 6)
    for index, (_, attribute, _) in enumerate(app.SETTING_ROWS):
        rendered = ''.join(line for lines, _ in pages for line, kind, field in lines if kind == 'forecast' and field == index).replace(' ', '')
        expected = ''.join(''.join(ui.stable_rows(line, 20, 2)) for line in effects[attribute]).replace(' ', '')
        assert rendered == expected, attribute


def stable_layout():
    info = SimpleNamespace(width=1920, height=800, fps=Fraction(24000, 1001), video_codec='hevc',
        duration=6996.99, audio_stream_count=2, audio_bit_rates=(None, 640000), hdr=False)
    metadata = {'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'bit_rate': '1158000'},
        {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'channel_layout': '5.1(side)'},
        {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'channel_layout': '5.1(side)', 'bit_rate': '640000'}]}
    app = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS, setting_index=0, color=lambda _: 0)
    snapshots = {}
    anchor_rows = None
    for phase in ('pending', 'rates', 'ready', 'changed', 'excluded'):
        settings = Settings(audio_bitrate=128 if phase != 'changed' else 192,
                            quality='compact' if phase == 'changed' else 'high')
        selected = [] if phase == 'excluded' else [0]
        sample = dict(duration=6, video_bps=2400000, audio_bps=[128000], file_bytes=1900000) if phase in ('ready', 'changed') else None
        plan = estimates.output_plan(settings, info, metadata, selected, 1_000_000_000,
            source_audio_rates={0: 640000} if phase != 'pending' else None,
            sample=sample, encoder='videotoolbox' if sample else '')
        app._av_effects = estimates.parameter_effects(settings, info, plan)
        for section in ('general', 'video'):
            for width, height in [(16, 2), (30, 12), (42, 28), (80, 40)]:
                pages = ui.parameter_pages(app, settings, width, height, section)
                positions = [(page, row, kind, index) for page, (lines, _) in enumerate(pages)
                             for row, (_, kind, index) in enumerate(lines)]
                key = section, width, height
                if key in snapshots:
                    assert positions == snapshots[key], (phase, key)
                snapshots[key] = positions
        layer = ui.TextLayer()
        layer.resize(180, 50)
        layer.begin()
        ui.track_panel(app, layer, settings, metadata, selected, [], 'audio', 0, 5, 2, 35, 42,
                       plan, 0, SimpleNamespace(subtitle_lanes=[]))
        rows = [''.join(char for char, _ in row) for row in layer.rows]
        markers = ('Set per stereo track:', 'Channels', 'Track kb/s', 'kb/s/ch*', 'Measured A1:', 'Size A1')
        locations = {marker: next(i for i, row in enumerate(rows) if marker in row) for marker in markers}
        assert anchor_rows is None or locations == anchor_rows, phase
        anchor_rows = locations
    settings = Settings(audio_bitrate=128)
    plan = estimates.output_plan(settings, info, metadata, [0], source_audio_rates={0: 640000})
    audio = ' '.join(' '.join(estimates.audio_summary(plan)).split())
    assert 'Track kb/s 640 → 128 (-80%)' in audio
    assert 'kb/s/ch* 106.7 → 64.0' in audio
    assert 'Channels 6 (5.1) → 2 stereo' in audio
    assert 'surround removed' in audio and 'Audible quality: unmeasured' in audio
    layer = ui.TextLayer()
    layer.resize(180, 25)
    layer.begin()
    ui.navigation(app, layer, 'presets', 4, 180, False)
    menu = ''.join(char for char, _ in layer.rows[4])
    for number, (_, title) in enumerate(ui.SECTIONS, 1):
        assert f'{number} {title}' in menu
    assert 'Shift' not in menu and '[3]' not in menu and '[4]' not in menu
    assert [key for key, _ in ui.SECTIONS][:6] == ['overview', 'presets', 'general', 'video', 'audio', 'subtitles']


def dynamic_audio_limits():
    assert core.AAC_OUTPUT_MAX_KBPS == 576
    assert core.audio_bitrate_limit(192000) == 192
    assert core.audio_bitrate_limit(640000) == 576
    assert core.audio_bitrate_limit() == 576
    assert core.audio_bitrate_choices([95000])[-1] == 95
    assert core.audio_bitrate_choices([192000, 640000])[-1] == 576
    assert core.audio_bitrate_choices([None])[-1] == 576
    info = SimpleNamespace(audio_bit_rates=(640000, 95000))
    assert core.target_audio_bitrates(info, [0, 1], 576) == (576, 95)
    settings = Settings(audio_bitrate=320)
    app = SimpleNamespace(SETTING_ROWS=TerminalApp.SETTING_ROWS, setting_index=4,
        active_settings=lambda: settings, _av_plan={'tracks': [{'source_bps': 640000}]})
    for value in (384, 448, 512, 576, 576):
        TerminalApp.handle_settings_key(app, curses.KEY_RIGHT)
        assert settings.audio_bitrate == value
    app._av_plan = {'tracks': [{'source_bps': 95000}]}
    TerminalApp.handle_settings_key(app, curses.KEY_RIGHT)
    assert settings.audio_bitrate == 576  # Right never lowers a requested ceiling.
    TerminalApp.handle_settings_key(app, curses.KEY_LEFT)
    assert settings.audio_bitrate == 95
    TerminalApp.handle_settings_key(app, curses.KEY_LEFT)
    assert settings.audio_bitrate == 80
    TerminalApp.handle_settings_key(app, curses.KEY_RIGHT)
    assert settings.audio_bitrate == 95


if __name__ == '__main__':
    retained_text()
    pagination()
    geometry_and_review()
    contextual_effects()
    stable_layout()
    dynamic_audio_limits()
    editor_loop()
    print('Passed: UI navigation, protected playback, contextual budgets, multichannel arithmetic, measured samples and unknown values.')
