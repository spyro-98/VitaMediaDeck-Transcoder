#!/usr/bin/env python3
"""Real source/output wipes, mouse dragging, retained position and fresh rendering."""
from dataclasses import asdict, replace
import curses
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
from vitamediadeck_tui import Settings
from vmd_av_preview import Clip, png
from vmd_comparison import WipeState, MouseInput, ComparisonFrames, RGBStore, png_rgb, wipe_rgb
import vmd_workflow as workflow


def mouse_child(report):
    def run(screen):
        state, events = WipeState(), []
        screen.keypad(True)
        mouse = MouseInput(curses, sys.stdout)
        screen.addstr(0, 0, 'COMPARISON_MOUSE_READY')
        screen.refresh()
        try:
            while True:
                key = screen.getch()
                key, mouse_event = mouse.decode(screen, key)
                if key == ord('q'):
                    break
                if key == curses.KEY_MOUSE:
                    event = mouse_event if mouse_event is not None else curses.getmouse()
                    events.append(event)
                    state.mouse(event[1], event[2], event[4], (3, 10, 501, 20), curses)
                else:
                    state.key(key)
            Path(report).write_text(json.dumps(dict(available=mouse.available, fraction=state.fraction,
                                                   enabled=state.enabled, events=events)))
        finally:
            mouse.close()
    curses.wrapper(run)


def mouse_pty(root):
    if os.name == 'nt':
        return 'PTY unavailable on this platform; mouse routing checked directly'
    import fcntl
    import pty
    import select
    import struct
    import termios
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 560, 0, 0))
    report = root / 'mouse-pty.json'
    process = subprocess.Popen([sys.executable, __file__, '--mouse-child', str(report)],
        stdin=slave, stdout=slave, stderr=slave, env={**os.environ, 'TERM': 'xterm-256color'})
    os.close(slave)
    output, deadline = bytearray(), time.monotonic() + 5
    try:
        while b'COMPARISON_MOUSE_READY' not in output and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                output.extend(os.read(master, 65536))
        assert b'COMPARISON_MOUSE_READY' in output, output.decode(errors='replace')
        os.write(master, b'\x1b[<0;136;5M')  # Press at 25%.
        time.sleep(.05)
        os.write(master, b'\x1b[<32;386;5M')  # Drag to 75%, beyond legacy X10 coordinates.
        time.sleep(.05)
        os.write(master, b'\x1b[<0;386;5mccq')  # Release, disable/enable, quit.
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        process.wait(timeout=3)
        assert process.returncode == 0, output.decode(errors='replace')
        data = json.loads(report.read_text())
        assert data['available'] and data['enabled'] and data['fraction'] == .75, data
        assert len(data['events']) >= 2, data
        assert b'?1002h' in output and b'?1002l' in output
        return 'Real ncurses SGR press/drag/release in a PTY; toggle retained 75%'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def wait_ready(value):
    deadline = time.monotonic() + 50
    while not value.ready() and not value.error and time.monotonic() < deadline:
        time.sleep(.01)
    assert value.ready(), value.error or 'Comparison timed out'


