"""Original/converted wipe, independent of conversion settings and playback."""
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib

import vitamediadeck_transcoder as core
import vmd_preview as graphics


@dataclass
class WipeState:
    enabled: bool = True
    fraction: float = .5
    dragging: bool = False

    def move(self, value):
        self.fraction = max(0., min(1., value))

    def key(self, key):
        if key in (ord('c'), ord('C')):
            self.enabled = not self.enabled
            self.dragging = False
        elif key in (ord(','), ord('.')):
            self.move(self.fraction + (-.02 if key == ord(',') else .02))
        else:
            return False
        return True

    def mouse(self, x, y, buttons, rectangle, curses):
        if not self.enabled:
            return False
        top, left, width, height = rectangle
        inside = left <= x < left + width and top <= y < top + height
        pressed = buttons & getattr(curses, 'BUTTON1_PRESSED', 0)
        released = buttons & getattr(curses, 'BUTTON1_RELEASED', 0)
        clicked = buttons & (getattr(curses, 'BUTTON1_CLICKED', 0) |
                             getattr(curses, 'BUTTON1_DOUBLE_CLICKED', 0))
        moved = buttons & getattr(curses, 'REPORT_MOUSE_POSITION', 0)
        if pressed and inside:
            self.dragging = True
        accepted = bool((clicked and inside) or self.dragging and (pressed or moved or released))
        if accepted:
            self.move((x - left) / max(1, width - 1))
        if released or clicked:
            self.dragging = False
        return accepted


class MouseInput:
    """Enable button-drag reporting only while the preview owns the terminal."""
    def __init__(self, curses, stream):
        self.curses, self.stream, self.previous = curses, stream, None
        self.pending, self.pending_at = '', 0
        try:
            self.mask = curses.ALL_MOUSE_EVENTS | getattr(curses, 'REPORT_MOUSE_POSITION', 0)
            available, self.previous = curses.mousemask(self.mask)
            self.available = bool(available)
            if self.available:
                curses.mouseinterval(0)
                stream.write('\x1b[?1002h\x1b[?1006h')
                stream.flush()
        except (AttributeError, curses.error):
            self.available = False

    def decode(self, screen, key):
        """Older macOS ncurses advertises X10 only; parse SGR without its help.

        SGR keeps mouse coordinates correct beyond X10's 223-column limit.
        Native ncurses KEY_MOUSE events continue through the regular path.
        """
        if not self.available or not self.pending and key != 27:
            return key, None
        if not self.pending:
            self.pending, self.pending_at = '\x1b', time.monotonic()
        elif 0 <= key < 256:
            self.pending += chr(key)
        try:
            while True:
                match = re.fullmatch(r'\x1b\[<(\d+);(\d+);(\d+)([Mm])', self.pending)
                if match:
                    code, x, y = map(int, match.groups()[:3])
                    release = match[4] == 'm'
                    self.pending = ''
                    if code & 64:
                        buttons = getattr(self.curses, 'BUTTON4_PRESSED' if not code & 1 else 'BUTTON5_PRESSED', 0)
                    elif code & 32:
                        buttons = getattr(self.curses, 'REPORT_MOUSE_POSITION', 0)
                    else:
                        number = (code & 3) + 1
                        buttons = getattr(self.curses, f'BUTTON{number}_{"RELEASED" if release else "PRESSED"}', 0)
                    return self.curses.KEY_MOUSE, (0, x - 1, y - 1, 0, buttons)
                if (not '\x1b[<'.startswith(self.pending) and
                        not re.fullmatch(r'\x1b\[<[\d;]*', self.pending)) or len(self.pending) > 64:
                    break
                screen.timeout(8 if len(self.pending) == 1 else 0)
                next_key = screen.getch()
                if next_key == -1:
                    if len(self.pending) > 1 and time.monotonic() - self.pending_at < .1:
                        return -1, None
                    break
                if not 0 <= next_key < 256:
                    self.curses.ungetch(next_key)
                    break
                self.pending += chr(next_key)
            # Preserve ordinary Escape/Alt input that was not a mouse report.
            for character in reversed(self.pending[1:]):
                self.curses.ungetch(ord(character))
            self.pending = ''
            return 27, None
        finally:
            screen.timeout(16)

    def call(self, callback, *args, **kwargs):
        """Dialogs own normal terminal input; do not feed them raw mouse reports."""
        if not self.available:
            return callback(*args, **kwargs)
        self.stream.write('\x1b[?1002l\x1b[?1006l')
        self.stream.flush()
        self.curses.mousemask(0)
        try:
            return callback(*args, **kwargs)
        finally:
            self.curses.mousemask(self.mask)
            self.stream.write('\x1b[?1002h\x1b[?1006h')
            self.stream.flush()

    def close(self):
        if self.available:
            self.stream.write('\x1b[?1002l\x1b[?1006l')
            self.stream.flush()
        if self.previous is not None:
            try:
                self.curses.mousemask(self.previous)
                self.curses.mouseinterval(200)
            except self.curses.error:
                pass


