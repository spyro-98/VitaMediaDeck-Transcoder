#!/usr/bin/env python3
"""Convert mainstream video files to the VitaMediaDeck playback contract.

This is a host-side utility. It is not part of the PS Vita application and is
never included in the VPK. The only runtime dependencies are Python 3, FFmpeg,
and ffprobe.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence


OUTPUT_WIDTH = 960
OUTPUT_HEIGHT = 544
COVER_WIDTH = 480
COVER_HEIGHT = 272
VITA_AUDIO_CODEC = "aac"
VITA_AUDIO_PROFILE = "LC"
VITA_AUDIO_SAMPLE_RATE = 48_000
VITA_AUDIO_MAX_CHANNELS = 2
COVER_THUMBNAIL_WINDOW_FRAMES = 120
COVER_BLACK_YAVG_MAX = 10.0
COVER_BLACK_YMAX_MAX = 32.0
MAX_FPS = Fraction(60, 1)
HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "hlg", "pq"}
VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv",
        ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".rm",
        ".rmvb", ".ts", ".vob", ".webm", ".wmv",
    }
)
ENCODER_NAMES = {
    "videotoolbox": "h264_videotoolbox",
    "nvenc": "h264_nvenc",
    "amf": "h264_amf",
    "vaapi": "h264_vaapi",
    "x264": "libx264",
}
QUALITY_BPP = {"compact": 0.10, "balanced": 0.14, "high": 0.18}
QUALITY_LIMITS = {
    "compact": (1_400_000, 4_000_000),
    "balanced": (1_800_000, 5_000_000),
    "high": (2_400_000, 6_000_000),
}
CONTENT_TUNES = ("movie", "anime", "anime-grain")
X264_CONTENT_TUNES = {
    "movie": "film",
    "anime": "animation",
    "anime-grain": "grain",
}
PROGRESS_PREFIX = "@@VMD_PROGRESS@@"
RESUME_STATE_VERSION = 1
PREFLIGHT_SECONDS = 3.0
VIDEO_STALL_SECONDS = 30.0
SYSTEM_LOAD_THREADS = {
    "low": 2,
    "balanced": -1,
    "full": 0,
}


class TranscodeError(RuntimeError):
    pass


class AudioDurationError(TranscodeError):
    pass


class VideoPipelineError(TranscodeError):
    pass


class VideoDurationError(VideoPipelineError):
    pass


class VideoProgressError(VideoPipelineError):
    pass


def emit_progress(
    phase: str,
    state: str,
    detail: str = "",
    **values: object,
) -> None:
    if os.environ.get("VMD_MACHINE_PROGRESS") != "1":
        return
    payload: dict[str, object] = {"phase": phase, "state": state, "detail": detail}
    batch_index = os.environ.get("VMD_BATCH_INDEX")
    batch_total = os.environ.get("VMD_BATCH_TOTAL")
    if batch_index and batch_total:
        try:
            index = int(batch_index)
            total = int(batch_total)
        except ValueError:
            index = total = 0
        if 1 <= index <= total:
            payload.update(
                {
                    "batch_index": index,
                    "batch_total": total,
                    "batch_source": os.environ.get("VMD_BATCH_SOURCE", ""),
                }
            )
    payload.update(values)
    print(
        PROGRESS_PREFIX
        + json.dumps(payload, separators=(",", ":")),
        flush=True,
    )


def announce_phase(number: int, total: int, label: str, phase: str, detail: str = "") -> None:
    print(f"\n[{number}/{total}] {label}", flush=True)
    emit_progress(phase, "start", detail)


@dataclass(frozen=True)
class MediaInfo:
    video_stream_index: int
    width: int
    height: int
    display_width: float
    display_height: float
    fps: Fraction
    interlaced: bool
    hdr: bool
    video_codec: str
    pixel_format: str
    color_transfer: str
    color_primaries: str
    color_space: str
    audio_stream_count: int
    audio_stream_indices: tuple[int, ...]
    audio_bit_rates: tuple[int | None, ...]
    audio_durations: tuple[float | None, ...]
    subtitle_codecs: tuple[str, ...]
    subtitle_stream_indices: tuple[int, ...]
    attachment_count: int
    cover_stream_index: int | None
    cover_name: str
    duration: float | None


@dataclass(frozen=True)
class Capabilities:
    encoders: frozenset[str]
    filters: frozenset[str]
    hwaccels: frozenset[str]


@dataclass(frozen=True)
class EncoderPlan:
    key: str
    codec: str
    hw_decode: bool


def command_text(command: Sequence[str]) -> str:
    return shlex.join(str(part) for part in command)


def resource_thread_limit(system_load: str) -> int:
    if system_load == "balanced":
        detected = os.cpu_count() or 4
        return max(2, min(8, detected - 2))
    return SYSTEM_LOAD_THREADS[system_load]


def prioritized_command(command: Sequence[str], system_load: str) -> list[str]:
    """Keep long FFmpeg jobs from starving the desktop session."""
    result = list(command)
    if system_load == "full" or os.name == "nt":
        return result
    taskpolicy = Path("/usr/sbin/taskpolicy")
    if platform.system() == "Darwin" and taskpolicy.is_file():
        qos = "background" if system_load == "low" else "utility"
        return [str(taskpolicy), "-c", qos, *result]
    if Path("/usr/bin/nice").is_file():
        return ["/usr/bin/nice", "-n", "10", *result]
    return result


def process_priority_kwargs(system_load: str) -> dict[str, Any]:
    if system_load != "full" and os.name == "nt":
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)}
    return {}


def run_ffmpeg(command: Sequence[str], system_load: str) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        prioritized_command(command, system_load),
        check=False,
        **process_priority_kwargs(system_load),
    )


def progress_time_seconds(line: str) -> float | None:
    match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def run_transcode_guarded(
    command: Sequence[str],
    system_load: str,
    expected_fps: Fraction,
    expected_duration: float | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Stream FFmpeg output and abort only after sustained frame-counter stagnation."""
    process = subprocess.Popen(
        prioritized_command(command, system_load),
        stdout=None,
        stderr=subprocess.PIPE,
        **process_priority_kwargs(system_load),
    )
    assert process.stderr is not None
    chunks: queue.Queue[bytes | None] = queue.Queue()

    def read_stderr() -> None:
        try:
            while True:
                reader = getattr(process.stderr, "read1", process.stderr.read)
                chunk = reader(4096)
                if not chunk:
                    break
                chunks.put(chunk)
        finally:
            chunks.put(None)

    reader_thread = threading.Thread(target=read_stderr, daemon=True)
    reader_thread.start()
    buffer = ""
    last_frame_count = -1
    last_frame_change = time.monotonic()
    last_progress_ratio = -1.0
    starvation: tuple[int, float, float] | None = None
    while True:
        try:
            raw_chunk = chunks.get(timeout=0.20)
        except queue.Empty:
            if process.poll() is not None and not reader_thread.is_alive():
                break
            continue
        if raw_chunk is None:
            break
        chunk = raw_chunk.decode(errors="replace")
        sys.stderr.write(chunk)
        sys.stderr.flush()
        buffer += chunk
        parts = re.split(r"[\r\n]", buffer)
        buffer = parts.pop()
        for line in parts:
            frame_match = re.search(r"(?:^|\s)frame=\s*(\d+)", line)
            media_seconds = progress_time_seconds(line)
            if media_seconds is not None and expected_duration and expected_duration > 0:
                progress_ratio = max(0.0, min(1.0, media_seconds / expected_duration))
                if progress_ratio >= last_progress_ratio + 0.005 or progress_ratio >= 1.0:
                    emit_progress(
                        "transcode",
                        "progress",
                        "VIDEO PASS",
                        progress=progress_ratio,
                        media_seconds=media_seconds,
                        duration=expected_duration,
                    )
                    last_progress_ratio = progress_ratio
            if not frame_match or media_seconds is None or media_seconds < 30.0:
                continue
            frame_count = int(frame_match.group(1))
            now = time.monotonic()
            if frame_count > last_frame_count:
                last_frame_count = frame_count
                last_frame_change = now
                continue
            stalled_for = now - last_frame_change
            if stalled_for >= VIDEO_STALL_SECONDS:
                starvation = (frame_count, media_seconds, stalled_for)
                break
        if starvation is not None:
            break
    if starvation is not None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader_thread.join(timeout=0.5)
        frame_count, media_seconds, stalled_for = starvation
        expected_by_clock = int(media_seconds * float(expected_fps))
        raise VideoProgressError(
            "Video pipeline stalled: the encoded frame counter remained at "
            f"{frame_count} for {stalled_for:.1f}s while the media clock reached "
            f"{media_seconds:.1f}s (approximately {expected_by_clock} frames expected by "
            "that clock). The unsafe attempt was stopped early."
        )
    returncode = process.wait()
    reader_thread.join(timeout=0.5)
    return subprocess.CompletedProcess(list(command), returncode)


