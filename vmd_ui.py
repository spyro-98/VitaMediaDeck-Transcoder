"""Readable, value-specific explanations and preview time input."""
from __future__ import annotations

import math
from fractions import Fraction


def fps_label(value):
    try:
        decimal = f'{float(Fraction(str(value))):.3f}'.rstrip('0').rstrip('.')
        return f'{decimal} fps' + (f' ({value})' if '/' in str(value) else '')
    except (ValueError, ZeroDivisionError):
        return str(value)


def setting_description(settings, attribute: str) -> str:
    value = getattr(settings, attribute)
    descriptions = {
        "encoder": {
            "auto": "Measure complete CPU/GPU pipelines; prefer hardware when timings are close.",
            "videotoolbox": "Use Apple's hardware H.264 encoder on macOS.",
            "nvenc": "Use the NVIDIA GPU's hardware H.264 encoder.",
            "amf": "Use AMD's hardware H.264 encoder.",
            "vaapi": "Use a Linux VAAPI GPU for H.264 encoding.",
            "x264": "Encode on the CPU; supports film, animation and grain tuning.",
        },
        "quality": {
            "maximum": "Highest bitrate budget; more detail, larger files; cannot recover lost source detail.",
            "high": "Prioritize fine detail with a high bitrate budget.",
            "balanced": "Use a middle bitrate envelope to balance detail and file size.",
            "compact": "Use the lowest bitrate envelope for smaller output files.",
        },
        "scaler": {
            "auto": "High/Max keep Lanczos detail; Balanced/Compact compare CPU/GPU resize speed.",
            "apple": "Apple hardware resize: faster, but may soften fine detail compared with Lanczos.",
            "lanczos": "Sharp downscaling; preserves texture, with possible ringing near hard edges.",
            "bicubic": "Softer resize; useful when Lanczos creates visible edge halos.",
            "spline": "Smooth, detailed resize; compare edges and grain with Lanczos.",
        },
        "content_tune": {
            "movie": "Live action: film tuning on x264; movie bitrate curve on hardware.",
            "film-grain": "Film scans: keep grain; more bits; x264 grain tuning. Prefer denoise off.",
            "anime": "Clean animation: protect line art; animation tuning on x264.",
            "anime-grain": "Preserve scanned animation grain; larger files; grain tuning on x264.",
            "vita-sw": "Vita software playback: x264 fast-decode, no B-frames, one reference. Use x264, at most 30 fps.",
        },
        "system_load": {
            "low": "Limit FFmpeg to 2 threads to reduce desktop load.",
            "balanced": "Leave 2 CPU cores available, with at most 8 FFmpeg threads.",
            "full": "Remove FFmpeg thread limits; use available CPU resources.",
        },
        "tone_map": {
            "mobius": "HDR to SDR: retain midtones and compress bright highlights.",
            "hable": "HDR to SDR: film-style highlight roll-off with stronger contrast.",
            "reinhard": "HDR to SDR: compress highlights with the Reinhard curve.",
        },
        "cover_mode": {
            "auto": "Prefer embedded artwork; otherwise extract a representative frame.",
            "custom": "Use the chosen image as a 480x272 embedded JPEG cover.",
            "none": "Do not generate or embed a cover image.",
        },
    }
    if attribute in descriptions:
        return descriptions[attribute].get(str(value), f"Unknown value: {value}")
    if attribute == "max_fps":
        from fractions import Fraction
        try:
            return f"Cap output at {float(Fraction(str(value))):.3f} fps; lower-rate sources stay lower."
        except (ValueError, ZeroDivisionError):
            return "Invalid FPS ceiling; enter a positive number or fraction up to 60."
    if attribute == "audio_bitrate":
        return f"AAC ceiling of {value} kb/s for both stereo channels together, per selected track; limited to its source bitrate."
    if attribute == "force_hdr":
        return ("Force HDR-to-SDR processing even when source HDR tags are missing." if value
                else "Detect HDR from source metadata; tone-map only detected HDR sources.")
    if attribute == "hw_decode":
        return ("Try hardware decoding when suitable; fall back to software. HDR uses CPU filters." if value
                else "Decode on the CPU; the chosen encoding engine is independent.")
    if attribute == "overwrite":
        return ("Allow the validated result to replace an existing output file." if value
                else "Refuse an existing output path on a fresh conversion; resume validates it first.")
    if attribute == "cover_image":
        if settings.cover_mode != "custom":
            return "Inactive: choose Custom cover mode to use an image path."
        return "Read this image and resize/pad it to the embedded 480x272 cover."
    if attribute == "vaapi_device":
        return "Linux render device used when the selected/automatic encoder uses VAAPI."
    if attribute == "ffmpeg":
        return "Run this FFmpeg executable for video, audio, covers and previews."
    if attribute == "ffprobe":
        return "Run this FFprobe executable to inspect media and validate output."
    return ""


