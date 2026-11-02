"""Retained text around graphics, plus a wrapped, paginated parameter sidebar."""
from __future__ import annotations

import curses
import math
import textwrap
import unicodedata
from dataclasses import asdict

import vmd_ui
import vitamediadeck_transcoder as core
import vmd_estimates as estimates
import vmd_theme as theme


def preview_geometry(width, height, cell_width, cell_height, vita=False):
    factor = min(width * cell_width / 960, height * cell_height / 544)
    if vita:
        factor = min(1, factor)
    pixels = (max(2, int(960 * factor)), max(2, int(544 * factor)))
    columns = max(1, min(width, math.ceil(pixels[0] / cell_width)))
    rows = max(1, min(height, math.ceil(pixels[1] / cell_height)))
    return columns, rows, pixels, vita and factor >= 1


def clock_text(seconds):
    seconds = max(0, int(seconds or 0))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


def progress_line(app, layer, row, left, width, ratio):
    """A dark full-width rail; only encoded video illuminates it."""
    lit = max(0, min(width, int(ratio * width)))
    layer.add(row, left, '━' * width, app.color(12), width)
    if lit:
        layer.add(row, left, '━' * lit, app.color(7), lit)
    if 0 < lit < width:
        layer.add(row, left + lit - 1, '●', app.color(7), 1)