def run_capture(command: Sequence[str], description: str) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise TranscodeError(f"Unable to run {description}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise TranscodeError(f"{description} failed:\n{detail}")
    return result.stdout


def resolve_tool(value: str, label: str) -> str:
    resolved = shutil.which(value)
    if resolved:
        return resolved
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    raise TranscodeError(f"{label} was not found: {value}")


def resolve_media_tools(
    ffmpeg_value: str,
    ffprobe_value: str,
) -> tuple[str, str, str]:
    """Resolve a matched FFmpeg/ffprobe pair, preferring ffmpeg-full on macOS."""
    use_defaults = ffmpeg_value == "ffmpeg" and ffprobe_value == "ffprobe"
    if platform.system() == "Darwin" and use_defaults:
        candidates = (
            Path("/opt/homebrew/opt/ffmpeg-full/bin"),
            Path("/usr/local/opt/ffmpeg-full/bin"),
        )
        for directory in candidates:
            ffmpeg = directory / "ffmpeg"
            ffprobe = directory / "ffprobe"
            if ffmpeg.is_file() and ffprobe.is_file():
                return str(ffmpeg), str(ffprobe), "Homebrew ffmpeg-full"
    return (
        resolve_tool(ffmpeg_value, "FFmpeg"),
        resolve_tool(ffprobe_value, "ffprobe"),
        "configured FFmpeg",
    )


def fraction_from_text(value: Any) -> Fraction | None:
    if value in (None, "", "0/0", "N/A"):
        return None
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


def duration_seconds(value: Any) -> float | None:
    """Parse ffprobe numeric durations and Matroska DURATION tags."""
    if value in (None, "", "N/A"):
        return None
    text = str(value).strip()
    try:
        if ":" not in text:
            result = float(text)
        else:
            hours, minutes, seconds = text.split(":", 2)
            result = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def stream_duration(stream: dict[str, Any]) -> float | None:
    return duration_seconds(stream.get("duration")) or duration_seconds(
        (stream.get("tags") or {}).get("DURATION")
    )


def stream_bit_rate(stream: dict[str, Any]) -> int | None:
    candidates: list[Any] = [stream.get("bit_rate")]
    tags = stream.get("tags") or {}
    candidates.extend(
        value
        for key, value in tags.items()
        if str(key).upper() in {"BPS", "BPS-ENG"}
    )
    for value in candidates:
        try:
            result = int(value)
        except (TypeError, ValueError):
            continue
        if result > 0:
            return result
    return None


def selected_track_ordinals(
    requested: Sequence[int] | None,
    disabled: bool,
    total: int,
    label: str,
) -> tuple[int, ...]:
    if disabled:
        return ()
    if requested is None:
        return tuple(range(total))
    selected: list[int] = []
    for ordinal in requested:
        if ordinal < 0 or ordinal >= total:
            raise TranscodeError(
                f"--{label}-track {ordinal} is invalid; this input has {total} {label} track(s), "
                f"numbered 0 through {max(0, total - 1)}."
            )
        if ordinal not in selected:
            selected.append(ordinal)
    return tuple(selected)


def normalize_fps(value: Fraction) -> Fraction:
    standards = (
        Fraction(24_000, 1_001),
        Fraction(24, 1),
        Fraction(25, 1),
        Fraction(30_000, 1_001),
        Fraction(30, 1),
        Fraction(48_000, 1_001),
        Fraction(48, 1),
        Fraction(50, 1),
        Fraction(60_000, 1_001),
        Fraction(60, 1),
    )
    for standard in standards:
        relative_error = abs(float(value - standard)) / float(standard)
        if relative_error <= 0.001:
            return standard
    return value.limit_denominator(100_000)


def probe_media(ffprobe: str, source: Path, force_hdr: bool) -> MediaInfo:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(source),
    ]
    try:
        payload = json.loads(run_capture(command, "ffprobe"))
    except json.JSONDecodeError as exc:
        raise TranscodeError(f"ffprobe returned invalid JSON: {exc}") from exc

    streams = payload.get("streams") or []
    embedded_covers = [
        item
        for item in streams
        if item.get("codec_type") == "video"
        and int((item.get("disposition") or {}).get("attached_pic") or 0)
    ]
    embedded_covers.sort(
        key=lambda item: (
            0
            if str((item.get("tags") or {}).get("filename") or "").lower()
            in {"cover.jpg", "cover.jpeg", "cover.png"}
            else 1,
            int(item.get("index") or 0),
        )
    )
    embedded_cover = embedded_covers[0] if embedded_covers else None
    video = next(
        (
            item
            for item in streams
            if item.get("codec_type") == "video"
            and not int((item.get("disposition") or {}).get("attached_pic") or 0)
        ),
        None,
    )
    if not video:
        raise TranscodeError("The input does not contain a video stream.")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise TranscodeError("ffprobe did not report a valid video resolution.")

    rotation = 0
    tags = video.get("tags") or {}
    try:
        rotation = int(float(tags.get("rotate", 0)))
    except (TypeError, ValueError):
        rotation = 0
    side_data = video.get("side_data_list") or []
    for item in side_data:
        if "rotation" in item:
            try:
                rotation = int(float(item["rotation"]))
            except (TypeError, ValueError):
                pass

    sar = fraction_from_text(video.get("sample_aspect_ratio")) or Fraction(1, 1)
    display_width = float(Fraction(width) * sar)
    display_height = float(height)
    if abs(rotation) % 180 == 90:
        display_width, display_height = display_height, display_width

    fps = normalize_fps(
        fraction_from_text(video.get("avg_frame_rate"))
        or fraction_from_text(video.get("r_frame_rate"))
        or Fraction(30, 1)
    )
    field_order = str(video.get("field_order") or "").lower()
    interlaced = field_order not in {"", "unknown", "progressive"}
    color_transfer = str(video.get("color_transfer") or "unknown").lower()
    color_primaries = str(video.get("color_primaries") or "unknown").lower()
    color_space = str(video.get("color_space") or "unknown").lower()
    hdr_side_data = any(
        any(token in str(item.get("side_data_type") or "").lower()
            for token in ("mastering", "content light", "dovi", "hdr10"))
        for item in side_data
    )
    pixel_format = str(video.get("pix_fmt") or "unknown")
    high_bit_depth = "10" in pixel_format or "12" in pixel_format
    wide_gamut = color_primaries == "bt2020" or color_space.startswith("bt2020")
    hdr = (
        force_hdr
        or color_transfer in HDR_TRANSFERS
        or hdr_side_data
        or (high_bit_depth and wide_gamut)
    )

    # The primary video duration is the playback contract. Matroska commonly
    # stores it as a DURATION tag instead of stream.duration.
    duration = stream_duration(video) or duration_seconds(
        (payload.get("format") or {}).get("duration")
    )

    return MediaInfo(
        video_stream_index=int(video.get("index") or 0),
        width=width,
        height=height,
        display_width=display_width,
        display_height=display_height,
        fps=fps,
        interlaced=interlaced,
        hdr=hdr,
        video_codec=str(video.get("codec_name") or "unknown"),
        pixel_format=pixel_format,
        color_transfer=color_transfer,
        color_primaries=color_primaries,
        color_space=color_space,
        audio_stream_count=sum(item.get("codec_type") == "audio" for item in streams),
        audio_stream_indices=tuple(
            int(item.get("index") or 0)
            for item in streams
            if item.get("codec_type") == "audio"
        ),
        audio_bit_rates=tuple(
            stream_bit_rate(item)
            for item in streams
            if item.get("codec_type") == "audio"
        ),
        audio_durations=tuple(
            stream_duration(item)
            for item in streams
            if item.get("codec_type") == "audio"
        ),
        subtitle_codecs=tuple(
            str(item.get("codec_name") or "unknown")
            for item in streams
            if item.get("codec_type") == "subtitle"
        ),
        subtitle_stream_indices=tuple(
            int(item.get("index") or 0)
            for item in streams
            if item.get("codec_type") == "subtitle"
        ),
        attachment_count=sum(item.get("codec_type") == "attachment" for item in streams),
        cover_stream_index=(
            int(embedded_cover.get("index") or 0) if embedded_cover is not None else None
        ),
        cover_name=(
            str((embedded_cover.get("tags") or {}).get("filename") or "embedded artwork")
            if embedded_cover is not None
            else ""
        ),
        duration=duration,
    )


def discover_capabilities(ffmpeg: str) -> Capabilities:
    encoders_text = run_capture([ffmpeg, "-hide_banner", "-encoders"], "FFmpeg encoder discovery")
    filters_text = run_capture([ffmpeg, "-hide_banner", "-filters"], "FFmpeg filter discovery")
    hwaccels_text = run_capture([ffmpeg, "-hide_banner", "-hwaccels"], "FFmpeg hwaccel discovery")

    encoders = {
        line.split()[1]
        for line in encoders_text.splitlines()
        if len(line.split()) >= 2 and line.lstrip().startswith("V")
    }
    filters = {
        line.split()[1]
        for line in filters_text.splitlines()
        if len(line.split()) >= 2 and not line.lstrip().startswith("=")
    }
    hwaccels = {
        line.strip()
        for line in hwaccels_text.splitlines()
        if line.strip() and not line.lower().startswith("hardware acceleration")
    }
    return Capabilities(frozenset(encoders), frozenset(filters), frozenset(hwaccels))


def even_floor(value: float) -> int:
    return max(2, int(math.floor(value / 2.0)) * 2)


def content_dimensions(info: MediaInfo) -> tuple[int, int]:
    factor = min(
        OUTPUT_WIDTH / info.display_width,
        OUTPUT_HEIGHT / info.display_height,
    )
    width = even_floor(info.display_width * factor)
    height = even_floor(info.display_height * factor)
    width = min(width, OUTPUT_WIDTH)
    height = min(height, OUTPUT_HEIGHT)
    return width, height


def fitted_dimensions(_info: MediaInfo) -> tuple[int, int]:
    return OUTPUT_WIDTH, OUTPUT_HEIGHT