def preset_hint(name):
    return {
        'Vita HW - Max perceptual': 'Vita HW decoder: highest detail budget, up to 60 fps. Larger, lossy files; cannot restore source detail.',
        'Vita HW - Medium': 'Vita HW decoder: balanced detail and size; retains source FPS up to 60.',
        'Vita HW - Light': 'Vita HW decoder: smaller files, up to 30 fps; less fine detail and lighter audio.',
        'Vita SW - Max perceptual': 'Vita SW decoder: highest detail budget, up to 30 fps. x264 fast-decode, no B-frames, one reference; test demanding scenes on Vita.',
        'Vita SW - Medium': 'Vita SW decoder: balanced size and detail, up to 30 fps. x264 fast-decode reduces decoding work.',
        'Vita SW - Light': 'Vita SW decoder: smaller files, up to 30 fps. Lower detail; fast-decode helps but smooth playback still depends on the source and Vita clocks.',
        'Vita Movie Max': 'Live-action films: CPU x264, high detail; preserves natural texture.',
        'Vita Anime Max': 'Clean animation: CPU x264, high detail; protects line art.',
        'Vita Anime Grain': 'Scanned animation: retain grain and texture; larger files.',
        'Balanced': 'General use: middle bitrate budget, up to 60 fps; compromise between detail and size.',
        'Compact 30 fps': 'Smaller files: compact video budget and 30 fps ceiling; fine detail may soften.',
        'Mac Fast Hardware': 'Mac: fast Apple hardware encoding.',
        'Mac GPU Gentle Clean': 'Mac: Core Image filters + hardware encode.',
        'CPU Maximum Detail': 'CPU: slowest; prioritize detail per bit.',
        'Film Preserve Grain': 'Film scans: keep texture; no denoising.',
        'Noisy Film Gentle Clean': 'Mild noise: gentle cleanup; inspect grain.',
        'Noisy Source Compact': 'Noisy video: smaller file, softer detail.',
        'Animation Smooth Gradients': 'Animation: smooth colour bands, light edges.',
        'Soft Source Gentle Sharpen': 'Soft video: mild edge contrast; no new detail.',
        'HDR Highlight Detail': 'HDR: film-like highlights on SDR output.',
        'Computer CPU Compatibility': 'Computer CPU decoding and encoding; useful without host hardware support. Not a Vita SW optimization.',
    }.get(name, 'Custom settings: compare the changes below before applying.')


def parse_timestamp(value: str) -> float:
    """Accept seconds, MM:SS or HH:MM:SS without non-finite/negative values."""
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("Use seconds, MM:SS or HH:MM:SS.")
    numbers = [float(part) for part in parts]
    if any(not math.isfinite(part) or part < 0 for part in numbers):
        raise ValueError("Time must be finite and non-negative.")
    if len(parts) > 1 and any(part >= 60 for part in numbers[1:]):
        raise ValueError("Minutes/seconds after a colon must be below 60.")
    result = 0.0
    for number in numbers:
        result = result * 60 + number
    return result


def clamp_timestamp(value: float, duration: float | None, fps: float) -> float:
    # Keep enough source frames after a seek for the output FPS filter to emit.
    if duration and duration > 0:
        return max(0.0, min(value, max(0.0, duration - max(.15, 2 / max(1, fps)))))
    return max(0.0, value)