def timeline_geometry(width, rows, cell_width=8, cell_height=16):
    tile_width = max(10, round(rows * cell_height / cell_width * 960 / 544))
    count = max(6, min(64, width // (tile_width + 1)))
    slots = [(i * width // count, (i + 1) * width // count - i * width // count - 1) for i in range(count)]
    return slots


class TextLayer:
    """Never erase native images as a side effect of updating UI text."""
    def __init__(self, base_style=0):
        self.width = self.height = 0
        self.previous = []
        self.clear_pending = True
        self.styles = {}
        self.base_style = base_style

    def resize(self, width, height):
        changed = (width, height) != (self.width, self.height)
        if changed:
            self.width, self.height = width, height
            self.invalidate()
        return changed

    def invalidate(self):
        self.previous = []
        self.clear_pending = True

    def begin(self):
        self.rows = [[(' ', self.base_style)] * max(0, self.width - 1) for _ in range(self.height)]

    def add(self, row, column, value, style=0, width=None):
        if not 0 <= row < self.height or column < 0:
            return
        end = min(self.width - 1, column + (width if width is not None else self.width))
        cursor = column
        for char in str(value).replace('\t', '    '):
            if unicodedata.combining(char) or unicodedata.category(char) in ('Mn', 'Me'):
                if column < cursor < self.width:
                    old, old_style = self.rows[row][cursor - 1]
                    self.rows[row][cursor - 1] = (old + char, old_style)
                continue
            char = char if char.isprintable() else ' '
            cells = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
            if cursor + cells > end:
                break
            self.rows[row][cursor] = (char, style)
            if cells == 2:
                self.rows[row][cursor + 1] = ('', style)
            cursor += cells

    def ansi(self, style):
        if style in self.styles:
            return self.styles[style]
        codes = ['0']
        for mask, code in ((curses.A_BOLD, '1'), (curses.A_DIM, '2'), (curses.A_REVERSE, '7')):
            if style & mask:
                codes.append(code)
        try:
            pair = curses.pair_number(style)
            if pair:
                if theme.truecolor() and pair in theme.PAIRS:
                    fg, bg = theme.PAIRS[pair]
                    codes += ['38;2;' + ';'.join(map(str, fg)), '48;2;' + ';'.join(map(str, bg))]
                else:
                    fg, bg = curses.pair_content(pair)
                    codes.extend([f'38;5;{fg}' if fg >= 0 else '39', f'48;5;{bg}' if bg >= 0 else '49'])
        except (curses.error, ValueError):
            pass
        self.styles[style] = '\x1b[' + ';'.join(codes) + 'm'
        return self.styles[style]

    def present(self, stream, protected=()):
        if self.clear_pending:
            stream.write('\x1b[0m\x1b[2J\x1b[H')
            self.clear_pending = False
        for row, cells in enumerate(self.rows):
            blocked = [False] * len(cells)
            for y, x, width, height in protected:
                if y <= row < y + height:
                    for column in range(max(0, x), min(len(cells), x + width)):
                        blocked[column] = True
            start = 0
            while start < len(cells):
                if blocked[start]:
                    start += 1
                    continue
                end = start + 1
                while end < len(cells) and not blocked[end]:
                    end += 1
                previous = self.previous[row][start:end] if row < len(self.previous) else None
                if cells[start:end] != previous:
                    stream.write(f'\x1b[{row + 1};{start + 1}H')
                    last_style = None
                    for char, style in cells[start:end]:
                        if style != last_style:
                            stream.write(self.ansi(style))
                            last_style = style
                        stream.write(char)
                start = end
        self.previous = self.rows
        stream.write('\x1b[0m')


GROUPS = (
    ('Video', (0, 1, 2, 3)),
    ('Colour / HDR', (6, 7)),
    ('Picture filters', (20, 16, 17, 18, 19)),
    ('Encoding speed', (15,)),
    ('Audio', (4,)),
    ('Performance', (5, 8)),
    ('Artwork', (9, 10)),
    ('Output', (11,)),
    ('Tools', (12, 13, 14)),
)
FIELD_ORDER = [index for _, indices in GROUPS for index in indices]
PARAMETER_SECTIONS = {'general': (5, 8, 9, 10, 11, 12, 13, 14), 'video': (0, 1, 2, 3, 6, 7, 20, 16, 17, 18, 19, 15)}


def wrapped(value, width):
    return textwrap.wrap(str(value), max(1, width), break_long_words=True, break_on_hyphens=False) or ['']


def stable_rows(value, width, count):
    lines = wrapped(value, width)
    if len(lines) > count:
        lines = lines[:count]
        lines[-1] = lines[-1][:max(0, width - 1)] + '…'
    return lines + [''] * max(0, count - len(lines))


def parameter_pages(app, settings, width, height, section=None):
    pages, lines, fields = [], [], []
    last_category = None
    capacity = max(1, height)
    def flush():
        nonlocal lines, fields, last_category
        if lines:
            pages.append((lines, fields))
        lines, fields, last_category = [], [], None
    for category, indices in GROUPS:
        for index in indices:
            if section in PARAMETER_SECTIONS and index not in PARAMETER_SECTIONS[section]:
                continue
            label, attribute, choices = app.SETTING_ROWS[index]
            value = getattr(settings, attribute)
            if isinstance(value, bool):
                value = 'Enabled' if value else 'Disabled'
            if attribute == 'audio_bitrate':
                value = f'{value} kb/s / track'
            elif attribute == 'max_fps':
                value = vmd_ui.fps_label(value)
            elif attribute == 'encoder':
                value = str(value) + (' (CPU)' if value == 'x264' else ' (hardware)' if value != 'auto' else '')
            card = [(part, 'name', index) for part in stable_rows(label, width, 1)]
            card += [(part, 'value', index) for part in stable_rows(f'Set: ‹ {value} ›', width, 1 if width >= 32 else 2)]
            slots = 1 if width >= 32 else 2 if width >= 20 else 3
            for effect in getattr(app, '_av_effects', {}).get(attribute, []):
                card += [(part, 'forecast', index) for part in stable_rows(effect, width, slots)]
            card.append(('', 'space', index))
            header = [(category, 'category', index)]
            if len(header + card) > capacity:
                flush()
                size = max(1, capacity - 1)
                for at in range(0, len(card), size):
                    chunk = (header if capacity > 1 else []) + card[at:at + size]
                    pages.append((chunk, [index]))
                continue
            prefix = header if category != last_category else []
            if len(lines) + len(prefix) + len(card) > capacity:
                flush()
                prefix = header
            lines += prefix + card
            fields.append(index)
            last_category = category
    flush()
    result = pages or [([], [])]
    return result


def sidebar(app, layer, settings, top, left, height, width, focus):
    inner = max(1, width - 2)
    headings = ['↑↓ Select  ←→ Change'] if height >= 7 and inner >= 25 else []
    for row, line in enumerate(headings):
        layer.add(top + row, left, line, app.color(4 if row == 0 else 8), inner)
    pages = parameter_pages(app, settings, inner, max(1, height - len(headings) - 1), focus)
    current = getattr(app, '_parameter_page', 0)
    selected = app.setting_index
    if getattr(app, '_parameter_field', None) != selected or current >= len(pages) or selected not in pages[current][1]:
        current = next((i for i, (_, fields) in enumerate(pages) if selected in fields), 0)
    app._parameter_pages, app._parameter_page = pages, current
    app._parameter_field = selected
    for row, (line, kind, index) in enumerate(pages[current][0]):
        style = app.color(9) if kind in ('name', 'value', 'forecast') else app.color(8)
        if kind == 'category':
            style = app.color(10)
            category_title = line
            line = '─ ' + line + ' ' + '─' * max(0, inner - len(line) - 3)
        if kind == 'original':
            style = app.color(3)
        if index == selected and focus in PARAMETER_SECTIONS and kind in ('name', 'value'):
            style = app.color(2) | curses.A_BOLD
        if kind == 'name' and index == selected:
            line = ('▶ ' if focus in PARAMETER_SECTIONS else '  ') + line
        layer.add(top + len(headings) + row, left, line.ljust(inner) if kind in ('category', 'name', 'value') else line, style, inner)
        if kind == 'category':
            layer.add(top + len(headings) + row, left + 2, category_title, style | curses.A_BOLD, inner - 2)
    layer.add(top + height - 1, left, f'Page {current + 1}/{len(pages)} · PgUp/PgDn', app.color(8), inner)


def next_field(app, step, section=None):
    order = list(PARAMETER_SECTIONS.get(section, FIELD_ORDER))
    at = order.index(app.setting_index) if app.setting_index in order else 0
    app.setting_index = order[(at + step) % len(order)]


def next_page(app, step):
    pages = getattr(app, '_parameter_pages', [])
    if not pages:
        return
    at = max(0, min(len(pages) - 1, app._parameter_page + step))
    app._parameter_page = at
    if pages[at][1]:
        app.setting_index = pages[at][1][0]
        app._parameter_field = app.setting_index


SECTIONS = [('overview', 'Info'), ('presets', 'Presets'), ('general', 'General'), ('video', 'Video'),
            ('audio', 'Audio'), ('subtitles', 'Subtitles'), ('review', 'Review'), ('media', 'Queue'), ('log', 'Log')]


def activate_section(app, old, new):
    remembered = getattr(app, '_section_fields', {})
    if old in PARAMETER_SECTIONS:
        remembered[old] = app.setting_index
    if new in PARAMETER_SECTIONS:
        app.setting_index = remembered.get(new, PARAMETER_SECTIONS[new][0])
    app._section_fields = remembered
    return new


def frame(app, layer, top, left, height, width, title, active=False):
    border = app.color(7 if active else 8)
    heading = app.color(2) if active else app.color(10)
    label = ('▶ ' if active else '') + title
    layer.add(top, left, '╭' + (' ' + label + ' ').ljust(width - 2, '─')[:width - 2] + '╮', heading, width)
    layer.add(top, left + 2, label, heading | curses.A_BOLD, width - 4)
    for row in range(top + 1, top + height - 1):
        layer.add(row, left, '│', border, 1)
        layer.add(row, left + width - 1, '│', border, 1)
    layer.add(top + height - 1, left, '╰' + '─' * (width - 2) + '╯', border, width)


def navigation(app, layer, focus, row, width, directory):
    x = 2
    total = sum(len(label) + 4 for _, label in SECTIONS)
    for number, (key, label) in enumerate(SECTIONS, 1):
        if total > width - 4:
            label = {'presets': 'Prst', 'general': 'Gen', 'video': 'Vid', 'audio': 'Aud', 'subtitles': 'Subs',
                     'review': 'Rev', 'media': 'Que'}.get(key, label)
        title = f' {number} {label} '
        selected = key == focus
        layer.add(row, x, title, app.color(2 if selected else 8) | (curses.A_BOLD if selected else 0), len(title))
        x += len(title)


def preview_control_lines(width):
    commands = ('Space/V Play/Pause', 'Z Fit/Vita', 'J Track', 'L Solo audio',
                '[ ] Sample ±1', '< > ±10', 'G 1–100', 'Home/End', '{ } Audio sample', 'H Filters',
                'C Compare ON/OFF', ', . Divider / mouse drag')
    lines, line = [], ''
    for command in commands:
        if line and len(line) + len(command) + 3 > width:
            lines.append(line)
            line = command
        else:
            line += (' · ' if line else '') + command
    return tuple([*lines, line])


def workspace_geometry(width, height, track_count):
    """Text toolbar and image panels have disjoint, reserved rectangles."""
    control_rows = len(preview_control_lines(width - 4))
    thumb_rows = 5 if height >= 65 else 4 if height >= 38 else 2
    if height < 24 and control_rows > 2:
        thumb_rows = 1
    visible = max(1, min(max(1, track_count), max(2, height // 7), height - thumb_rows - 17 - control_rows))
    panel_rows = max(3, height - visible - thumb_rows - 14 - control_rows)
    panel_top = 5 + control_rows
    content_top = panel_top + 1
    timeline_top = content_top + panel_rows + 1
    return dict(control_rows=control_rows, thumb_rows=thumb_rows, visible=visible,
                panel_rows=panel_rows, panel_top=panel_top, content_top=content_top,
                timeline_top=timeline_top, lanes_top=timeline_top + thumb_rows + 4)


def preview_controls(app, layer, top, left, width, frozen=False):
    locked = {'Space/V Play/Pause': 'Space/V Locked', 'Z Fit/Vita': 'Z Locked',
              'J Track': 'J Wait', 'L Solo audio': 'L Locked', '[ ] Sample ±1': '[ ] Locked',
              '< > ±10': '< > ...', 'G 1–100': 'G Wait', 'Home/End': 'Locked', '{ } Audio sample': '{ } Locked'}
    for row, value in enumerate(preview_control_lines(width)):
        if frozen:
            for command, replacement in locked.items():
                value = value.replace(command, replacement.ljust(len(command)))
        layer.add(top + row, left, value.ljust(width), app.color(10 if row == 0 else 8), width)


def track_panel(app, layer, settings, metadata, audio, subtitles, focus, cursor,
                top, left, height, width, plan, position, clip):
    is_audio = focus == 'audio'
    streams = [s for s in metadata.get('streams', []) if s.get('codec_type') == ('audio' if is_audio else 'subtitle')]
    selected = audio if is_audio else subtitles
    if not streams:
        lines = [('No audio tracks.' if is_audio else 'No subtitle tracks.', 'value')]
    else:
        cursor = min(cursor, len(streams) - 1)
        stream = streams[cursor]
        included = cursor in selected
        label = stream_label(stream, cursor, 'A' if is_audio else 'S')
        lines = [(label, 'heading'), ('Included' if included else 'Excluded', 'value' if included else 'muted')]
        if is_audio:
            lines.append((f'Set per stereo track: {settings.audio_bitrate} kb/s', 'change'))
            lines += [(line, 'value') for line in estimates.audio_summary(plan, cursor)]
            lines += [('+ / - changes the stereo track target.', 'muted')]
        else:
            codec = stream.get('codec_name', '?')
            converted = 'srt' if codec in ('mov_text', 'text', 'webvtt') else codec
            lines += [(f"Format: {codec} → {converted if included else '--'}", 'value'),
                      ('Output: separate subtitle track' if included else 'Output: track omitted', 'value'),
                      (f'Included: {len(subtitles)}/{len(streams)} tracks', 'value'),
                      ('Source file: unchanged', 'muted'),
                      ('Caption at playhead', 'heading')]
            lanes = clip.subtitle_lanes
            caption = cue_at_for_panel(lanes[cursor].cues, position) if cursor < len(lanes) else ''
            lines += [(part, 'value') for part in stable_rows(caption or '—', width, 3)]
        lines += [('Enter: Include / Exclude', 'muted'), ('X: None   E: All', 'muted')]
    paged_lines(app, layer, lines, focus, top, left, height, width, stable=True)


def cue_at_for_panel(cues, position):
    return ' / '.join(text for start, end, text in cues if start <= position < end)


def paged_lines(app, layer, lines, focus, top, left, height, width, stable=False):
    """Page complete wrapped content, never silently cut a source/review field."""
    slots = 1 if width >= 32 else 2 if width >= 20 else 3
    rows = [(part, kind) for value, kind in lines for part in (stable_rows(value, width, slots) if stable else wrapped(value, width))]
    capacity = max(1, height - 2)
    total = max(1, (len(rows) + capacity - 1) // capacity)
    pages = getattr(app, '_av_pages', {})
    current = min(pages.get(focus, 0), total - 1)
    pages[focus] = current
    app._av_pages = pages
    app._av_page_totals = {**getattr(app, '_av_page_totals', {}), focus: total}
    for i, (value, kind) in enumerate(rows[current * capacity:(current + 1) * capacity]):
        style = app.color({'heading': 10, 'value': 9, 'original': 3, 'change': 7, 'muted': 8}.get(kind, 9))
        if kind == 'heading':
            title = value
            value = value.ljust(width, '─')
        layer.add(top + i, left, value, style, width)
        if kind == 'heading':
            layer.add(top + i, left, title, style | curses.A_BOLD, width)
    layer.add(top + height - 1, left, f'Page {current + 1}/{total} · PgUp/PgDn', app.color(8), width)


def move_page(app, focus, step):
    pages = getattr(app, '_av_pages', {})
    total = getattr(app, '_av_page_totals', {}).get(focus, 1)
    pages[focus] = max(0, min(total - 1, pages.get(focus, 0) + step))
    app._av_pages = pages


def rate(value):
    try:
        return f'{float(value) / 1000:.0f} kb/s'
    except (TypeError, ValueError):
        return 'bitrate unknown'


def size(value):
    if value is None:
        return 'unknown'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def audio_description(stream):
    return ' · '.join(str(v) for v in (stream.get('codec_name', '?').upper(),
        rate(core.stream_bit_rate(stream)), stream.get('channel_layout') or f"{stream.get('channels', '?')} ch",
        f"{stream.get('sample_rate', '?')} Hz"))


def stream_label(stream, ordinal, kind):
    tags = stream.get('tags') or {}
    return f"{kind}{ordinal + 1} {display_language(tags.get('language', 'und'))} {tags.get('title') or str(stream.get('codec_name', '')).upper()}"


def display_language(language):
    return {'ita': 'Italian', 'it': 'Italian', 'eng': 'English', 'en': 'English',
            'jpn': 'Japanese', 'ja': 'Japanese', 'fra': 'French', 'fre': 'French', 'fr': 'French',
            'deu': 'German', 'ger': 'German', 'de': 'German', 'spa': 'Spanish', 'es': 'Spanish',
            'und': 'Unspecified', '': 'Unspecified'}.get(language, language)


def source_values(info, metadata, selected_audio):
    streams = metadata.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), {})
    codec = str(video.get('codec_name') or getattr(info, 'video_codec', '?')).upper()
    resolution = f"{getattr(info, 'width', '?')}×{getattr(info, 'height', '?')}"
    colour = ' · '.join(str(video.get(key) or '?') for key in ('color_transfer', 'color_space', 'pix_fmt'))
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    audio = f'{len(audios)} source tracks; {len(selected_audio)} included. Details: Audio (5).'
    return dict(encoder=f'{codec} · {resolution}', quality=rate(core.stream_bit_rate(video)),
        content_tune='encoding tune not stored in the file', max_fps=f"{float(getattr(info, 'fps', 0)):.3f} fps",
        tone_map=colour, force_hdr=colour, hw_decode=codec,
        audio_bitrate=audio if audios else 'no audio tracks')


def overview_lines(source, info, metadata):
    fmt = metadata.get('format', {})
    lines = [('Source file', 'heading'), (source.name, 'value'), (str(source.parent), 'muted'),
             (f'Size: {size(source.stat().st_size)}', 'value'),
             (f"Duration: {float(getattr(info, 'duration', 0) or 0):.2f} s", 'value'),
             (f"Container: {fmt.get('format_long_name') or fmt.get('format_name', '?')}", 'value'),
             (f"Total bitrate: {rate(fmt.get('bit_rate'))}", 'value'), ('', 'muted')]
    counters = {}
    for stream in metadata.get('streams', []):
        kind = stream.get('codec_type', 'unknown')
        ordinal = counters.get(kind, 0)
        counters[kind] = ordinal + 1
        lines.append((f'{kind.title()} {ordinal + 1}', 'heading'))
        if kind == 'video':
            for key in ('codec_name', 'profile', 'width', 'height', 'sample_aspect_ratio', 'display_aspect_ratio',
                        'pix_fmt', 'avg_frame_rate', 'r_frame_rate', 'field_order', 'color_space', 'color_transfer', 'color_primaries'):
                if key in stream:
                    value = vmd_ui.fps_label(stream[key]) if key in ('avg_frame_rate', 'r_frame_rate') else stream[key]
                    lines.append((f"{key.replace('_', ' ').title()}: {value}", 'original'))
            lines.append((rate(core.stream_bit_rate(stream)), 'original'))
        elif kind == 'audio':
            lines += [(f"Codec: {stream.get('codec_name', '?').upper()}", 'original'),
                      (f"Channels: {stream.get('channel_layout') or str(stream.get('channels', '?')) + ' channels'}", 'original'),
                      (f'Bitrate per track: {rate(core.stream_bit_rate(stream))}', 'original'),
                      (f"Sample rate: {stream.get('sample_rate', '?')} Hz", 'original'),
                      ('The bitrate describes the whole audio track, shared by its channels.', 'muted')]
        else:
            lines.append((str(stream.get('codec_name', kind)), 'original'))
        for key, value in (stream.get('tags') or {}).items():
            if key.lower() in ('language', 'title', 'filename', 'mimetype', 'encoder', 'duration'):
                lines.append((f'{key}: {value}', 'muted'))
        flags = [key for key, value in stream.get('disposition', {}).items() if value]
        if flags:
            lines.append(('Flags: ' + ', '.join(flags), 'muted'))
        lines.append(('', 'muted'))
    lines += [('Next steps', 'heading'), ('2 Presets; 3 General; 4 Video; 5 Audio; 6 Subtitles.', 'value'),
              ('7: review output and tracks before T converts.', 'value')]
    return lines


def review_lines(source, settings, info, metadata, audio, subtitles, initial, audio_rates=None, plan=None):
    original = source_values(info, metadata, audio)
    lines = [('Original → Output', 'heading'), (original['encoder'], 'original'),
             ('→ H.264 High · 960×544 canvas', 'value'), ('Original proportions preserved', 'muted')]
    try:
        plan = plan or estimates.output_plan(settings, info, metadata, audio, source.stat().st_size, audio_rates)
        fps, target, maximum = plan['fps'], plan['target_bps'], plan['max_bps']
        estimate = plan['estimated_bytes']
        original_size = source.stat().st_size
        lines += [(f'FPS: {float(info.fps):.3f} → {float(fps):.3f}', 'value'),
                  (f'Video target: {rate(target)} (max {rate(maximum)})', 'value'),
                  ('Size / compression estimate', 'heading'),
                  (f'Original: {size(original_size)}', 'original'), (f'Output estimate: {size(estimate)}', 'value')]
        if estimate is not None and original_size:
            reduction = 100 * (1 - estimate / original_size)
            lines.append((f'Estimated reduction: {reduction:.1f}%' if reduction >= 0 else f'Estimated increase: {-reduction:.1f}%', 'change'))
            lines.append((f'Output/source: {estimate / original_size * 100:.1f}%', 'value'))
        lines.append(('Estimate = duration × target video/audio bitrates. Variable bitrate, subtitles, attachments and container overhead change the final size.', 'muted'))
        lines.extend((line, 'value' if line.startswith('Measured') else 'muted') for line in estimates.sample_lines(plan))
        lines.append(('Audio targets: measured' if audio_rates is not None else 'Audio targets: planned', 'muted'))
    except (AttributeError, ValueError, TypeError, ZeroDivisionError):
        lines.append(('Size estimate unavailable: source duration or format is missing.', 'muted'))
    effects = estimates.parameter_effects(settings, info, plan) if plan is not None else {}
    current = asdict(settings)
    changes = [(key, initial.get(key), value) for key, value in current.items()
               if key not in ('audio_tracks', 'subtitle_tracks') and initial.get(key) != value]
    lines += [('', 'muted'), (f'Settings changed this session ({len(changes)})', 'heading')]
    for key, before, after in changes:
        lines += [(key.replace('_', ' ').title(), 'value')]
        if key in original:
            lines.append(('File original: ' + original[key], 'original'))
        before_label = vmd_ui.fps_label(before) if key == 'max_fps' else before
        after_label = vmd_ui.fps_label(after) if key == 'max_fps' else after
        lines += [(f'App initial: {before_label}', 'muted'), (f'Now: {after_label}', 'change')]
        if plan is not None and key != 'audio_bitrate':
            lines.extend((line, 'value') for line in effects.get(key, []))
    if not changes:
        lines.append(('No app settings changed since opening this media.', 'muted'))
    for kind, selected, code in [('audio', audio, 'A'), ('subtitle', subtitles, 'S')]:
        streams = [s for s in metadata.get('streams', []) if s.get('codec_type') == kind]
        lines += [('', 'muted'), (f'{kind.title()} retained: {len(selected)}/{len(streams)}', 'heading')]
        for i, stream in enumerate(streams):
            included = i in selected
            lines.append((f"[{'Keep' if included else 'Exclude'}] {stream_label(stream, i, code)}", 'value' if included else 'muted'))
            if kind == 'audio':
                lines += [(f"Original codec: {stream.get('codec_name', '?').upper()}", 'original'),
                          (f"Original channels: {stream.get('channel_layout') or str(stream.get('channels', '?')) + ' channels'}", 'original'),
                          (f'Original bitrate per track: {rate(core.stream_bit_rate(stream))}', 'original')]
                source_rate = core.stream_bit_rate(stream)
                target = next((track for track in plan['tracks'] if track['ordinal'] == i), None) if plan else None
                cap = target['target_kbps'] if target else core.audio_bitrate_target(settings.audio_bitrate, source_rate)
                lines += [('Output: AAC-LC stereo, 48000 Hz' if included else 'Output: excluded', 'value'),
                          (f'Output target per track: {cap} kb/s' if included else 'Output target per track: --', 'value')]
            else:
                lines.append((f"{stream.get('codec_name', '?')} · {'retained in output' if included else 'excluded from output'}", 'value' if included else 'muted'))
    if plan is not None:
        lines += [('All included audio', 'heading'), (f"Combined bitrate target: {plan['audio_kbps']} kb/s", 'value'),
                  (f"Audio size estimate: {estimates.byte_size(plan['audio_kbps'] * 1000 * plan['duration'] / 8) if plan['duration'] else 'unknown duration'}", 'value'),
                  ('Each track has its own stereo bitrate. Excluded tracks are omitted and add nothing to this total.', 'muted')]
    lines += [('', 'muted'), ('Artwork / output', 'heading'), (f'Cover: {settings.cover_mode}', 'value'),
              ('T converts using these settings. Source files stay unchanged.', 'muted')]
    return lines


def preset_differences(app, settings, chosen):
    """List actual field changes in the same units as the parameter editor."""
    def value(attribute, setting):
        result = getattr(setting, attribute)
        if attribute == 'max_fps':
            return vmd_ui.fps_label(result)
        if attribute == 'audio_bitrate':
            return f'{result} kb/s per track'
        if isinstance(result, bool):
            return 'On' if result else 'Off'
        return str(result or 'None')
    result = [(f'{label}: {value(attribute, settings)} → {value(attribute, chosen)}', 'change')
              for label, attribute, _ in app.SETTING_ROWS
              if getattr(settings, attribute) != getattr(chosen, attribute)]
    for attribute, label, prefix in (('audio_tracks', 'Audio selection', 'A'), ('subtitle_tracks', 'Subtitle selection', 'S')):
        current, selected = getattr(settings, attribute), getattr(chosen, attribute)
        batch_all = selected is None and current is not None and app.input_path.is_dir()
        if batch_all or selected is not None and current != selected:
            before = 'current selection' if current is None else ', '.join(f'{prefix}{i + 1}' for i in current) or 'none'
            after = 'all source tracks' if selected is None else ', '.join(f'{prefix}{i + 1}' for i in selected) or 'none'
            result.append((f'{label}: {before} → {after}', 'change'))
    return result


def queue_panel(app, layer, top, left, height, width):
    sources = app.batch_sources
    if not sources:
        layer.add(top, left, 'No media in queue.', app.color(8), width)
        return
    selected = max(0, min(app.batch_selection_index, len(sources) - 1))
    controls = [f'Queue {selected + 1}/{len(sources)}', '↑↓ Select · Enter Toggle', '+ / - Reorder · R Reset']
    header = [part for line in controls for part in wrapped(line, width)]
    header = header[:max(0, height - 2)]
    for row, line in enumerate(header):
        layer.add(top + row, left, line, app.color(9 if row == 0 else 8), width)
    capacity = max(1, height - len(header) - 1)
    rows = []
    for index, source in enumerate(sources):
        prefix = ('▶ ' if index == selected else '  ') + ('[x] ' if app.batch_enabled.get(source, True) else '[ ] ')
        parts = wrapped(prefix + str(source.relative_to(app.input_path)), width)
        rows.extend((part, index) for part in parts)
    first = next(row for row, (_, index) in enumerate(rows) if index == selected)
    last = max(row for row, (_, index) in enumerate(rows) if index == selected)
    start = max(0, min(first - capacity // 2, len(rows) - capacity))
    if last >= start + capacity:
        start = last - capacity + 1
    for row, (line, index) in enumerate(rows[start:start + capacity]):
        chosen = index == selected
        layer.add(top + len(header) + row, left, line.ljust(width),
                  app.color(2 if chosen else 9 if app.batch_enabled.get(sources[index], True) else 8), width)
    layer.add(top + height - 1, left, f'{selected + 1}/{len(sources)} · ↑↓ Scroll', app.color(8), width)


def presets_panel(app, layer, settings, top, left, height, width):
    presets = app.presets.rows()
    if not presets:
        layer.add(top, left, 'No presets. N saves the current settings.', app.color(8), width)
        return
    app.preset_index = max(0, min(app.preset_index, len(presets) - 1))
    name, custom, chosen = presets[app.preset_index]
    forecast = []
    differences = preset_differences(app, settings, chosen)
    context = getattr(app, '_av_estimate_context', None)
    if context:
        context = dict(context)
        current = estimates.output_plan(settings, **context)
        if chosen.audio_tracks is not None:
            context['audio'] = [i for i in chosen.audio_tracks if i < context['info'].audio_stream_count]
        elif app.input_path.is_dir():
            context['audio'] = list(range(context['info'].audio_stream_count))
        plan = estimates.output_plan(chosen, **context)
        forecast = [('Estimated result: current → preset', 'heading'),
                    (f"Video budget: {estimates.bitrate(current['target_bps'])} → {estimates.bitrate(plan['target_bps'])}", 'value'),
                    (f"Effective FPS: {float(current['fps']):.3f} → {float(plan['fps']):.3f}", 'value'),
                    (f"All audio: {current['audio_kbps']} → {plan['audio_kbps']} kb/s", 'value'),
                    (f"File budget: {estimates.byte_size(current['estimated_bytes'])} → {estimates.byte_size(plan['estimated_bytes'])}", 'value'),
                    ('Budgets are estimates; actual size depends on the scenes.', 'muted')]
    details = [(name, 'heading'),
               ('No setting changes' if not differences else f'{len(differences)} changes · current → preset', 'heading'),
               *(differences or [('Matches the current configuration.', 'value')]), *forecast,
               ('Recommended use', 'heading'), (vmd_ui.preset_hint(name), 'value'),
               ('Enter applies; browsing leaves the current preview unchanged.', 'muted')]
    if height < 12:
        paged_lines(app, layer, [(f'▶ {app.preset_index + 1}/{len(presets)} {name}', 'change'),
                    ('↑↓ Browse · Enter Apply · PgUp/Dn Details', 'value')] + details,
                    'presets', top, left, height, width)
        return
    layer.add(top, left, '↑↓ Browse · Enter Apply', app.color(9), width)
    layer.add(top + 1, left, 'PgUp/Dn Details · N Save · D Delete', app.color(8), width)
    # Reserve a stable third for paged details; let the list use the rest.
    # Selection changes never move the list/details boundary.
    list_rows = max(1, min(len(presets), max(2, (height - 4) * 2 // 3)))
    start = max(0, min(app.preset_index - list_rows // 2, len(presets) - list_rows))
    for row, (label, is_custom, _) in enumerate(presets[start:start + list_rows]):
        selected = start + row == app.preset_index
        marker = '▶ ' if selected else '  '
        label = marker + label + (' *' if is_custom else '')
        layer.add(top + 3 + row, left, label.ljust(width), app.color(2 if selected else 9), width)
    details_top = top + 4 + list_rows
    details_height = height - (details_top - top)
    if details_height > 2:
        paged_lines(app, layer, details, 'presets', details_top, left, details_height, width)
