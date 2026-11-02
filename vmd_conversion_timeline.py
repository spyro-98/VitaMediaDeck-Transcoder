"""A full-source filmstrip, progressively illuminated by encoded-video position."""
from pathlib import Path
import subprocess
import threading

import vitamediadeck_transcoder as core
import vmd_preview as graphics
import vmd_theme as theme


class MovieTimeline:
    def __init__(self, source, settings, info, count, width, height, backend, encode_image, defer=False,
                 placeholders_only=False, duration=None):
        self.source, self.settings, self.info = Path(source), settings, info
        self.count, self.width, self.height, self.backend = count, width, height, backend
        self.encode_image = encode_image
        self.placeholders_only = placeholders_only
        self.closed, self.lock = threading.Event(), threading.Lock()
        self.allowed = threading.Event()
        if not defer:
            self.allowed.set()
        self.process, self.error = None, ''
        self.duration = (info.duration if info else duration) or 0
        self.positions = [self.duration * i / count for i in range(count)]
        self.rgb = [None] * count
        self.dark = [bytes(theme.SURFACE) * (width * height)] * count
        self.placeholder = bytes(theme.SELECTION) * (width * height)
        self.painted = [None] * count
        self.payloads = [None] * count
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while not self.allowed.wait(.1):
                if self.closed.is_set():
                    return
            if self.placeholders_only or self.closed.is_set():
                return
            ffmpeg, ffprobe, _ = core.resolve_media_tools(self.settings['ffmpeg'], self.settings['ffprobe'])
            if self.info is None:
                self.info = core.probe_media(ffprobe, self.source, False)
            caps = core.discover_capabilities(ffmpeg) if self.info.hdr else None
            if self.info.hdr:
                chain = core.filter_chain(self.info, 960, 544, self.info.fps, 'x264', caps, 'mobius')
                chain += f',scale={self.width}:{self.height}:flags=lanczos'
            else:
                w, h = self.width, self.height
                cw, ch = core.content_dimensions(self.info)
                factor = min(w / cw, h / ch)
                cw, ch = max(1, round(cw * factor)), max(1, round(ch * factor))
                chain = f'format=rgb24,scale={cw}:{ch}:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1'
            for i, position in enumerate(self.positions):
                if self.closed.is_set():
                    return
                command = [ffmpeg, '-v', 'error', '-threads', '1', '-filter_threads', '1', '-ss', f'{position:.6f}',
                           '-i', str(self.source), '-map', f'0:{self.info.video_stream_index}', '-an', '-sn', '-dn',
                           '-vf', chain, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
                with self.lock:
                    if self.closed.is_set():
                        return
                    self.process = subprocess.Popen(core.prioritized_command(command, 'low'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                    **core.process_priority_kwargs('low'))
                    process = self.process
                try:
                    rgb, errors = process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise ValueError('Source timeline thumbnail timed out.')
                if self.closed.is_set():
                    return
                if process.returncode or len(rgb) != self.width * self.height * 3:
                    raise ValueError('Cannot read source timeline thumbnail: ' + errors.decode(errors='replace')[-150:])
                dark = bytes(min(255, round(value * .12 + theme.BG[at % 3] * .65)) for at, value in enumerate(rgb))
                self.dark[i], self.rgb[i] = dark, rgb
        except (OSError, ValueError, core.TranscodeError) as exc:
            if not self.closed.is_set():
                self.error = str(exc)

    def render(self, ratio):
        ratio = max(0.0, min(1.0, ratio))
        for i in range(self.count):
            raw = self.rgb[i] or self.placeholder
            cut = max(0, min(self.width, int((ratio * self.count - i) * self.width)))
            token = id(raw), cut
            if token == self.painted[i]:
                continue
            dark = self.dark[i]
            stride, boundary = self.width * 3, cut * 3
            data = dark if not cut else raw if cut == self.width else b''.join(
                raw[row:row + boundary] + dark[row + boundary:row + stride]
                for row in range(0, len(raw), stride))
            self.payloads[i] = (self.encode_image(data, self.width, self.height) if self.backend in ('kitty', 'iterm2') else
                                getattr(graphics, self.backend)(data, self.width, self.height))
            self.painted[i] = token
        return tuple(self.payloads), tuple(self.painted)

    def close(self, wait=False):
        self.closed.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
        if wait:
            self.thread.join(timeout=3)

    def activate(self):
        self.allowed.set()