def target_fps(source_fps: Fraction, max_fps: Fraction) -> Fraction:
    chosen = min(source_fps, max_fps, MAX_FPS)
    if chosen <= 0:
        return Fraction(30, 1)
    return chosen.limit_denominator(100_000)


def target_audio_bitrates(
    info: MediaInfo,
    selected_audio_tracks: Sequence[int],
    requested_kbps: int,
) -> tuple[int, ...]:
    """Keep each AAC target at or below the source bitrate when it is known.

    Matroska and some variable-bitrate codecs do not always expose a per-stream
    bitrate through ffprobe.  In that case the explicitly selected bitrate is
    retained, rather than guessing a possibly incorrect value.
    """
    targets: list[int] = []
    for ordinal in selected_audio_tracks:
        source_bps = info.audio_bit_rates[ordinal]
        source_kbps = source_bps // 1000 if source_bps else None
        targets.append(min(requested_kbps, source_kbps) if source_kbps else requested_kbps)
    return tuple(targets)


def measured_audio_bit_rate(
    ffprobe: str,
    source: Path,
    audio_ordinal: int,
    duration: float | None,
) -> int | None:
    """Measure a stream when containers omit its bitrate metadata.

    FFprobe does not publish `bit_rate` for many Matroska audio tracks.  Packet
    sizes divided by the audio stream duration provide the actual average rate
    without relying on the container's overall bitrate.
    """
    if duration is None or duration <= 0:
        return None
    try:
        packet_sizes = run_capture(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                f"a:{audio_ordinal}",
                "-show_packets",
                "-show_entries",
                "packet=size",
                "-of",
                "csv=p=0",
                str(source),
            ],
            f"audio bitrate measurement for track {audio_ordinal}",
        )
    except TranscodeError:
        return None
    total_bytes = 0
    for line in packet_sizes.splitlines():
        try:
            total_bytes += int(line.strip().split(",", 1)[0])
        except ValueError:
            continue
    return int(total_bytes * 8 / duration) if total_bytes else None


def resolved_audio_bitrates(
    ffprobe: str,
    source: Path,
    info: MediaInfo,
    selected_audio_tracks: Sequence[int],
    requested_kbps: int,
) -> tuple[int, ...]:
    source_rates = list(info.audio_bit_rates)
    for ordinal in selected_audio_tracks:
        if source_rates[ordinal] is None:
            source_rates[ordinal] = measured_audio_bit_rate(
                ffprobe,
                source,
                ordinal,
                info.audio_durations[ordinal] or info.duration,
            )
        if source_rates[ordinal] is None:
            raise TranscodeError(
                f"Cannot determine the bitrate of selected audio track {ordinal}. "
                "The conversion stops rather than creating an audio stream above the source rate."
            )
    measured_info = replace(info, audio_bit_rates=tuple(source_rates))
    return target_audio_bitrates(measured_info, selected_audio_tracks, requested_kbps)


def bitrate_plan(
    width: int,
    height: int,
    fps: Fraction,
    quality: str,
    content_tune: str = "movie",
) -> tuple[int, int, int]:
    if quality == "high" and width == OUTPUT_WIDTH and height == OUTPUT_HEIGHT:
        cadence = float(fps)
        if cadence <= 24.1:
            targets = {"movie": 2_400_000, "anime": 2_200_000, "anime-grain": 2_800_000}
        elif cadence <= 30.1:
            targets = {"movie": 2_800_000, "anime": 2_600_000, "anime-grain": 3_200_000}
        elif cadence <= 50.1:
            targets = {"movie": 5_000_000, "anime": 4_500_000, "anime-grain": 5_500_000}
        else:
            targets = {"movie": 5_600_000, "anime": 5_200_000, "anime-grain": 6_000_000}
        target = targets[content_tune]
        maxrate = 4_500_000 if cadence <= 30.1 else 8_000_000
        return target, maxrate, maxrate * 2

    minimum, maximum = QUALITY_LIMITS[quality]
    estimated = width * height * float(fps) * QUALITY_BPP[quality]
    estimated *= {"movie": 1.0, "anime": 0.9, "anime-grain": 1.1}[content_tune]
    target = max(minimum, min(maximum, int(round(estimated / 100_000.0)) * 100_000))
    maxrate = min(14_000_000, int(round(target * 1.3 / 100_000.0)) * 100_000)
    return target, maxrate, maxrate * 2


def h264_level(width: int, height: int, fps: Fraction) -> str:
    del width, height
    return "3.2" if fps > Fraction(30, 1) else "3.1"


def filter_chain(
    info: MediaInfo,
    width: int,
    height: int,
    fps: Fraction,
    encoder_key: str,
    capabilities: Capabilities,
    tone_map: str,
) -> str:
    filters: list[str] = []
    content_width, content_height = content_dimensions(info)
    if info.interlaced:
        # Do not double a 25/29.97 fps source to its field cadence.  The
        # transcoder preserves the source frame rate by deinterlacing one
        # output frame per input frame.
        filters.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")

    if info.hdr:
        if "zscale" not in capabilities.filters or "tonemap" not in capabilities.filters:
            raise TranscodeError(
                "HDR input detected, but the selected FFmpeg executable lacks "
                "zscale/tonemap. It may not be the ffmpeg-full binary you installed. "
                "On macOS, leave both tool settings as ffmpeg/ffprobe for automatic "
                "Homebrew ffmpeg-full discovery, or select its paired bin paths. "
                "The tool will not create an incorrectly clipped or washed-out SDR file."
            )
        filters.extend(
            [
                f"zscale=w={content_width}:h={content_height}:filter=lanczos:t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                f"tonemap=tonemap={tone_map}:desat=2",
                "zscale=t=bt709:m=bt709:r=limited:d=error_diffusion",
                "format=yuv420p",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            ]
        )
    else:
        filters.extend(
            [
                (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                    "force_divisible_by=2:flags=lanczos"
                ),
                "format=yuv420p",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            ]
        )

    filters.append("setsar=1")
    filters.append(f"fps={fps.numerator}/{fps.denominator}")
    filters.append(f"setpts=N*{fps.denominator}/({fps.numerator}*TB)")
    if encoder_key == "vaapi":
        filters.extend(["format=nv12", "hwupload"])
    return ",".join(filters)


def encoder_candidates(requested: str, capabilities: Capabilities, vaapi_device: Path) -> list[str]:
    if requested != "auto":
        codec = ENCODER_NAMES[requested]
        if codec not in capabilities.encoders:
            raise TranscodeError(f"The requested encoder is not available in FFmpeg: {codec}")
        if requested == "vaapi" and not vaapi_device.exists():
            raise TranscodeError(f"VAAPI device not found: {vaapi_device}")
        return [requested]

    order: list[str] = []
    system = platform.system()
    if system == "Darwin":
        order.append("videotoolbox")
    order.extend(["nvenc", "amf"])
    if system == "Linux" and vaapi_device.exists():
        order.append("vaapi")
    order.append("x264")

    available: list[str] = []
    for key in order:
        if ENCODER_NAMES[key] in capabilities.encoders and key not in available:
            available.append(key)
    if not available:
        raise TranscodeError("No supported H.264 encoder was found in this FFmpeg build.")
    return available


def encoder_arguments(
    key: str,
    level: str,
    target_bps: int,
    maxrate_bps: int,
    bufsize_bps: int,
    gop: int,
    content_tune: str,
) -> list[str]:
    args = [
        "-c:v",
        ENCODER_NAMES[key],
        "-profile:v",
        "high",
        "-level:v",
        level,
        "-b:v",
        str(target_bps),
        "-maxrate",
        str(maxrate_bps),
        "-bufsize",
        str(bufsize_bps),
        "-g",
        str(gop),
    ]
    if key == "videotoolbox":
        args.extend(
            [
                "-allow_sw",
                "0",
                "-realtime",
                "0",
                "-prio_speed",
                "0",
                "-max_ref_frames",
                "3",
                "-bf",
                "2",
                "-coder",
                "cabac",
            ]
        )
    elif key == "nvenc":
        args.extend(
            [
                "-preset",
                "p6",
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                "18",
                "-multipass",
                "fullres",
                "-spatial_aq",
                "1",
                "-temporal_aq",
                "1",
                "-bf",
                "2",
                "-refs",
                "3",
            ]
        )
    elif key == "amf":
        args.extend(
            [
                "-usage",
                "transcoding",
                "-quality",
                "quality",
                "-rc",
                "vbr_peak",
                "-bf",
                "2",
                "-refs",
                "3",
            ]
        )
    elif key == "vaapi":
        args.extend(["-rc_mode", "VBR", "-bf", "2", "-refs", "3"])
    else:
        args.extend(
            [
                "-preset",
                "slow",
                "-tune",
                X264_CONTENT_TUNES[content_tune],
                "-crf",
                "17.5" if content_tune == "anime" else "18",
                "-bf",
                "2",
                "-refs",
                "3",
            ]
        )
    return args


