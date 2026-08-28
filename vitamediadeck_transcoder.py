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
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence


OUTPUT_WIDTH = 960
OUTPUT_HEIGHT = 544
MAX_FPS = Fraction(60, 1)
HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "hlg", "pq"}
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


class TranscodeError(RuntimeError):
    pass


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
    subtitle_codecs: tuple[str, ...]
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


def fraction_from_text(value: Any) -> Fraction | None:
    if value in (None, "", "0/0", "N/A"):
        return None
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


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
    if interlaced and fps <= Fraction(31, 1):
        fps = normalize_fps(fps * 2)

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

    duration_value = (payload.get("format") or {}).get("duration")
    try:
        duration = float(duration_value) if duration_value not in (None, "N/A") else None
    except (TypeError, ValueError):
        duration = None

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
        subtitle_codecs=tuple(
            str(item.get("codec_name") or "unknown")
            for item in streams
            if item.get("codec_type") == "subtitle"
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


def bitrate_plan(width: int, height: int, fps: Fraction, quality: str) -> tuple[int, int, int]:
    minimum, maximum = QUALITY_LIMITS[quality]
    estimated = width * height * float(fps) * QUALITY_BPP[quality]
    target = max(minimum, min(maximum, int(round(estimated / 100_000.0)) * 100_000))
    maxrate = min(14_000_000, int(round(target * 1.3 / 100_000.0)) * 100_000)
    return target, maxrate, maxrate * 2


def h264_level(width: int, height: int, fps: Fraction) -> str:
    macroblocks_per_frame = math.ceil(width / 16) * math.ceil(height / 16)
    return "3.2" if macroblocks_per_frame * float(fps) > 108_000 else "3.1"


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
        filters.append("bwdif=mode=send_field:parity=auto:deint=interlaced")

    if info.hdr:
        if "zscale" not in capabilities.filters or "tonemap" not in capabilities.filters:
            raise TranscodeError(
                "HDR input detected, but this FFmpeg build lacks zscale/tonemap. "
                "Install a full FFmpeg build compiled with --enable-libzimg; the tool "
                "will not create an incorrectly clipped or washed-out SDR file."
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
                "-coder",
                "cabac",
            ]
        )
    elif key == "nvenc":
        args.extend(
            [
                "-preset",
                "p5",
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                "19",
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
                "-crf",
                "18",
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
    audio_bitrate: int,
    tone_map: str,
    vaapi_device: Path,
    preflight: bool,
) -> list[str]:
    target_bps, maxrate_bps, bufsize_bps = bitrate_plan(width, height, fps, quality)
    level = h264_level(width, height, fps)
    gop = max(24, int(round(float(fps) * 2.0)))

    command = [ffmpeg, "-hide_banner", "-y"]
    if preflight:
        command.extend(["-loglevel", "error"])
    if encoder_key == "vaapi":
        command.extend(["-vaapi_device", str(vaapi_device)])
    if use_hw_decode and encoder_key != "vaapi":
        command.extend(["-hwaccel", "auto"])
    command.extend(["-i", str(source)])
    if preflight:
        command.extend(["-t", "1"])
    command.extend(["-map", f"0:{info.video_stream_index}"])
    if not preflight:
        command.extend(["-map", "0:a?", "-map", "0:s?", "-map", "0:t?"])

    command.extend(
        [
            "-vf",
            filter_chain(info, width, height, fps, encoder_key, capabilities, tone_map),
            "-r",
            f"{fps.numerator}/{fps.denominator}",
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
        )
    )
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
        command.extend(["-an", "-f", "null", destination])
    else:
        if info.audio_stream_count:
            command.extend(
                [
                    "-c:a",
                    "aac",
                    "-profile:a",
                    "aac_low",
                    "-b:a",
                    f"{audio_bitrate}k",
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                ]
            )
        for output_index, codec in enumerate(info.subtitle_codecs):
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
    audio_bitrate: int,
    tone_map: str,
    vaapi_device: Path,
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
            audio_bitrate,
            tone_map,
            vaapi_device,
            preflight=True,
        )
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            return EncoderPlan(key=key, codec=ENCODER_NAMES[key], hw_decode=hw_decode)
        mode = "hardware decode" if hw_decode else "software decode"
        last_line = next(
            (line.strip() for line in reversed(result.stderr.splitlines()) if line.strip()),
            "unknown FFmpeg error",
        )
        print(f"Preflight rejected {ENCODER_NAMES[key]} ({mode}): {last_line}", file=sys.stderr)
    return None


def validate_output(
    ffprobe: str,
    output: Path,
    expected_audio_count: int,
    expected_subtitle_count: int,
    expected_fps: Fraction,
) -> dict[str, Any]:
    payload = json.loads(
        run_capture(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
            "output validation",
        )
    )
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
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

    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    if len(audio_streams) != expected_audio_count:
        raise TranscodeError("Output validation failed: one or more audio tracks are missing.")
    for audio in audio_streams:
        if audio.get("codec_name") != "aac":
            raise TranscodeError("Output validation failed: an audio track is not AAC.")
        if int(audio.get("channels") or 0) > 2:
            raise TranscodeError("Output validation failed: an AAC track has more than two channels.")
        if int(audio.get("sample_rate") or 0) != 48_000:
            raise TranscodeError("Output validation failed: an AAC track is not 48 kHz.")

    subtitle_count = sum(item.get("codec_type") == "subtitle" for item in streams)
    if subtitle_count != expected_subtitle_count:
        raise TranscodeError("Output validation failed: one or more subtitle tracks are missing.")
    format_names = str((payload.get("format") or {}).get("format_name") or "")
    if "matroska" not in format_names:
        raise TranscodeError("Output validation failed: output container is not Matroska.")
    return payload


