"""Terminal adaptation of VitaMediaDeck's Legacy Vita palette and menu surfaces.

Source: VitaMediaDeck/src/ui/theme.c (VT_UI_THEME_LEGACY_VITA) and
src/ui/components.c (ui_menu_surface, ui_menu_focus_draw).
Sony PAF widgets remain console-only; these are terminal components.
"""
import os

BG = (5, 24, 53)
SURFACE = (9, 43, 79)
RAISED = (18, 62, 104)
SELECTION = (28, 92, 150)
FOCUS = (155, 220, 255)
TEXT = (234, 245, 240)
MUTED = (181, 200, 194)
FAINT = (86, 114, 143)
PAIRS = {
    1: (FOCUS, BG), 2: (TEXT, SELECTION), 3: ((219, 239, 243), BG),
    4: ((165, 225, 255), BG), 5: ((255, 104, 116), BG), 6: (TEXT, SELECTION),
    7: (FOCUS, BG), 8: (MUTED, BG), 9: (TEXT, BG), 10: (TEXT, RAISED),
    11: ((231, 177, 107), BG), 12: (FAINT, BG), 13: (FOCUS, BG),
    14: ((63, 160, 225), BG), 15: (FAINT, SURFACE), 16: (TEXT, SURFACE),
}


def xterm_rgb(index):
    if index >= 232:
        return (8 + (index - 232) * 10,) * 3
    index -= 16
    values = (0, 95, 135, 175, 215, 255)
    return values[index // 36], values[index // 6 % 6], values[index % 6]


def nearest(rgb):
    candidates = range(16, 256)
    if rgb[2] - rgb[0] >= 20 and rgb[2] - rgb[1] >= 20:
        # Do not quantize Vita's navy canvas to an equally close neutral grey.
        candidates = [i for i in range(16, 232) if xterm_rgb(i)[2] > max(xterm_rgb(i)[:2])]
    return min(candidates, key=lambda index: sum((a - b) ** 2 for a, b in zip(rgb, xterm_rgb(index))))


def configure(curses):
    for pair, (foreground, background) in PAIRS.items():
        if curses.COLORS >= 256:
            curses.init_pair(pair, nearest(foreground), nearest(background))
        else:
            foreground = curses.COLOR_RED if pair == 5 else curses.COLOR_CYAN if pair in (1, 7, 13, 14) else curses.COLOR_WHITE
            background = curses.COLOR_BLUE if pair not in (2, 6) else curses.COLOR_CYAN
            if pair in (2, 6):
                foreground = curses.COLOR_BLACK
            curses.init_pair(pair, foreground, background)


def truecolor():
    return os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit') or os.environ.get('TERM_PROGRAM') in ('iTerm.app', 'WezTerm', 'vscode') or 'kitty' in os.environ.get('TERM', '')
