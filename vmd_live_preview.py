"""Preview timing and fresh single-frame encoding helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile

import vitamediadeck_transcoder as core
import vmd_ui as ui


def sample_time(duration, fps, index):
    if not duration or duration <= 0:
        return 0.0
    if index is None:
        return ui.clamp_timestamp(duration * .2, duration, fps)
    last = ui.clamp_timestamp(duration, duration, fps)
    return last * max(0, min(99, index)) / 99


@dataclass
class Frame:
    png: bytes
    rgb: bytes
    width: int
    height: int
    timestamp: float
    duration: float | None
    fps: float
    encoder: str


def encode_frame(source, settings, index, width, height):
    ffmpeg, ffprobe, _ = core.resolve_media_tools(settings['ffmpeg'], settings['ffprobe'])
    info = core.probe_media(ffprobe, source, settings['force_hdr'])
    if not info.duration or info.duration <= 0:
        raise ValueError('Source duration is unknown; 100-point preview navigation needs a seekable video with a known duration.')
    caps = core.discover_capabilities(ffmpeg)
    fps = core.target_fps(info.fps, core.fraction_from_text(settings['max_fps']) or core.MAX_FPS)
    timestamp = sample_time(info.duration, float(fps), index)
    errors = []
    with tempfile.TemporaryDirectory(prefix='vmd-live-preview-') as directory:
        encoded = Path(directory) / 'sample.mkv'
        chosen = None
        for encoder in core.encoder_candidates(settings['encoder'], caps, Path(settings['vaapi_device'])):
            modes = [True, False] if settings['hw_decode'] and not info.hdr and encoder != 'vaapi' else [False]
            for hardware in modes:
                command = core.build_command(ffmpeg, source, str(encoded), info, caps, encoder, hardware,
                    core.OUTPUT_WIDTH, core.OUTPUT_HEIGHT, fps, settings['quality'], settings['content_tune'],
                    settings['audio_bitrate'], settings['tone_map'], Path(settings['vaapi_device']), False,
                    audio_tracks=[], subtitle_tracks=[], system_load=settings['system_load'], preserve_extras=False,
                    video_options=core.video_options(settings))
                input_index = command.index('-i')
                command[input_index:input_index] = ['-ss', f'{timestamp:.6f}']
                command[-1:-1] = ['-frames:v', '1', '-loglevel', 'error']
                result = subprocess.run(command, capture_output=True, timeout=30)
                if result.returncode == 0 and not core.filter_output_failed(result.stderr) and encoded.is_file() and encoded.stat().st_size:
                    chosen = encoder
                    break
                errors.append(result.stderr.decode(errors='replace')[-350:])
            if chosen:
                break
        if not chosen:
            raise ValueError('Preview encoding failed: ' + ' / '.join(errors))
        command = [ffmpeg, '-v', 'error', '-i', str(encoded), '-vf', f'scale={width}:{height}', '-frames:v', '1']
        png = subprocess.run(command + ['-f', 'image2pipe', '-c:v', 'png', '-'], capture_output=True, timeout=20, check=True).stdout
        rgb = subprocess.run(command + ['-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, timeout=20, check=True).stdout
        if len(rgb) != width * height * 3 or not png:
            raise ValueError('The encoded sample did not produce a preview frame.')
        return Frame(png, rgb, width, height, timestamp, info.duration, float(fps), chosen)