def default_output(source: Path) -> Path:
    return source.with_name(f"{source.stem}.vitamediadeck.mkv")


def print_summary(
    info: MediaInfo,
    width: int,
    height: int,
    fps: Fraction,
    plan: EncoderPlan,
    target_bps: int,
    maxrate_bps: int,
) -> None:
    print("\nVitaMediaDeck conversion plan")
    print(f"  Input:       {info.video_codec}, {info.width}x{info.height}, {float(info.fps):.3f} fps")
    print(f"  Color:       {'HDR -> BT.709 SDR' if info.hdr else 'SDR BT.709 output'}")
    print(f"  Output:      H.264 High, {width}x{height}, {float(fps):.3f} fps")
    print(f"  Encoder:     {plan.codec}")
    print(f"  HW decode:   {'requested (FFmpeg may fall back)' if plan.hw_decode else 'disabled'}")
    print(f"  Video rate:  {target_bps / 1_000_000:.1f} Mb/s target, {maxrate_bps / 1_000_000:.1f} Mb/s max")
    print(f"  Audio:       {info.audio_stream_count} AAC-LC stereo 48 kHz track(s)")
    print(f"  Subtitles:   {len(info.subtitle_codecs)} preserved track(s)")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a video to a PS Vita-safe H.264/AAC Matroska file for VitaMediaDeck. "
            "Hardware encoding is preferred automatically."
        )
    )
    parser.add_argument("input", type=Path, help="input video accepted by the installed FFmpeg")
    parser.add_argument("output", type=Path, nargs="?", help="output Matroska (.mkv) path")
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
        "--max-fps",
        default="60",
        help="maximum output fps as a number or rational, never above 60 (default: 60)",
    )
    parser.add_argument("--audio-bitrate", type=int, default=192, help="AAC bitrate in kb/s")
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
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    parser.add_argument("--dry-run", action="store_true", help="inspect and print the command only")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        ffmpeg = resolve_tool(args.ffmpeg, "FFmpeg")
        ffprobe = resolve_tool(args.ffprobe, "ffprobe")
        source = args.input.expanduser().resolve()
        if not source.is_file():
            raise TranscodeError(f"Input file not found: {source}")
        output = (args.output or default_output(source)).expanduser().resolve()
        if source == output:
            raise TranscodeError("Input and output paths must be different.")
        if output.suffix.lower() != ".mkv":
            raise TranscodeError(
                "The output must use .mkv so all audio and subtitle track types can be preserved."
            )
        if output.exists() and not args.overwrite:
            raise TranscodeError(f"Output already exists (use --overwrite): {output}")
        if args.audio_bitrate < 64 or args.audio_bitrate > 320:
            raise TranscodeError("--audio-bitrate must be between 64 and 320 kb/s.")
        max_fps = fraction_from_text(args.max_fps)
        if max_fps is None or max_fps > MAX_FPS:
            raise TranscodeError("--max-fps must be greater than zero and no higher than 60.")

        capabilities = discover_capabilities(ffmpeg)
        info = probe_media(ffprobe, source, args.force_hdr)
        width, height = fitted_dimensions(info)
        fps = target_fps(info.fps, max_fps)
        candidates = encoder_candidates(args.encoder, capabilities, args.vaapi_device)

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

        if args.dry_run:
            plan = EncoderPlan(
                candidates[0],
                ENCODER_NAMES[candidates[0]],
                not args.no_hw_decode and candidates[0] != "vaapi",
            )
        else:
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
                            not args.no_hw_decode,
                            width,
                            height,
                            fps,
                            args.quality,
                            args.audio_bitrate,
                            args.tone_map,
                            args.vaapi_device,
                        )
                    )
                    is not None
                ),
                None,
            )
            if plan is None:
                raise TranscodeError("Every available H.264 encoder failed its one-second preflight.")

        target_bps, maxrate_bps, _ = bitrate_plan(width, height, fps, args.quality)
        print_summary(info, width, height, fps, plan, target_bps, maxrate_bps)

        temporary = output.with_name(f".{output.stem}.partial{output.suffix}")
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
            args.audio_bitrate,
            args.tone_map,
            args.vaapi_device,
            preflight=False,
        )
        print("\nCommand:")
        print(command_text(command))
        if args.dry_run:
            return 0

        output.parent.mkdir(parents=True, exist_ok=True)
        if temporary.exists():
            temporary.unlink()
        try:
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                raise TranscodeError(f"FFmpeg exited with status {result.returncode}.")
            if not temporary.is_file():
                raise TranscodeError("FFmpeg reported success but did not create the output file.")
            validate_output(
                ffprobe,
                temporary,
                info.audio_stream_count,
                len(info.subtitle_codecs),
                fps,
            )
            os.replace(temporary, output)
        except BaseException:
            if temporary.exists():
                temporary.unlink()
            raise

        print(f"\nReady: {output}")
        return 0
    except KeyboardInterrupt:
        print("\nConversion interrupted.", file=sys.stderr)
        return 130
    except (TranscodeError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