class RGBStore:
    """Temporary playback frames owned by one freshly built preview."""
    MAGIC = b'VMDPIX1\n'

    def __init__(self, filename):
        self.filename, self.frames = Path(filename), []
        with self.filename.open('rb') as stream:
            if stream.read(8) != self.MAGIC:
                raise ValueError('Invalid comparison data.')
            self.width, self.height, count = struct.unpack('>III', stream.read(12))
            if not 1 <= self.width <= 1920 or not 1 <= self.height <= 1080 or not 1 <= count <= 600:
                raise ValueError('Invalid comparison frame geometry.')
            end = self.filename.stat().st_size
            for _ in range(count):
                length = struct.unpack('>I', stream.read(4))[0]
                if not 0 < length <= self.width * self.height * 6 + 1024 or stream.tell() + length > end:
                    raise ValueError('Incomplete comparison data.')
                self.frames.append((stream.tell(), length))
                stream.seek(length, 1)
            if stream.tell() != end:
                raise ValueError('Invalid comparison frame index.')

    @classmethod
    def write(cls, filename, width, height, frames):
        count = 0
        with Path(filename).open('wb') as stream:
            stream.write(cls.MAGIC + struct.pack('>III', width, height, 0))
            for rgb in frames:
                if len(rgb) != width * height * 3:
                    raise ValueError('Incomplete comparison frame.')
                packed = zlib.compress(rgb, 1)
                stream.write(struct.pack('>I', len(packed)) + packed)
                count += 1
            stream.seek(8)
            stream.write(struct.pack('>III', width, height, count))
        return cls(filename)

    def frame(self, index):
        index = max(0, min(index, len(self.frames) - 1))
        offset, size = self.frames[index]
        with self.filename.open('rb') as stream:
            stream.seek(offset)
            decoder = zlib.decompressobj()
            rgb = decoder.decompress(stream.read(size), self.width * self.height * 3 + 1)
        if len(rgb) != self.width * self.height * 3 or not decoder.eof:
            raise ValueError('Damaged comparison frame.')
        return rgb


def png_rgb(payload, width, height):
    """Read only the RGB/filter-zero PNG frames produced by our own renderer."""
    if payload[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Invalid converted comparison image.')
    at, packed = 8, bytearray()
    while at + 12 <= len(payload):
        size = struct.unpack('>I', payload[at:at + 4])[0]
        name, data = payload[at + 4:at + 8], payload[at + 8:at + 8 + size]
        if name == b'IHDR' and data != struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0):
            raise ValueError('Comparison image dimensions changed.')
        if name == b'IDAT':
            packed.extend(data)
        at += size + 12
    stride = width * 3
    decoder = zlib.decompressobj()
    raw = decoder.decompress(packed, (stride + 1) * height + 1)
    if len(raw) != (stride + 1) * height or not decoder.eof:
        raise ValueError('Incomplete converted comparison image.')
    if any(raw[y * (stride + 1)] != 0 for y in range(height)):
        raise ValueError('Unsupported internal comparison image filter.')
    return b''.join(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)] for y in range(height))