def build_command(
    ffmpeg: str,
    source: Path,
    destination: str,
    info: MediaInfo,
    capabilities: Capabilities,
    encoder_key: str,
    use_hw_decode: bool,
    width: int,
    height: int,
    fps: Fraction,
    quality: str,
    content_tune: str,
    audio_bitrate: int,
    tone_map: str,
    vaapi_device: Path,
    preflight: bool,
    audio_tracks: Sequence[int] | None = None,
    subtitle_tracks: Sequence[int] | None = None,
    system_load: str = "balanced",
    preserve_extras: bool = True,
    audio_bitrates: Sequence[int] | None = None,
) -> list[str]:
    target_bps, maxrate_bps, bufsize_bps = bitrate_plan(
        width, height, fps, quality, content_tune
    )
    level = h264_level(width, height, fps)
    gop = max(24, int(round(float(fps) * 2.0)))

    command = [ffmpeg, "-hide_banner", "-y"]
    thread_limit = resource_thread_limit(system_load)
    if thread_limit:
        command.extend(
            [
                "-filter_threads",
                str(max(1, min(4, thread_limit // 2))),
                "-filter_complex_threads",
                str(max(1, min(4, thread_limit // 2))),
                "-threads",
                str(thread_limit),
            ]
        )
    if preflight:
        command.extend(["-loglevel", "error"])
    if encoder_key == "vaapi":
        command.extend(["-vaapi_device", str(vaapi_device)])
    if use_hw_decode and encoder_key != "vaapi":
        command.extend(["-hwaccel", "auto"])
    command.extend(["-i", str(source)])
    if preflight:
        command.extend(["-t", f"{PREFLIGHT_SECONDS:g}"])
    command.extend(["-map", f"0:{info.video_stream_index}"])
    if not preflight:
        selected_audio = (
            tuple(range(info.audio_stream_count))
            if audio_tracks is None
            else tuple(audio_tracks)
        )
        selected_subtitles = (
            tuple(range(len(info.subtitle_codecs)))
            if subtitle_tracks is None
            else tuple(subtitle_tracks)
        )
        for ordinal in selected_audio:
            command.extend(["-map", f"0:{info.audio_stream_indices[ordinal]}"])
        for ordinal in selected_subtitles:
            command.extend(["-map", f"0:{info.subtitle_stream_indices[ordinal]}"])
        if preserve_extras:
            command.extend(["-map", "0:t?"])

    command.extend(
        [
            "-vf",
            filter_chain(info, width, height, fps, encoder_key, capabilities, tone_map),
            "-r",
            f"{fps.numerator}/{fps.denominator}",
            "-fps_mode",
            "cfr",
        ]
    )
    command.extend(
        encoder_arguments(
            encoder_key,
            level,
            target_bps,
            maxrate_bps,
            bufsize_bps,
            gop,
            content_tune,
        )
    )
    if thread_limit and encoder_key == "x264":
        command.extend(["-threads:v", str(thread_limit)])
    command.extend(
        [
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
        ]
    )

    if preflight:
        command.extend(
            [
                "-an",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "null",
                destination,
            ]
        )
    else:
        if selected_audio:
            command.extend(
                [
                    "-c:a",
                    VITA_AUDIO_CODEC,
                    "-profile:a",
                    "aac_low",
                    "-ac",
                    str(VITA_AUDIO_MAX_CHANNELS),
                    "-ar",
                    str(VITA_AUDIO_SAMPLE_RATE),
                ]
            )
            audio_targets = audio_bitrates or tuple(audio_bitrate for _ in selected_audio)
            for output_index, bitrate in enumerate(audio_targets):
                command.extend([f"-b:a:{output_index}", f"{bitrate}k"])
            command.extend(["-af", audio_filter(info.duration)])
        for output_index, source_ordinal in enumerate(selected_subtitles):
            codec = info.subtitle_codecs[source_ordinal]
            subtitle_codec = "srt" if codec in {"mov_text", "text", "webvtt"} else "copy"
            command.extend([f"-c:s:{output_index}", subtitle_codec])
        command.extend(["-c:t", "copy"])
        command.extend(
            [
                "-dn",
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-metadata:s:v:0",
                "rotate=0",
                "-max_muxing_queue_size",
                "4096",
                destination,
            ]
        )
    return command


def representative_timestamp(duration: float | None) -> float:
    if duration is None or duration <= 0:
        return 0.0
    if duration <= 1.0:
        return duration * 0.25
    return min(max(duration * 0.10, 1.0), 30.0, duration - 0.1)


def alternate_representative_timestamp(duration: float | None) -> float | None:
    """Return a distant second candidate when the primary frame is unusable."""
    if duration is None or duration <= 2.0:
        return None
    primary = representative_timestamp(duration)
    alternate = min(max(duration * 0.50, 1.0), duration - 0.1)
    return alternate if abs(alternate - primary) >= 1.0 else None


def build_cover_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    timestamp: float | None,
    video_stream_index: int | None = None,
) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-y"]
    if timestamp is not None and timestamp > 0:
        command.extend(["-ss", f"{timestamp:.3f}"])
    filters: list[str] = []
    if timestamp is not None:
        # A single timestamp can land on a fade-to-black. Pick the most
        # representative picture from the following short window while still
        # keeping generation bounded and seek-friendly.
        filters.append(f"thumbnail={COVER_THUMBNAIL_WINDOW_FRAMES}")
    filters.extend(
        [
            f"scale={COVER_WIDTH}:{COVER_HEIGHT}:force_original_aspect_ratio=decrease:"
            "flags=lanczos",
            f"pad={COVER_WIDTH}:{COVER_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
            "setsar=1",
        ]
    )
    command.extend(
        [
            "-i",
            str(source),
            "-map",
            f"0:{video_stream_index}" if video_stream_index is not None else "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            ",".join(filters),
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "3",
            "-update",
            "1",
            str(destination),
        ]
    )
    return command


def cover_is_nearly_black(ffmpeg: str, cover: Path) -> bool:
    """Reject technically valid artwork that disappears on the OLED canvas."""
    output = run_capture(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(cover),
            "-frames:v",
            "1",
            "-vf",
            "signalstats,metadata=mode=print:file=-",
            "-f",
            "null",
            "-",
        ],
        "cover luminance analysis",
    )
    values: dict[str, float] = {}
    for key in ("YAVG", "YMAX"):
        match = re.search(rf"lavfi\.signalstats\.{key}=([0-9]+(?:\.[0-9]+)?)", output)
        if match:
            values[key] = float(match.group(1))
    if len(values) != 2:
        raise TranscodeError("FFmpeg did not report cover luminance metadata.")
    return (
        values["YAVG"] <= COVER_BLACK_YAVG_MAX
        and values["YMAX"] <= COVER_BLACK_YMAX_MAX
    )


def build_attach_cover_command(
    ffmpeg: str,
    source: Path,
    cover: Path,
    destination: Path,
    attachment_index: int,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-attach",
        str(cover),
        f"-metadata:s:t:{attachment_index}",
        "mimetype=image/jpeg",
        f"-metadata:s:t:{attachment_index}",
        "filename=cover.jpg",
        str(destination),
    ]


def audio_filter(duration: float | None) -> str:
    filters = ["aresample=async=1000:first_pts=0"]
    if duration:
        value = f"{duration:.6f}"
        filters.extend([f"apad=whole_dur={value}", f"atrim=duration={value}"])
    return ",".join(filters)


def build_audio_repair_command(
    ffmpeg: str,
    source: Path,
    destination: Path,
    audio_bitrates: Sequence[int],
    duration: float | None,
    audio_stream_indices: Sequence[int],
) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
    ]
    for stream_index in audio_stream_indices:
        command.extend(["-map", f"0:{stream_index}"])
    command.extend(
        [
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        VITA_AUDIO_CODEC,
        "-profile:a",
        "aac_low",
        "-ac",
        str(VITA_AUDIO_MAX_CHANNELS),
        "-ar",
        str(VITA_AUDIO_SAMPLE_RATE),
        "-af",
        audio_filter(duration),
        "-map_metadata",
        "0",
        ]
    )
    for output_index, bitrate in enumerate(audio_bitrates):
        command.extend([f"-b:a:{output_index}", f"{bitrate}k"])
    command.append(str(destination))
    return command


def build_stream_remux_command(
    ffmpeg: str,
    video_source: Path,
    original_source: Path,
    destination: Path,
    audio_source: Path | None,
    subtitle_stream_indices: Sequence[int],
    subtitle_codecs: Sequence[str],
) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(video_source),
    ]
    if audio_source is not None:
        command.extend(["-i", str(audio_source)])
    original_input = 2 if audio_source is not None else 1
    command.extend(["-i", str(original_source), "-map", "0:v:0"])
    if audio_source is not None:
        command.extend(["-map", "1:a?"])
    for stream_index in subtitle_stream_indices:
        command.extend(["-map", f"{original_input}:{stream_index}"])
    command.extend(["-map", f"{original_input}:t?", "-c", "copy"])
    for output_index, codec in enumerate(subtitle_codecs):
        if codec in {"mov_text", "text", "webvtt"}:
            command.extend([f"-c:s:{output_index}", "srt"])
    command.extend(
        [
            "-map_metadata",
            str(original_input),
            "-map_chapters",
            str(original_input),
            "-max_muxing_queue_size",
            "4096",
            str(destination),
        ]
    )
    return command


def preflight_encoder(
    ffmpeg: str,
    source: Path,
    info: MediaInfo,
    capabilities: Capabilities,
    key: str,
    prefer_hw_decode: bool,
    width: int,
    height: int,
    fps: Fraction,
    quality: str,
    content_tune: str,
    audio_bitrate: int,
    tone_map: str,
    vaapi_device: Path,
    system_load: str,
) -> EncoderPlan | None:
    decode_modes = [True, False] if prefer_hw_decode and key != "vaapi" else [False]
    for hw_decode in decode_modes:
        command = build_command(
            ffmpeg,
            source,
            "-",
            info,
            capabilities,
            key,
            hw_decode,
            width,
            height,
            fps,
            quality,
            content_tune,
            audio_bitrate,
            tone_map,
            vaapi_device,
            preflight=True,
            system_load=system_load,
        )
        result = subprocess.run(
            prioritized_command(command, system_load),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **process_priority_kwargs(system_load),
        )
        reported_frames = [int(value) for value in re.findall(r"(?m)^frame=(\d+)$", result.stdout)]
        frame_count = reported_frames[-1] if reported_frames else 0
        probe_duration = min(PREFLIGHT_SECONDS, info.duration or PREFLIGHT_SECONDS)
        minimum_frames = max(1, int(float(fps) * probe_duration * 0.60))
        if result.returncode == 0 and frame_count >= minimum_frames:
            return EncoderPlan(key=key, codec=ENCODER_NAMES[key], hw_decode=hw_decode)
        mode = "hardware decode" if hw_decode else "software decode"
        if result.returncode == 0:
            print(
                f"Preflight rejected {ENCODER_NAMES[key]} ({mode}): encoded only "
                f"{frame_count}/{minimum_frames} required frames in {probe_duration:.1f}s.",
                file=sys.stderr,
            )
            continue
        error_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        last_line = next(
            (
                line
                for token in (
                    "Cannot create compression session",
                    "Error while opening encoder",
                    "Error while filtering",
                    "failed",
                    "error",
                )
                for line in error_lines
                if token.lower() in line.lower()
            ),
            error_lines[-1] if error_lines else "unknown FFmpeg error",
        )
        print(f"Preflight rejected {ENCODER_NAMES[key]} ({mode}): {last_line}", file=sys.stderr)
    return None


def fallback_video_plan(
    ffmpeg: str,
    source: Path,
    info: MediaInfo,
    capabilities: Capabilities,
    current: EncoderPlan,
    width: int,
    height: int,
    fps: Fraction,
    quality: str,
    content_tune: str,
    audio_bitrate: int,
    tone_map: str,
    vaapi_device: Path,
    system_load: str,
) -> EncoderPlan:
    candidates: list[str] = []
    if current.hw_decode:
        candidates.append(current.key)
    if current.key != "x264" and ENCODER_NAMES["x264"] in capabilities.encoders:
        candidates.append("x264")
    for key in candidates:
        candidate = preflight_encoder(
            ffmpeg,
            source,
            info,
            capabilities,
            key,
            False,
            width,
            height,
            fps,
            quality,
            content_tune,
            audio_bitrate,
            tone_map,
            vaapi_device,
            system_load,
        )
        if candidate is not None:
            return candidate
    raise TranscodeError(
        "The video pipeline stalled and no independently preflighted safe fallback was available."
    )


def validate_output(
    ffprobe: str,
    output: Path,
    expected_audio_count: int,
    expected_subtitle_count: int,
    expected_fps: Fraction,
    require_cover: bool,
    expected_duration: float | None = None,
    validate_audio_duration: bool = True,
    expected_attachment_count: int | None = None,
) -> dict[str, Any]:
    payload = json.loads(
        run_capture(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
            "output validation",
        )
    )
    streams = payload.get("streams") or []
    video = next(
        (
            item
            for item in streams
            if item.get("codec_type") == "video"
            and not int((item.get("disposition") or {}).get("attached_pic") or 0)
        ),
        None,
    )
    if not video or video.get("codec_name") != "h264":
        raise TranscodeError("Output validation failed: video is not H.264.")
    if video.get("profile") != "High" or int(video.get("level") or 0) not in {31, 32}:
        raise TranscodeError("Output validation failed: H.264 profile/level is not Vita-safe.")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width != OUTPUT_WIDTH or height != OUTPUT_HEIGHT:
        raise TranscodeError(f"Output validation failed: unsafe resolution {width}x{height}.")
    if video.get("pix_fmt") != "yuv420p":
        raise TranscodeError(
            f"Output validation failed: expected yuv420p, got {video.get('pix_fmt')}."
        )
    fps = fraction_from_text(video.get("avg_frame_rate")) or Fraction(0, 1)
    if fps > MAX_FPS + Fraction(1, 100):
        raise TranscodeError(f"Output validation failed: frame rate is {float(fps):.3f} fps.")
    if not fps or abs(float(fps - expected_fps)) / float(expected_fps) > 0.005:
        raise TranscodeError("Output validation failed: source frame cadence was not retained.")
    if video.get("color_space") != "bt709" or video.get("color_range") != "tv":
        raise TranscodeError("Output validation failed: video is not tagged BT.709 limited-range.")

    output_duration = stream_duration(video) or duration_seconds(
        (payload.get("format") or {}).get("duration")
    )
    duration_reference = expected_duration or output_duration
    if expected_duration is not None and output_duration is not None:
        if abs(output_duration - expected_duration) > 2.0:
            raise VideoDurationError(
                "Output validation failed: video duration is "
                f"{output_duration:.3f}s, expected {expected_duration:.3f}s."
            )

    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    if len(audio_streams) != expected_audio_count:
        raise TranscodeError("Output validation failed: one or more audio tracks are missing.")
    for track_number, audio in enumerate(audio_streams, start=1):
        if str(audio.get("codec_name") or "").lower() != VITA_AUDIO_CODEC:
            raise TranscodeError("Output validation failed: an audio track is not AAC.")
        if str(audio.get("profile") or "").upper() != VITA_AUDIO_PROFILE:
            raise TranscodeError("Output validation failed: an audio track is not AAC-LC.")
        channels = int(audio.get("channels") or 0)
        if channels < 1 or channels > VITA_AUDIO_MAX_CHANNELS:
            raise TranscodeError("Output validation failed: an AAC track is not mono or stereo.")
        if int(audio.get("sample_rate") or 0) != VITA_AUDIO_SAMPLE_RATE:
            raise TranscodeError("Output validation failed: an AAC track is not 48 kHz.")
        audio_duration = stream_duration(audio)
        if validate_audio_duration and duration_reference is not None:
            if audio_duration is None:
                raise AudioDurationError(
                    f"Output validation failed: audio track {track_number} has no duration."
                )
            if abs(audio_duration - duration_reference) > 2.0:
                raise AudioDurationError(
                    "Output validation failed: audio track "
                    f"{track_number} ends at {audio_duration:.3f}s, but video ends at "
                    f"{duration_reference:.3f}s. The incomplete file was not published."
                )

    subtitle_count = sum(item.get("codec_type") == "subtitle" for item in streams)
    if subtitle_count != expected_subtitle_count:
        raise TranscodeError("Output validation failed: one or more subtitle tracks are missing.")
    attachment_count = sum(item.get("codec_type") == "attachment" for item in streams)
    if (
        expected_attachment_count is not None
        and attachment_count != expected_attachment_count
    ):
        raise TranscodeError("Output validation failed: one or more attachments are missing.")
    format_names = str((payload.get("format") or {}).get("format_name") or "")
    if "matroska" not in format_names:
        raise TranscodeError("Output validation failed: output container is not Matroska.")
    if require_cover:
        cover = next(
            (
                item
                for item in streams
                if item.get("codec_type") == "video"
                and int((item.get("disposition") or {}).get("attached_pic") or 0)
                and item.get("codec_name") == "mjpeg"
                and str((item.get("tags") or {}).get("filename") or "").lower()
                == "cover.jpg"
                and str((item.get("tags") or {}).get("mimetype") or "").lower()
                == "image/jpeg"
            ),
            None,
        )
        if cover is None:
            raise TranscodeError("Output validation failed: embedded cover.jpg is missing.")
        if (
            int(cover.get("width") or 0) != COVER_WIDTH
            or int(cover.get("height") or 0) != COVER_HEIGHT
        ):
            raise TranscodeError(
                "Output validation failed: embedded cover has an unexpected resolution."
            )
    return payload


def default_output(source: Path) -> Path:
    return source.with_name(f"{source.stem}.vitamediadeck.mkv")


def default_batch_output(source: Path) -> Path:
    """Keep batch output separate from its source directory by default."""
    return source.with_name(f"{source.name}.vitamediadeck")


def batch_output_path(source_root: Path, output_root: Path, source: Path) -> Path:
    relative = source.relative_to(source_root)
    return (output_root / relative).with_name(f"{relative.stem}.vitamediadeck.mkv")


def partial_output_paths(output: Path) -> tuple[Path, ...]:
    """List only transient files that this transcoder itself creates."""
    return (
        output.with_name(f".{output.stem}.partial{output.suffix}"),
        output.with_name(f".{output.stem}.audio-repair.mka"),
        output.with_name(f".{output.stem}.repaired.partial{output.suffix}"),
        output.with_name(f".{output.stem}.cover.jpg"),
        output.with_name(f".{output.stem}.covered.partial{output.suffix}"),
    )


def cleanup_partial_output(output: Path) -> None:
    for temporary in partial_output_paths(output):
        if temporary.is_file():
            temporary.unlink()


def resume_output_is_valid(
    ffprobe: str,
    source: Path,
    output: Path,
    args: argparse.Namespace,
) -> bool:
    """Skip a batch member only when its completed output still meets the contract."""
    if not output.is_file():
        return False
    try:
        info = probe_media(ffprobe, source, args.force_hdr)
        max_fps = fraction_from_text(args.max_fps)
        if max_fps is None:
            return False
        selected_audio_tracks = selected_track_ordinals(
            args.audio_track,
            args.no_audio,
            info.audio_stream_count,
            "audio",
        )
        selected_subtitle_tracks = selected_track_ordinals(
            args.subtitle_track,
            args.no_subtitles,
            len(info.subtitle_codecs),
            "subtitle",
        )
        validate_output(
            ffprobe,
            output,
            len(selected_audio_tracks),
            len(selected_subtitle_tracks),
            target_fps(info.fps, max_fps),
            not args.no_cover,
            info.duration,
            expected_attachment_count=info.attachment_count,
        )
    except (TranscodeError, OSError, json.JSONDecodeError):
        return False
    return True


def load_resume_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscodeError(f"Could not read resume state: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != RESUME_STATE_VERSION:
        raise TranscodeError("The resume state is missing or uses an unsupported version.")
    if payload.get("mode") not in {"file", "batch"}:
        raise TranscodeError("The resume state has an invalid conversion mode.")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TranscodeError("The resume state does not contain any input/output entries.")
    return payload


def resume_entries(
    state_path: Path,
    mode: str,
    source: Path,
    output: Path,
) -> list[tuple[Path, Path]]:
    state = load_resume_state(state_path)
    expected_source = Path(str(state.get("input_path") or "")).expanduser().resolve()
    expected_output = Path(str(state.get("output_path") or "")).expanduser().resolve()
    if state.get("mode") != mode or source != expected_source or output != expected_output:
        raise TranscodeError(
            "The resume state does not match the selected input and output paths."
        )
    entries: list[tuple[Path, Path]] = []
    for item in state["entries"]:
        if not isinstance(item, dict):
            raise TranscodeError("The resume state contains an invalid entry.")
        item_source = Path(str(item.get("source") or "")).expanduser().resolve()
        item_output = Path(str(item.get("output") or "")).expanduser().resolve()
        if not item_source.is_file():
            raise TranscodeError(f"A saved input file no longer exists: {item_source}")
        if mode == "batch" and source not in item_source.parents:
            raise TranscodeError(f"Saved input is outside the original folder: {item_source}")
        if mode == "batch" and output not in item_output.parents:
            raise TranscodeError(f"Saved output is outside the original destination: {item_output}")
        entries.append((item_source, item_output))
    if mode == "file" and entries != [(source, output)]:
        raise TranscodeError("The resume state does not match the selected input/output file.")
    if mode == "batch" and [item_source for item_source, _ in entries] != batch_video_sources(
        source
    ):
        raise TranscodeError(
            "The files in the source folder changed after the resume state was saved."
        )
    return entries


def batch_video_sources(source: Path) -> list[Path]:
    """Return known video files below a folder in stable playback order."""
    return sorted(
        (
            candidate
            for candidate in source.rglob("*")
            if candidate.is_file()
            and not candidate.name.startswith("._")
            and candidate.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda candidate: str(candidate.relative_to(source)).casefold(),
    )


def batch_child_command(args: argparse.Namespace, source: Path, output: Path) -> list[str]:
    """Re-run the verified single-file flow for one member of a batch."""
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        str(source),
        str(output),
        "--encoder",
        args.encoder,
        "--quality",
        args.quality,
        "--content-tune",
        args.content_tune,
        "--max-fps",
        args.max_fps,
        "--audio-bitrate",
        str(args.audio_bitrate),
        "--system-load",
        args.system_load,
        "--tone-map",
        args.tone_map,
        "--vaapi-device",
        str(args.vaapi_device),
        "--ffmpeg",
        args.ffmpeg,
        "--ffprobe",
        args.ffprobe,
    ]
    if args.force_hdr:
        command.append("--force-hdr")
    if args.no_hw_decode:
        command.append("--no-hw-decode")
    if args.audio_track:
        for ordinal in args.audio_track:
            command.extend(["--audio-track", str(ordinal)])
    elif args.no_audio:
        command.append("--no-audio")
    if args.subtitle_track:
        for ordinal in args.subtitle_track:
            command.extend(["--subtitle-track", str(ordinal)])
    elif args.no_subtitles:
        command.append("--no-subtitles")
    if args.cover_image:
        command.extend(["--cover-image", str(args.cover_image)])
    elif args.no_cover:
        command.append("--no-cover")
    if args.overwrite:
        command.append("--overwrite")
    if args.dry_run:
        command.append("--dry-run")
    return command


def convert_directory(args: argparse.Namespace, source: Path) -> int:
    """Convert every supported video below source while preserving subfolders."""
    output_root = (args.output or default_batch_output(source)).expanduser().resolve()
    if output_root.suffix.lower() == ".mkv":
        raise TranscodeError("A folder input needs an output directory, not an .mkv file.")
    if output_root == source or source in output_root.parents:
        raise TranscodeError(
            "The batch output directory must be outside the input directory."
        )
    saved_entries = (
        resume_entries(args.resume_state, "batch", source, output_root)
        if args.resume_state
        else None
    )
    sources = [item_source for item_source, _ in saved_entries] if saved_entries else batch_video_sources(source)
    if not sources:
        raise TranscodeError("No supported video files were found in the selected folder or its subfolders.")
    targets: dict[Path, Path] = {}
    seen_targets: dict[str, Path] = {}
    for video, saved_target in saved_entries or [(item, None) for item in sources]:
        relative = video.relative_to(source)
        target = saved_target or batch_output_path(source, output_root, video)
        target_key = str(target).casefold()
        previous = seen_targets.get(target_key)
        if previous is not None:
            raise TranscodeError(
                "Two source files would create the same batch output: "
                f"{previous.relative_to(source)} and {relative}."
            )
        seen_targets[target_key] = video
        targets[video] = target

    print(f"Batch input: {source}", flush=True)
    print(f"Batch output: {output_root}", flush=True)
    print(f"Videos found: {len(sources)}", flush=True)
    if args.resume_state:
        print(f"Resume state: {args.resume_state.expanduser().resolve()}", flush=True)
        _, resume_ffprobe, _ = resolve_media_tools(args.ffmpeg, args.ffprobe)
    else:
        resume_ffprobe = ""
    failures: list[Path] = []
    for index, video in enumerate(sources, start=1):
        relative = video.relative_to(source)
        target = targets[video]
        print(f"\n[BATCH {index}/{len(sources)}] {relative}", flush=True)
        if args.resume_state:
            cleanup_partial_output(target)
            if resume_output_is_valid(resume_ffprobe, video, target, args):
                print(f"[BATCH {index}/{len(sources)}] SKIPPED: completed output is valid", flush=True)
                emit_progress(
                    "complete",
                    "done",
                    "resumed existing output",
                    batch_index=index,
                    batch_total=len(sources),
                    batch_source=str(relative),
                )
                continue
        child_env = {
            **os.environ,
            "VMD_BATCH_INDEX": str(index),
            "VMD_BATCH_TOTAL": str(len(sources)),
            "VMD_BATCH_SOURCE": str(relative),
        }
        result = subprocess.run(
            batch_child_command(args, video, target),
            check=False,
            env=child_env,
        )
        if result.returncode != 0:
            failures.append(relative)
            print(
                f"[BATCH {index}/{len(sources)}] FAILED: {relative}",
                file=sys.stderr,
                flush=True,
            )

    if failures:
        print(
            f"Batch completed with {len(failures)} failure(s): "
            + ", ".join(str(item) for item in failures),
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(
        f"\nBatch complete: {len(sources)} file(s) converted to {output_root}",
        flush=True,
    )
    return 0


def print_summary(
    info: MediaInfo,
    width: int,
    height: int,
    fps: Fraction,
    plan: EncoderPlan,
    content_tune: str,
    target_bps: int,
    maxrate_bps: int,
    audio_bitrates: Sequence[int],
    selected_audio_count: int,
    selected_subtitle_count: int,
    system_load: str,
    cover_description: str,
) -> None:
    print("\nVitaMediaDeck conversion plan")
    print(f"  Input:       {info.video_codec}, {info.width}x{info.height}, {float(info.fps):.3f} fps")
    print(f"  Color:       {'HDR -> BT.709 SDR' if info.hdr else 'SDR BT.709 output'}")
    print(f"  Output:      H.264 High, {width}x{height}, {float(fps):.3f} fps")
    print(f"  Encoder:     {plan.codec}")
    tuning_detail = (
        f"x264 tune={X264_CONTENT_TUNES[content_tune]}"
        if plan.key == "x264"
        else "content-specific bitrate curve; encoder-native HQ mode"
    )
    print(f"  Tuning:      {content_tune} ({tuning_detail})")
    if plan.hw_decode:
        decode_detail = "requested (FFmpeg may fall back)"
    elif info.hdr:
        encode_detail = (
            "software encode selected"
            if plan.key == "x264"
            else "hardware encode remains active"
        )
        decode_detail = f"software for stable HDR tone mapping; {encode_detail}"
    else:
        decode_detail = "disabled"
    print(f"  HW decode:   {decode_detail}")
    thread_limit = resource_thread_limit(system_load)
    load_detail = "unlimited" if thread_limit == 0 else f"up to {thread_limit} FFmpeg threads"
    print(f"  System load: {system_load} ({load_detail})")
    print(f"  Video rate:  {target_bps / 1_000_000:.1f} Mb/s target, {maxrate_bps / 1_000_000:.1f} Mb/s max")
    print(
        f"  Audio:       {selected_audio_count}/{info.audio_stream_count} selected; "
        f"AAC-LC mono/stereo 48 kHz at {', '.join(f'{rate} kb/s' for rate in audio_bitrates) or 'none'}"
    )
    print("  A/V guard:   timestamp repair, duration padding/trim, per-track validation")
    print(
        f"  Subtitles:   {selected_subtitle_count}/{len(info.subtitle_codecs)} "
        "selected track(s)"
    )
    print(f"  Cover:       {cover_description}")
    if info.duration:
        total_bps = target_bps + sum(audio_bitrates) * 1000
        estimated_bytes = info.duration * total_bps / 8
        estimated_text = (
            f"{estimated_bytes / 1_000_000_000:.2f} GB"
            if estimated_bytes >= 100_000_000
            else f"{estimated_bytes / 1_000_000:.1f} MB"
        )
        print(
            f"  Size model:  about {estimated_text} "
            "(duration x configured bitrates; resolution alone does not set size)"
        )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a video to a PS Vita-safe H.264/AAC Matroska file for VitaMediaDeck. "
            "Hardware encoding is preferred automatically."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="input video, or a folder to convert recursively",
    )
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="output .mkv for one video, or output directory for a folder",
    )
    parser.add_argument(
        "--encoder",
        choices=["auto", *ENCODER_NAMES],
        default="auto",
        help="H.264 encoder (default: auto hardware selection with x264 fallback)",
    )
    parser.add_argument(
        "--quality",
        choices=sorted(QUALITY_BPP),
        default="high",
        help="quality/size profile (default: high)",
    )
    parser.add_argument(
        "--content-tune",
        choices=CONTENT_TUNES,
        default="movie",
        help=(
            "source-specific tuning: movie uses x264 film, anime uses animation, "
            "and anime-grain preserves analog grain (default: movie)"
        ),
    )
    parser.add_argument(
        "--max-fps",
        default="60",
        help="maximum output fps as a number or rational, never above 60 (default: 60)",
    )
    parser.add_argument("--audio-bitrate", type=int, default=192, help="AAC-LC output bitrate in kb/s")
    parser.add_argument(
        "--system-load",
        choices=tuple(SYSTEM_LOAD_THREADS),
        default="balanced",
        help=(
            "desktop resource policy: low uses 2 FFmpeg threads, balanced leaves two "
            "CPU cores free (up to 8 threads), full removes limits (default: balanced)"
        ),
    )
    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument(
        "--audio-track",
        type=int,
        action="append",
        help="preserve this zero-based input audio track; repeat to select multiple (default: all)",
    )
    audio_group.add_argument(
        "--no-audio",
        action="store_true",
        help="omit every audio track",
    )
    subtitle_group = parser.add_mutually_exclusive_group()
    subtitle_group.add_argument(
        "--subtitle-track",
        type=int,
        action="append",
        help="preserve this zero-based input subtitle track; repeat to select multiple (default: all)",
    )
    subtitle_group.add_argument(
        "--no-subtitles",
        action="store_true",
        help="omit every subtitle track",
    )
    parser.add_argument(
        "--tone-map",
        choices=["mobius", "hable", "reinhard"],
        default="mobius",
        help="HDR-to-SDR tone-map algorithm (default: mobius)",
    )
    parser.add_argument("--force-hdr", action="store_true", help="tone-map even if HDR tags are missing")
    parser.add_argument(
        "--no-hw-decode",
        action="store_true",
        help="disable the initial hardware-decoding attempt; hardware encoding is unaffected",
    )
    parser.add_argument(
        "--vaapi-device",
        type=Path,
        default=Path("/dev/dri/renderD128"),
        help="Linux VAAPI render node",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable or path")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable or path")
    cover_group = parser.add_mutually_exclusive_group()
    cover_group.add_argument(
        "--cover-image",
        type=Path,
        help="use this image instead of embedded input artwork or an extracted video frame",
    )
    cover_group.add_argument(
        "--no-cover",
        action="store_true",
        help="do not generate and embed cover.jpg (enabled by default)",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    parser.add_argument(
        "--resume-state",
        type=Path,
        help="resume a saved conversion with the exact input/output paths and file list",
    )
    parser.add_argument("--dry-run", action="store_true", help="inspect and print the command only")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        source = args.input.expanduser().resolve()
        if source.is_dir():
            return convert_directory(args, source)
        if not source.is_file():
            raise TranscodeError(f"Input file not found: {source}")
        ffmpeg, ffprobe, tool_origin = resolve_media_tools(args.ffmpeg, args.ffprobe)
        print(f"FFmpeg toolchain: {tool_origin} ({ffmpeg})")
        output = (args.output or default_output(source)).expanduser().resolve()
        if source == output:
            raise TranscodeError("Input and output paths must be different.")
        if output.suffix.lower() != ".mkv":
            raise TranscodeError(
                "The output must use .mkv so all audio and subtitle track types can be preserved."
            )
        if args.resume_state:
            resume_entries(args.resume_state, "file", source, output)
            cleanup_partial_output(output)
            if output.exists():
                args.overwrite = True
        if output.exists() and not args.overwrite:
            raise TranscodeError(f"Output already exists (use --overwrite): {output}")
        custom_cover = args.cover_image.expanduser().resolve() if args.cover_image else None
        if custom_cover is not None and not custom_cover.is_file():
            raise TranscodeError(f"Cover image not found: {custom_cover}")
        cover_enabled = not args.no_cover
        if args.audio_bitrate < 64 or args.audio_bitrate > 320:
            raise TranscodeError("--audio-bitrate must be between 64 and 320 kb/s.")
        max_fps = fraction_from_text(args.max_fps)
        if max_fps is None or max_fps > MAX_FPS:
            raise TranscodeError("--max-fps must be greater than zero and no higher than 60.")

        announce_phase(1, 7, "Inspecting input media and FFmpeg capabilities...", "analysis")
        capabilities = discover_capabilities(ffmpeg)
        info = probe_media(ffprobe, source, args.force_hdr)
        selected_audio_tracks = selected_track_ordinals(
            args.audio_track,
            args.no_audio,
            info.audio_stream_count,
            "audio",
        )
        selected_subtitle_tracks = selected_track_ordinals(
            args.subtitle_track,
            args.no_subtitles,
            len(info.subtitle_codecs),
            "subtitle",
        )
        width, height = fitted_dimensions(info)
        fps = target_fps(info.fps, max_fps)
        audio_bitrates = resolved_audio_bitrates(
            ffprobe, source, info, selected_audio_tracks, args.audio_bitrate
        )
        candidates = encoder_candidates(args.encoder, capabilities, args.vaapi_device)
        # HDR tone mapping is a CPU filter chain. Keeping decoded frames in
        # VideoToolbox only to download them for zscale adds pressure to shared
        # GPU memory and has produced severe timestamp starvation on long HEVC
        # sources. Hardware encoding remains enabled and provides the main gain.
        prefer_hw_decode = not args.no_hw_decode and not info.hdr

        if info.hdr:
            # Validate the HDR filter contract before attempting encoder probes.
            filter_chain(
                info,
                width,
                height,
                fps,
                candidates[0],
                capabilities,
                args.tone_map,
            )
        emit_progress("analysis", "done")

        if args.dry_run:
            plan = EncoderPlan(
                candidates[0],
                ENCODER_NAMES[candidates[0]],
                prefer_hw_decode and candidates[0] != "vaapi",
            )
        else:
            announce_phase(
                2,
                7,
                "Testing H.264 encoders and decode paths...",
                "preflight",
            )
            plan = next(
                (
                    result
                    for key in candidates
                    if (
                        result := preflight_encoder(
                            ffmpeg,
                            source,
                            info,
                            capabilities,
                            key,
                            prefer_hw_decode,
                            width,
                            height,
                            fps,
                            args.quality,
                            args.content_tune,
                            args.audio_bitrate,
                            args.tone_map,
                            args.vaapi_device,
                            args.system_load,
                        )
                    )
                    is not None
                ),
                None,
            )
            if plan is None:
                raise TranscodeError(
                    "Every available H.264 encoder failed its three-second frame preflight."
                )
            emit_progress("preflight", "done", plan.codec)

        target_bps, maxrate_bps, _ = bitrate_plan(
            width, height, fps, args.quality, args.content_tune
        )
        if custom_cover is not None:
            cover_description = f"embedded JPEG from {custom_cover.name}"
        elif cover_enabled and info.cover_stream_index is not None:
            cover_description = f"embedded input cover ({info.cover_name})"
        elif cover_enabled:
            cover_description = "embedded JPEG from a representative frame"
        else:
            cover_description = "disabled"
        print_summary(
            info,
            width,
            height,
            fps,
            plan,
            args.content_tune,
            target_bps,
            maxrate_bps,
            audio_bitrates,
            len(selected_audio_tracks),
            len(selected_subtitle_tracks),
            args.system_load,
            cover_description,
        )

        temporary = output.with_name(f".{output.stem}.partial{output.suffix}")
        audio_temporary = output.with_name(f".{output.stem}.audio-repair.mka")
        repaired_temporary = output.with_name(f".{output.stem}.repaired.partial{output.suffix}")
        cover_file = output.with_name(f".{output.stem}.cover.jpg")
        covered_temporary = output.with_name(f".{output.stem}.covered.partial{output.suffix}")
        command = build_command(
            ffmpeg,
            source,
            str(temporary),
            info,
            capabilities,
            plan.key,
            plan.hw_decode,
            width,
            height,
            fps,
            args.quality,
            args.content_tune,
            args.audio_bitrate,
            args.tone_map,
            args.vaapi_device,
            preflight=False,
            # Video is encoded independently so timed non-video streams cannot
            # run the Matroska clock hours ahead of a slow 4K HDR filter graph.
            audio_tracks=(),
            subtitle_tracks=(),
            system_load=args.system_load,
            preserve_extras=False,
        )
        audio_command = (
            build_audio_repair_command(
                ffmpeg,
                source,
                audio_temporary,
                audio_bitrates,
                info.duration,
                tuple(
                    info.audio_stream_indices[ordinal]
                    for ordinal in selected_audio_tracks
                ),
            )
            if selected_audio_tracks
            else None
        )
        replacement_command = build_stream_remux_command(
            ffmpeg,
            temporary,
            source,
            repaired_temporary,
            audio_temporary if selected_audio_tracks else None,
            tuple(
                info.subtitle_stream_indices[ordinal]
                for ordinal in selected_subtitle_tracks
            ),
            tuple(
                info.subtitle_codecs[ordinal]
                for ordinal in selected_subtitle_tracks
            ),
        )
        print("\nCommand:")
        print(command_text(command))
        if audio_command is not None:
            print("\nIsolated audio command:")
            print(command_text(audio_command))
        print("\nFinal stream remux command:")
        print(command_text(replacement_command))
        fallback_cover_command: list[str] | None = None
        fallback_cover_reason = ""
        reject_black_cover = False
        if cover_enabled:
            if custom_cover is not None:
                cover_source = custom_cover
                cover_timestamp = None
                cover_stream_index = None
                cover_origin = f"custom artwork: {custom_cover.name}"
            elif info.cover_stream_index is not None:
                cover_source = source
                cover_timestamp = None
                cover_stream_index = info.cover_stream_index
                cover_origin = f"embedded input cover: {info.cover_name}"
                fallback_cover_command = build_cover_command(
                    ffmpeg,
                    temporary,
                    cover_file,
                    representative_timestamp(info.duration),
                )
                fallback_cover_reason = "embedded artwork is invalid or nearly black"
                reject_black_cover = True
            else:
                cover_source = temporary
                cover_timestamp = representative_timestamp(info.duration)
                cover_stream_index = None
                cover_origin = "representative frame from converted video"
                alternate_timestamp = alternate_representative_timestamp(info.duration)
                if alternate_timestamp is not None:
                    fallback_cover_command = build_cover_command(
                        ffmpeg,
                        temporary,
                        cover_file,
                        alternate_timestamp,
                    )
                    fallback_cover_reason = "primary representative window is nearly black"
                reject_black_cover = True
            cover_command = build_cover_command(
                ffmpeg,
                cover_source,
                cover_file,
                cover_timestamp,
                cover_stream_index,
            )
            attach_command = build_attach_cover_command(
                ffmpeg,
                temporary,
                cover_file,
                covered_temporary,
                info.attachment_count,
            )
            print("\nCover command:")
            print(command_text(cover_command))
            if fallback_cover_command is not None:
                print("\nCover fallback command (used if the first artwork is unusable):")
                print(command_text(fallback_cover_command))
            print("\nCover attachment command:")
            print(command_text(attach_command))
        if args.dry_run:
            return 0

        output.parent.mkdir(parents=True, exist_ok=True)
        for work_file in (
            temporary,
            audio_temporary,
            repaired_temporary,
            cover_file,
            covered_temporary,
        ):
            if work_file.exists():
                work_file.unlink()
        try:
            announce_phase(
                3,
                7,
                "Transcoding the isolated video stream with a video-only clock...",
                "transcode",
                f"VIDEO PASS // {plan.codec}",
            )

            def execute_video_attempt(active_command: Sequence[str]) -> None:
                result = run_transcode_guarded(
                    active_command,
                    args.system_load,
                    fps,
                    info.duration,
                )
                if result.returncode != 0:
                    raise TranscodeError(f"FFmpeg exited with status {result.returncode}.")
                if not temporary.is_file():
                    raise TranscodeError(
                        "FFmpeg reported success but did not create the output file."
                    )
                # Validate the video before entering the isolated audio pass or artwork
                # stages. This catches the long-HDR failure where audio reaches
                # EOF while VideoToolbox emits only a handful of video frames.
                validate_output(
                    ffprobe,
                    temporary,
                    0,
                    0,
                    fps,
                    False,
                    info.duration,
                    validate_audio_duration=False,
                    expected_attachment_count=0,
                )

            try:
                execute_video_attempt(command)
            except VideoPipelineError as exc:
                print(
                    "Warning: the selected video pipeline did not sustain valid frame output. "
                    "Retrying with an independently tested safe decode/encoder path.",
                    file=sys.stderr,
                )
                print(f"Detected: {exc}", file=sys.stderr)
                emit_progress(
                    "transcode",
                    "fallback",
                    "VIDEO RECOVERY // stable timestamps + software decode fallback",
                )
                plan = fallback_video_plan(
                    ffmpeg,
                    source,
                    info,
                    capabilities,
                    plan,
                    width,
                    height,
                    fps,
                    args.quality,
                    args.content_tune,
                    args.audio_bitrate,
                    args.tone_map,
                    args.vaapi_device,
                    args.system_load,
                )
                if temporary.exists():
                    temporary.unlink()
                command = build_command(
                    ffmpeg,
                    source,
                    str(temporary),
                    info,
                    capabilities,
                    plan.key,
                    plan.hw_decode,
                    width,
                    height,
                    fps,
                    args.quality,
                    args.content_tune,
                    args.audio_bitrate,
                    args.tone_map,
                    args.vaapi_device,
                    preflight=False,
                    audio_tracks=(),
                    subtitle_tracks=(),
                    system_load=args.system_load,
                    preserve_extras=False,
                )
                print(f"\nVideo recovery plan: {plan.codec} with software decoding")
                print(command_text(command))
                execute_video_attempt(command)

            if audio_command is not None:
                emit_progress(
                    "transcode",
                    "stage",
                    f"AUDIO PASS // {len(selected_audio_tracks)} selected AAC track(s)",
                )
                print("\nEncoding selected audio tracks independently...")
                result = run_ffmpeg(audio_command, args.system_load)
                if result.returncode != 0 or not audio_temporary.is_file():
                    raise TranscodeError("The isolated AAC pass failed.")
            emit_progress(
                "transcode",
                "stage",
                "FINAL REMUX // adding audio, subtitles, and attachments",
            )
            print("\nRemuxing video, audio, subtitles, and attachments...")
            result = run_ffmpeg(replacement_command, args.system_load)
            if result.returncode != 0 or not repaired_temporary.is_file():
                raise TranscodeError("The final stream remux failed.")
            os.replace(repaired_temporary, temporary)
            emit_progress("transcode", "done")
            announce_phase(4, 7, "Validating the transcoded media contract...", "media_validation")
            validate_output(
                ffprobe,
                temporary,
                len(selected_audio_tracks),
                len(selected_subtitle_tracks),
                fps,
                False,
                info.duration,
                expected_attachment_count=info.attachment_count,
            )
            emit_progress("media_validation", "done")
            final_temporary = temporary
            if cover_enabled:
                announce_phase(5, 7, f"Preparing cover artwork ({cover_origin})...", "cover", cover_origin)
                result = run_ffmpeg(cover_command, args.system_load)
                first_cover_failed = result.returncode != 0 or not cover_file.is_file()
                if not first_cover_failed and reject_black_cover:
                    first_cover_failed = cover_is_nearly_black(ffmpeg, cover_file)
                if (
                    first_cover_failed
                    and fallback_cover_command is not None
                ):
                    if cover_file.exists():
                        cover_file.unlink()
                    print(
                        f"Warning: {fallback_cover_reason}; using the alternate "
                        "representative video window instead.",
                        file=sys.stderr,
                    )
                    emit_progress(
                        "cover",
                        "fallback",
                        f"{fallback_cover_reason}; generating alternate frame",
                    )
                    result = run_ffmpeg(fallback_cover_command, args.system_load)
                if result.returncode != 0 or not cover_file.is_file():
                    raise TranscodeError("FFmpeg could not generate the cover image.")
                if reject_black_cover and cover_is_nearly_black(ffmpeg, cover_file):
                    raise TranscodeError(
                        "FFmpeg generated a nearly black cover twice; provide "
                        "--cover-image to choose artwork explicitly."
                    )
                emit_progress("cover", "done")
                announce_phase(
                    6,
                    7,
                    "Attaching cover and remuxing Matroska without re-encoding...",
                    "package",
                )
                result = run_ffmpeg(attach_command, args.system_load)
                if result.returncode != 0 or not covered_temporary.is_file():
                    raise TranscodeError("FFmpeg could not embed the cover image.")
                emit_progress("package", "done")
                announce_phase(7, 7, "Running final output validation...", "final_validation")
                validate_output(
                    ffprobe,
                    covered_temporary,
                    len(selected_audio_tracks),
                    len(selected_subtitle_tracks),
                    fps,
                    True,
                    info.duration,
                    expected_attachment_count=info.attachment_count,
                )
                emit_progress("final_validation", "done")
                final_temporary = covered_temporary
            else:
                print("\n[5/7] Cover disabled; artwork and remux phases skipped.", flush=True)
                emit_progress("cover", "skipped", "disabled")
                emit_progress("package", "skipped", "disabled")
                emit_progress("final_validation", "done", "validated without cover")
            os.replace(final_temporary, output)
            emit_progress("complete", "done", str(output))
        finally:
            for work_file in (
                temporary,
                audio_temporary,
                repaired_temporary,
                cover_file,
                covered_temporary,
            ):
                if work_file.exists():
                    work_file.unlink()

        print(f"\nReady: {output}")
        return 0
    except KeyboardInterrupt:
        emit_progress("cancelled", "done")
        print("\nConversion interrupted.", file=sys.stderr)
        return 130
    except (TranscodeError, OSError, json.JSONDecodeError) as exc:
        emit_progress("error", "done", str(exc))
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
