"""Bounded random audio excerpts, converted with the transcoder's AAC contract."""
from __future__ import annotations

from pathlib import Path
import os
import platform
import random
import math
import shutil
import subprocess
import tempfile
import threading

import vitamediadeck_transcoder as core


def random_segments(duration, count=10, seconds=4.0):
    if not duration or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Audio preview requires a known positive video duration.')
    length = min(seconds, duration)
    maximum = max(0, duration - length)
    rng = random.SystemRandom()
    return [(rng.uniform(0, maximum), length) for _ in range(count)]


def encode_audio(source, settings, ordinal, start, length, output, run=subprocess.run):
    ffmpeg, ffprobe, _ = core.resolve_media_tools(settings['ffmpeg'], settings['ffprobe'])
    info = core.probe_media(ffprobe, source, settings['force_hdr'])
    if not 0 <= ordinal < info.audio_stream_count:
        raise ValueError('The selected input does not have that audio track.')
    if not info.duration or not 0 <= start < info.duration:
        raise ValueError('The selected audio excerpt is outside the source duration.')
    length = min(length, info.duration - start)
    bitrate = core.resolved_audio_bitrates(ffprobe, source, info, [ordinal], settings['audio_bitrate'])[0]
    command = [ffmpeg, '-v', 'error', '-y', '-ss', f'{start:.9f}', '-i', str(source),
               '-map', f'0:{info.audio_stream_indices[ordinal]}', '-vn', '-sn', '-dn',
               '-c:a', 'aac', '-profile:a', 'aac_low', '-ac', '2', '-ar', '48000',
               '-b:a', f'{bitrate}k', '-af', core.audio_filter(length), '-t', f'{length:.9f}', str(output)]
    result = run(command, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace')[-350:])
    return ffmpeg, bitrate, length


def player_command(encoded, wav, duration):
    ffplay = shutil.which('ffplay')
    if ffplay:
        return [ffplay, '-nodisp', '-autoexit', '-loglevel', 'error', '-t', str(duration), str(encoded)]
    if platform.system() == 'Darwin' and shutil.which('afplay'):
        return ['afplay', str(wav)]
    for player in ('paplay', 'aplay'):
        if shutil.which(player):
            return [player, str(wav)]
    if os.name == 'nt':
        powershell = shutil.which('powershell') or shutil.which('pwsh')
        if powershell:
            # The file path is passed via an environment variable, never as code.
            return [powershell, '-NoProfile', '-NonInteractive', '-Command',
                    '$p = New-Object System.Media.SoundPlayer; '
                    '$p.SoundLocation = $env:VMD_AUDIO_PREVIEW_FILE; $p.PlaySync()']
    raise ValueError('No audio player found. Install ffplay, or use afplay / paplay / aplay.')


class AudioPreview:
    def __init__(self):
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.token = 0
        self.pending = None
        self.process = None
        self.active = False
        self.status = 'L Listen to the converted AAC excerpt'
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def stop(self):
        with self.lock:
            self.token += 1
            self.pending = None
            self.active = False
            self.status = 'Audio stopped'
            if self.process and self.process.poll() is None:
                self.process.terminate()

    def play(self, source, settings, ordinal, segment):
        self.stop()
        with self.lock:
            self.pending = (self.token, source, settings.copy(), ordinal, segment)
            self.active = True
            self.status = 'Encoding AAC excerpt...'

    def close(self):
        self.closed.set()
        self.stop()

    def _run(self, command, token, **kwargs):
        with self.lock:
            if token != self.token or self.closed.is_set():
                raise ValueError('Audio preview cancelled.')
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.process = process
        try:
            stdout, stderr = process.communicate(timeout=kwargs.get('timeout', 30))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _worker(self):
        while not self.closed.wait(.05):
            with self.lock:
                job, self.pending = self.pending, None
            if job is None:
                continue
            token, source, settings, ordinal, (start, length) = job
            try:
                with tempfile.TemporaryDirectory(prefix='vmd-audio-preview-') as directory:
                    encoded = Path(directory) / 'excerpt.m4a'
                    wav = Path(directory) / 'excerpt.wav'
                    run = lambda command, **kwargs: self._run(command, token, **kwargs)
                    ffmpeg, bitrate, length = encode_audio(source, settings, ordinal, start, length, encoded, run)
                    command = player_command(encoded, wav, length)
                    if command[0] != shutil.which('ffplay'):
                        result = run([ffmpeg, '-v', 'error', '-y', '-i', str(encoded), '-t', str(length), str(wav)], timeout=20)
                        if result.returncode:
                            raise ValueError(result.stderr.decode(errors='replace')[-350:])
                    with self.lock:
                        if token != self.token or self.closed.is_set():
                            continue
                        self.status = f'Playing AAC-LC {bitrate} kb/s · 48 kHz stereo'
                        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            env={**os.environ, 'VMD_AUDIO_PREVIEW_FILE': str(wav)})
                        player = self.process
                    _, error = player.communicate(timeout=length + 15)
                    if player.returncode:
                        raise ValueError(error.decode(errors='replace')[-350:] or 'Audio player failed.')
                    with self.lock:
                        if token == self.token:
                            self.status = 'Excerpt finished · L play again'
            except Exception as exc:
                with self.lock:
                    if token == self.token:
                        if self.process and self.process.poll() is None:
                            self.process.kill()
                            self.process.wait()
                        self.status = f'Audio preview: {exc}'
            finally:
                with self.lock:
                    if token == self.token:
                        self.active = False