def wipe_rgb(original, processed, width, height, fraction):
    split = max(0, min(width, round(fraction * width)))
    if split == 0:
        return processed
    if split == width:
        return original
    stride, result = width * 3, bytearray(processed)
    for row in range(height):
        begin = row * stride
        result[begin:begin + split * 3] = original[begin:begin + split * 3]
    # Divider and grip are part of the image, so graphics cannot cover them.
    for row in range(height):
        at = row * stride + split * 3
        result[at:at + 3] = bytes((185, 230, 255))
    if width >= 40 and height >= 20:
        half, tall = max(2, min(6, width // 80)), max(4, min(12, height // 20))
        for row in range(height // 2 - tall, height // 2 + tall):
            begin, end = max(0, split - half), min(width, split + half + 1)
            shade = (185, 230, 255) if row % 4 == 0 else (16, 80, 130)
            result[row * stride + begin * 3:row * stride + end * 3] = bytes(shade) * (end - begin)
    return bytes(result)


def clip_key(clip):
    if not clip.ready() or not getattr(clip, 'video_key', None):
        return None
    return (clip.video_key, clip.width, clip.height, clip.backend, clip.frame_fps)


class ComparisonFrames:
    """Rebuild source frames for each preview; moving the divider only composes pixels."""
    def __init__(self, clip):
        self.key = clip_key(clip)
        self.width, self.height, self.backend = clip.width, clip.height, clip.backend
        self.native = self.backend in ('kitty', 'iterm2')
        self.processed_png = tuple(clip.frames) if self.native else ()
        self.process, self.error = None, ''
        self.lock, self.closed, self.complete = threading.Lock(), threading.Event(), threading.Event()
        self.original, self.processed = None, None
        self.directory = tempfile.TemporaryDirectory(prefix='vmd-compare-')
        self.root = Path(self.directory.name)
        if not self.native:
            shutil.copyfile(clip.path, self.root / 'processed.mkv')
        self.ffmpeg = clip.ffmpeg
        self.thread_limit = core.resource_thread_limit(clip.settings.get('system_load', 'balanced'))
        self.source, self.start, self.duration, self.fps = clip.source, clip.start, clip.duration, clip.frame_fps
        self.source_signature = clip.source_signature
        self.info, self.metadata, self.caps = clip.info, clip.metadata, clip.capabilities
        self.thread = threading.Thread(target=self._build, daemon=True)
        self.thread.start()

    def _decode(self, arguments):
        with tempfile.TemporaryFile() as errors:
            with self.lock:
                if self.closed.is_set():
                    raise ValueError('Comparison cancelled.')
                limits = ['-threads', str(self.thread_limit), '-filter_threads', str(self.thread_limit)] if self.thread_limit else []
                process = subprocess.Popen([self.ffmpeg, '-v', 'error', *limits, *arguments, '-t', str(self.duration),
                    '-an', '-sn', '-dn', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                    stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL)
                self.process = process
            try:
                while not self.closed.is_set():
                    frame = process.stdout.read(self.width * self.height * 3)
                    if not frame:
                        break
                    yield frame
                code = process.wait()
                if code:
                    errors.seek(0)
                    raise ValueError(errors.read(2000).decode(errors='replace') or 'Cannot read comparison frames.')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()

    def _build(self):
        try:
            if self.original is None:
                path = self.root / 'original.rgbz'
                video = next((stream for stream in self.metadata.get('streams', [])
                              if stream.get('codec_type') == 'video' and not stream.get('disposition', {}).get('attached_pic')), {})
                # HDR needs fixed display normalization for an SDR terminal;
                # do not apply the user's tone-map or enhancement choices.
                info = replace(self.info, hdr=video.get('color_transfer') in core.HDR_TRANSFERS, interlaced=False)
                fps = Fraction(str(self.fps)).limit_denominator(100000)
                options = {**core.VIDEO_OPTION_DEFAULTS, 'filter_backend': 'cpu', 'scaler': 'lanczos',
                           'denoise': 'off', 'sharpen': 'off', 'deband': 'off'}
                filters = core.filter_chain(info, 960, 544, fps, 'x264', self.caps, 'mobius', video_options=options)
                filters += f',scale={self.width}:{self.height}:flags=lanczos'
                self.original = RGBStore.write(path, self.width, self.height, self._decode([
                    '-ss', f'{self.start:.9f}', '-i', str(self.source), '-map', f'0:{self.info.video_stream_index}', '-vf', filters]))
            if not self.native:
                self.processed = RGBStore.write(self.root / 'processed.rgbz', self.width, self.height,
                    self._decode(['-i', str(self.root / 'processed.mkv'), '-map', '0:v:0', '-vf',
                                  f'fps={self.fps},scale={self.width}:{self.height}:flags=lanczos']))
            if not self.closed.is_set():
                self.complete.set()
        except Exception as exc:
            if not self.closed.is_set():
                self.error = str(exc)
        finally:
            if self.closed.is_set():
                self.directory.cleanup()

    def ready(self):
        return self.complete.is_set() and not self.closed.is_set()

    def frame(self, index, fraction):
        if self.native:
            processed = png_rgb(self.processed_png[min(index, len(self.processed_png) - 1)], self.width, self.height)
        else:
            processed = self.processed.frame(index)
        rgb = wipe_rgb(self.original.frame(index), processed, self.width, self.height, fraction)
        if self.native:
            from vmd_av_preview import png
            return png(rgb, self.width, self.height, compression=1)
        return getattr(graphics, self.backend)(rgb, self.width, self.height)

    def close(self, wait=True):
        self.closed.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
        if wait and self.thread:
            self.thread.join(timeout=3)
        if not self.thread or not self.thread.is_alive():
            self.directory.cleanup()
