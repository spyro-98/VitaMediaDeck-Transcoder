"""Dependency-free terminal image backends; FFmpeg supplies PNG and RGB pixels."""
from __future__ import annotations

import base64
import os
import select
import sys
import time
import re
import struct


def cell_size():
    """Return measured character pixels when available, otherwise an estimate."""
    try:
        import fcntl
        import termios
        rows, columns, width, height = struct.unpack('HHHH', fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\0' * 8))
        if width and height and rows and columns:
            return width / columns, height / rows, True
    except (ImportError, OSError, ValueError):
        pass
    cached = os.environ.get('VMD_CELL_PIXELS', '')
    try:
        width, height = map(float, cached.split(','))
        if width > 0 and height > 0:
            return width, height, True
    except ValueError:
        pass
    return 8.0, 16.0, False


def detect_cell_size():
    """Bounded xterm CSI 16 t query, before curses owns input."""
    width, height, measured = cell_size()
    if measured or os.name == 'nt' or not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    if os.environ.get('TMUX') or os.environ.get('STY'):
        return
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    reply = b''
    try:
        tty.setcbreak(fd)
        sys.stdout.write('\x1b[16t')
        sys.stdout.flush()
        deadline = time.monotonic() + .15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            reply += os.read(fd, 128)
            match = re.search(rb'\x1b\[6;(\d+);(\d+)t', reply)
            if match:
                height, width = map(int, match.groups())
                if width and height:
                    os.environ['VMD_CELL_PIXELS'] = f'{width},{height}'
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def detect_backend() -> str:
    override = os.environ.get("VMD_PREVIEW", "auto").lower()
    if override in {"kitty", "iterm2", "sixel", "unicode"}:
        return override
    # Multiplexers can eat graphics sequences; use a predictable fallback.
    if os.environ.get("TMUX") or os.environ.get("STY"):
        return "unicode"
    term = os.environ.get("TERM", "").lower()
    program = os.environ.get("TERM_PROGRAM", "").lower()
    if os.environ.get("KITTY_WINDOW_ID") or "kitty" in term or program == "ghostty":
        return "kitty"
    if program in {"iterm.app", "wezterm"} or os.environ.get("ITERM_SESSION_ID"):
        return "iterm2"
    if "sixel" in term:
        return "sixel"
    # DA1 advertises Sixel with parameter 4. Probe only before curses starts,
    # and restore the terminal even on timeout/unsupported platforms.
    if os.name != "nt" and sys.stdin.isatty() and sys.stdout.isatty():
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        reply = b""
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b[c")
            sys.stdout.flush()
            deadline = time.monotonic() + 0.15
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                reply += os.read(fd, 64)
                if b"c" in reply:
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if reply.startswith(b"\x1b[?") and reply.endswith(b"c"):
            if b"4" in reply[3:-1].split(b";"):
                return "sixel"
    return "unicode"


def kitty(png: bytes, columns: int, rows: int, image_id: int = 31415, natural=False) -> str:
    encoded = base64.b64encode(png).decode("ascii")
    packets = []
    for start in range(0, len(encoded), 4096):
        more = int(start + 4096 < len(encoded))
        geometry = '' if natural else f'c={columns},r={rows},'
        prefix = f"a=T,f=100,t=d,i={image_id},{geometry}q=2," if start == 0 else ""
        packets.append(f"\x1b_G{prefix}m={more};{encoded[start:start + 4096]}\x1b\\")
    return "".join(packets)


def iterm2(png: bytes, columns: int, rows: int, pixels=None) -> str:
    width, height = (f'{pixels[0]}px', f'{pixels[1]}px') if pixels else (columns, rows)
    return (f"\x1b]1337;File=inline=1;size={len(png)};width={width};height={height};"
            f"preserveAspectRatio=1:{base64.b64encode(png).decode('ascii')}\a")


def sixel(rgb: bytes, width: int, height: int) -> str:
    # Fixed 216-colour RGB cube. One band is six vertical pixels.
    palette = [f"#{i};2;{r * 20};{g * 20};{b * 20}"
               for i, (r, g, b) in enumerate((r, g, b) for r in range(6)
                                             for g in range(6) for b in range(6))]
    pixels = [(rgb[i] * 5 // 255) * 36 + (rgb[i + 1] * 5 // 255) * 6
              + rgb[i + 2] * 5 // 255 for i in range(0, len(rgb), 3)]
    bands = []
    for y in range(0, height, 6):
        colours = set(pixels[y * width:min(y + 6, height) * width])
        lines = []
        for colour in sorted(colours):
            data = []
            for x in range(width):
                mask = sum(1 << dy for dy in range(min(6, height - y))
                           if pixels[(y + dy) * width + x] == colour)
                data.append(chr(63 + mask))
            lines.append(f"#{colour}" + "".join(data))
        bands.append("$".join(lines))
    return f'\x1bPq"1;1;{width};{height}' + "".join(palette) + "-".join(bands) + "\x1b\\"


def unicode(rgb: bytes, width: int, height: int) -> str:
    lines = []
    for y in range(0, height - 1, 2):
        line = []
        for x in range(width):
            a = (y * width + x) * 3
            b = ((y + 1) * width + x) * 3
            top, bottom = rgb[a:a + 3], rgb[b:b + 3]
            line.append(f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m"
                        f"\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m▀")
        lines.append("".join(line) + "\x1b[0m")
    return "\r\n".join(lines)


def clear(backend: str) -> str:
    delete = "\x1b_Ga=d,d=I,i=31415,q=2\x1b\\" if backend == "kitty" else ""
    return delete + "\x1b[0m\x1b[2J\x1b[H"