def controls():
    state = WipeState()
    assert state.enabled and state.fraction == .5
    rectangle = (3, 10, 101, 20)
    assert not state.mouse(8, 4, curses.BUTTON1_PRESSED, rectangle, curses)
    assert state.mouse(35, 4, curses.BUTTON1_PRESSED, rectangle, curses)
    assert state.fraction == .25 and state.dragging
    assert state.mouse(85, 4, curses.REPORT_MOUSE_POSITION, rectangle, curses)
    assert state.fraction == .75
    assert state.mouse(999, -10, curses.REPORT_MOUSE_POSITION, rectangle, curses)
    assert state.fraction == 1
    assert state.mouse(-10, -10, curses.BUTTON1_RELEASED, rectangle, curses)
    assert state.fraction == 0 and not state.dragging
    assert not state.mouse(85, 4, curses.REPORT_MOUSE_POSITION, rectangle, curses)
    assert state.mouse(47, 4, curses.BUTTON1_CLICKED, rectangle, curses)
    assert state.fraction == .37
    state.key(ord('c'))
    assert not state.enabled and state.fraction == .37
    assert not state.mouse(35, 4, curses.BUTTON1_CLICKED, rectangle, curses)
    state.key(ord('C'))
    assert state.enabled and state.fraction == .37
    state.key(ord('.'))
    assert abs(state.fraction - .39) < 1e-9
    state.key(ord(','))
    assert abs(state.fraction - .37) < 1e-9
    assert not state.key(ord('+'))  # Parameter keys still belong to the editor.
    class MouseCurses:
        ALL_MOUSE_EVENTS = 3
        REPORT_MOUSE_POSITION = 4
        error = curses.error
        calls = []
        @classmethod
        def mousemask(cls, mask):
            cls.calls.append(mask)
            return mask, 1
        @classmethod
        def mouseinterval(cls, interval):
            cls.calls.append(('interval', interval))
    stream = io.StringIO()
    mouse = MouseInput(MouseCurses, stream)
    assert mouse.available
    assert mouse.call(lambda: 123) == 123
    mouse.close()
    assert MouseCurses.calls == [7, ('interval', 0), 0, 7, 1, ('interval', 200)]
    assert all(value in stream.getvalue() for value in ('?1002h', '?1006h', '?1002l', '?1006l'))


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    controls()
    pty_result = mouse_pty(root)
    ffmpeg, _, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    source = root / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=320x240:rate=24:duration=2', '-c:v', 'libx264', '-preset', 'ultrafast', str(source)], check=True)
    untouched = hashlib.sha256(source.read_bytes()).hexdigest()
    settings = Settings(encoder='x264', filter_backend='cpu', scaler='lanczos', hw_decode=False,
                        quality='compact', denoise='strong', sharpen='medium')
    state, clips, comparisons, results = WipeState(fraction=.37), [], [], []
    try:
        for backend, width, height in [('iterm2', 960, 544), ('kitty', 192, 108),
                                       ('unicode', 48, 28), ('sixel', 96, 54)]:
            clip = Clip(source, asdict(settings), None, [], [], width, height, backend)
            clips.append(clip)
            wait_ready(clip)
            encoded_before = dict(clip.work)
            comparison = ComparisonFrames(clip)
            comparisons.append(comparison)
            wait_ready(comparison)
            original = comparison.original.frame(0)
            processed = (png_rgb(clip.frames[0], width, height) if comparison.native else comparison.processed.frame(0))
            assert original != processed, 'Fixture did not produce visible processing differences'
            for fraction in (0, .25, .37, .5, 1):
                mixed = wipe_rgb(original, processed, width, height, fraction)
                if fraction == 0:
                    assert mixed == processed
                elif fraction == 1:
                    assert mixed == original
                else:
                    split, row = round(fraction * width), height // 4
                    assert mixed[row * width * 3:row * width * 3 + 3] == original[row * width * 3:row * width * 3 + 3]
                    assert mixed[(row * width + width - 1) * 3:(row * width + width) * 3] == processed[(row * width + width - 1) * 3:(row * width + width) * 3]
                payload = comparison.frame(0, fraction)
                if comparison.native:
                    assert png_rgb(payload, width, height) == mixed
                elif backend == 'unicode':
                    assert '▀' in payload
                else:
                    assert payload.startswith('\x1bPq')
            measured = time.monotonic()
            for index in range(min(12, len(clip.frames))):
                comparison.frame(index, state.fraction)
            elapsed = time.monotonic() - measured
            assert not hasattr(comparison, 'memo') and not hasattr(comparison.original, 'memo')
            assert dict(clip.work) == encoded_before  # Wipe never re-encodes media.
            assert not clip.playing and clip.clock is None
            results.append(dict(backend=backend, original_frames=len(comparison.original.frames),
                                processed_frames=len(clip.frames), render_ms=elapsed * 1000 / min(12, len(clip.frames)),
                                split='pixel checked', mouse='drag/click/release checked'))
            if backend == 'iterm2':
                before = state.fraction
                updated = Clip(source, asdict(replace(settings, quality='maximum')), None, [], [], width, height,
                               backend, previous=clip)
                clips.append(updated)
                wait_ready(updated)
                refreshed = ComparisonFrames(updated)
                comparisons.append(refreshed)
                wait_ready(refreshed)
                assert state.fraction == before and refreshed.key != comparison.key
                assert refreshed.original.filename != comparison.original.filename
                assert refreshed.original.frame(0) == original
                assert png_rgb(refreshed.frame(0, state.fraction), width, height)[0:3] == original[0:3]
                state.key(ord('c'))
                state.key(ord('c'))
                assert state.fraction == before
        assert hashlib.sha256(source.read_bytes()).hexdigest() == untouched
        # Internal PNG round trips stay exact for the preview's RGB format.
        rgb = bytes(range(120))
        assert png_rgb(png(rgb, 8, 5), 8, 5) == rgb
        assert not (root / 'cache').exists()
        workflow.atomic_json(root / 'validation.json', dict(backends=results, position=state.fraction,
            terminal_mouse=pty_result,
            source='unchanged', original_render='decoded again after every parameter change',
            memory='temporary playback buffers only; no cross-preview reuse',
            physical_terminal='mouse event routing and layouts checked; terminal-specific drag presentation still needs live QA'))
        print(root / 'validation.json')
    finally:
        for comparison in comparisons:
            comparison.close()
        for clip in clips:
            clip.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--mouse-child':
        mouse_child(sys.argv[2])
    else:
        main()
