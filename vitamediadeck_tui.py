#!/usr/bin/env python3
"""Terminal user interface for VitaMediaDeck Transcoder."""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import textwrap
from collections import deque
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Sequence

try:
    import psutil
except ImportError:  # Optional outside Windows; required by requirements-tui.txt there.
    psutil = None

try:
    import curses
except ImportError as exc:  # pragma: no cover - exercised on Windows without windows-curses
    raise SystemExit(
        "The terminal UI needs the 'windows-curses' package on Windows. "
        "Install it with: py -m pip install windows-curses"
    ) from exc

import vitamediadeck_transcoder as core
import vmd_workflow as workflow
import vmd_preview as preview
import vmd_ui as ui
import vmd_audio_preview as audio_preview
import vmd_av_preview as av_preview
import vmd_progress as video_progress


TABS = ("Overview", "Streams", "Settings", "Presets", "Batch", "Log")
KEY_BACK_TAB = getattr(curses, "KEY_BTAB", 353)
VIDEO_EXTENSIONS = core.VIDEO_EXTENSIONS
PHASE_RANGES: dict[str, tuple[float, float, str]] = {
    "analysis": (0.00, 0.00, "ANALYZING INPUT"),
    "preflight": (0.00, 0.00, "TESTING ENCODER"),
    "transcode": (0.00, 1.00, "ENCODING VIDEO"),
    "media_validation": (1.00, 1.00, "CHECKING MEDIA"),
    "cover": (1.00, 1.00, "PREPARING COVER"),
    "package": (1.00, 1.00, "FINALIZING FILE"),
    "final_validation": (1.00, 1.00, "VERIFYING OUTPUT"),
    "complete": (1.00, 1.00, "COMPLETE"),
}
PHASE_ORDER = (
    "analysis",
    "preflight",
    "transcode",
    "media_validation",
    "cover",
    "package",
    "final_validation",
)
PHASE_SHORT = {
    "analysis": "SCAN",
    "preflight": "TEST",
    "transcode": "ENCODE",
    "media_validation": "CHECK",
    "cover": "COVER",
    "package": "MUX",
    "final_validation": "VERIFY",
}


@dataclass
class Settings:
    encoder: str = "auto"
    quality: str = "maximum"
    content_tune: str = "movie"
    max_fps: str = "60"
    audio_bitrate: int = core.AAC_OUTPUT_MAX_KBPS
    system_load: str = "full"
    tone_map: str = "mobius"
    force_hdr: bool = False
    hw_decode: bool = True
    cover_mode: str = "auto"
    cover_image: str = ""
    overwrite: bool = False
    vaapi_device: str = "/dev/dri/renderD128"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    audio_tracks: list[int] | None = None
    subtitle_tracks: list[int] | None = None
    encode_speed: str = "slow"
    denoise: str = "off"
    sharpen: str = "off"
    deband: str = "off"
    scaler: str = "auto"
    filter_backend: str = "auto"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Settings":
        allowed = {item.name for item in fields(cls)}
        if 'filter_backend' not in payload:
            payload = {**payload, 'filter_backend': 'cpu', 'scaler': payload.get('scaler', 'lanczos')}
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def command(
        self,
        script: Path,
        source: Path,
        output: Path,
        audio_tracks: Sequence[int] | None = None,
        subtitle_tracks: Sequence[int] | None = None,
        resume_state: Path | None = None,
        batch_plan: Path | None = None,
    ) -> list[str]:
        if audio_tracks is None:
            audio_tracks = self.audio_tracks
        if subtitle_tracks is None:
            subtitle_tracks = self.subtitle_tracks
        command = [
            sys.executable,
            "-u",
            str(script),
            str(source),
            str(output),
            "--encoder",
            self.encoder,
            "--quality",
            self.quality,
            "--content-tune",
            self.content_tune,
            "--max-fps",
            self.max_fps,
            "--audio-bitrate",
            str(self.audio_bitrate),
            "--system-load",
            self.system_load,
            "--tone-map",
            self.tone_map,
            "--vaapi-device",
            self.vaapi_device,
            "--ffmpeg",
            self.ffmpeg,
            "--ffprobe",
            self.ffprobe,
        ]
        command.extend(core.video_option_arguments(self))
        if self.force_hdr:
            command.append("--force-hdr")
        if not self.hw_decode:
            command.append("--no-hw-decode")
        if audio_tracks is not None:
            if audio_tracks:
                for ordinal in sorted(audio_tracks):
                    command.extend(["--audio-track", str(ordinal)])
            else:
                command.append("--no-audio")
        if subtitle_tracks is not None:
            if subtitle_tracks:
                for ordinal in sorted(subtitle_tracks):
                    command.extend(["--subtitle-track", str(ordinal)])
            else:
                command.append("--no-subtitles")
        if self.cover_mode == "none":
            command.append("--no-cover")
        elif self.cover_mode == "custom":
            command.extend(["--cover-image", self.cover_image])
        if self.overwrite:
            command.append("--overwrite")
        if resume_state is not None:
            command.extend(["--resume-state", str(resume_state)])
        if batch_plan is not None:
            command.extend(["--batch-plan", str(batch_plan)])
        return command


@dataclass
class LiveProgress:
    phase: str = "idle"
    phase_label: str = "IDLE"
    detail: str = ""
    phase_ratio: float = 0.0
    local_ratio: float = 0.0
    overall_ratio: float = 0.0
    batch_index: int = 0
    batch_total: int = 0
    batch_source: str = ""
    frame: str = "-"
    fps: str = "-"
    quality: str = "-"
    size: str = "-"
    bitrate: str = "-"
    speed: str = "-"
    media_seconds: float = 0.0
    video_fps: float = 0.0
    video_finished: bool = False
    media_duration: float | None = None
    eta_seconds: float | None = None
    started_at: float = 0.0


BUILTIN_PRESETS: dict[str, Settings] = {
    "Vita HW - Max perceptual": Settings(quality="maximum"),
    "Vita HW - Medium": Settings(quality="balanced", audio_bitrate=160),
    "Vita HW - Light": Settings(quality="compact", max_fps="30", audio_bitrate=128),
    "Vita SW - Max perceptual": Settings(encoder="x264", content_tune="vita-sw", max_fps="30", quality="maximum"),
    "Vita SW - Medium": Settings(encoder="x264", content_tune="vita-sw", max_fps="30", quality="balanced", audio_bitrate=160),
    "Vita SW - Light": Settings(encoder="x264", content_tune="vita-sw", max_fps="30", quality="compact", audio_bitrate=128),
    "Vita Movie Max": Settings(encoder="x264", content_tune="movie"),
    "Vita Anime Max": Settings(encoder="x264", content_tune="anime"),
    "Vita Anime Grain": Settings(encoder="x264", content_tune="anime-grain"),
    "Balanced": Settings(quality="balanced"),
    "Compact 30 fps": Settings(quality="compact", max_fps="30", audio_bitrate=160),
    "Computer CPU Compatibility": Settings(encoder="x264", hw_decode=False),
    "HDR Highlight Detail": Settings(tone_map="hable"),
    "Mac Fast Hardware": Settings(encoder="videotoolbox", quality="balanced"),
    "CPU Maximum Detail": Settings(encoder="x264", quality="maximum", encode_speed="veryslow"),
    "Film Preserve Grain": Settings(encoder="x264", content_tune="film-grain", quality="maximum"),
    "Noisy Film Gentle Clean": Settings(denoise="light", quality="high"),
    "Noisy Source Compact": Settings(denoise="medium", quality="compact", max_fps="30", audio_bitrate=160),
    "Animation Smooth Gradients": Settings(content_tune="anime", deband="light", sharpen="light"),
    "Soft Source Gentle Sharpen": Settings(sharpen="light", scaler="spline"),
    "Mac GPU Gentle Clean": Settings(encoder="videotoolbox", filter_backend="apple-gpu", denoise="light", sharpen="light"),
}

LEGACY_PRESETS = {
    "vita perceptual max": Settings(quality="high", audio_bitrate=192, system_load="balanced"),
    "software compatibility": Settings(encoder="x264", hw_decode=False, quality="high", audio_bitrate=192, system_load="balanced"),
}


def config_directory() -> Path:
    system = platform.system()
    if system == "Windows":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "VitaMediaDeck Transcoder"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "VitaMediaDeck Transcoder"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "vitamediadeck-transcoder"


class PresetStore:
    def __init__(self) -> None:
        self.path = config_directory() / "presets.json"
        self.custom: dict[str, Settings] = {}
        self.error = ""
        self.load()

    def load(self) -> None:
        self.custom = {}
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("preset file must contain a JSON object")
            self.custom = {
                str(name): Settings.from_dict(value)
                for name, value in payload.items()
                if isinstance(value, dict)
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.error = f"Could not load presets: {exc}"

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {name: asdict(settings) for name, settings in sorted(self.custom.items())},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def rows(self) -> list[tuple[str, bool, Settings]]:
        rows = [(name, False, settings) for name, settings in BUILTIN_PRESETS.items()]
        rows.extend((name, True, settings) for name, settings in sorted(self.custom.items()))
        return rows

    def save(self, name: str, settings: Settings) -> None:
        if not name.strip():
            raise ValueError("Preset name cannot be empty.")
        if name in BUILTIN_PRESETS:
            raise ValueError("Built-in presets cannot be replaced.")
        self.custom[name.strip()] = Settings.from_dict(asdict(settings))
        self.persist()

    def delete(self, name: str) -> None:
        del self.custom[name]
        self.persist()


def duration_text(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def bytes_text(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "unknown"


def tag_text(stream: dict[str, Any]) -> str:
    tags = stream.get("tags") or {}
    language = tags.get("language") or "und"
    title = tags.get("title")
    return f"{language}{f' / {title}' if title else ''}"


class TerminalApp:
    SETTING_ROWS = (
        ("Encoder", "encoder", ("auto", "videotoolbox", "nvenc", "amf", "vaapi", "x264")),
        ("Quality", "quality", ("compact", "balanced", "high", "maximum")),
        ("Content tuning", "content_tune", core.CONTENT_TUNES),
        ("Maximum FPS", "max_fps", ("12", "15", "20", "24000/1001", "24", "25", "30000/1001", "30", "50", "60000/1001", "60")),
        ("Audio bitrate", "audio_bitrate", core.AUDIO_BITRATE_STEPS),
        ("System load", "system_load", ("low", "balanced", "full")),
        ("Tone map", "tone_map", ("mobius", "hable", "reinhard")),
        ("Force HDR", "force_hdr", (False, True)),
        ("Hardware decode", "hw_decode", (False, True)),
        ("Cover", "cover_mode", ("none", "auto", "custom")),
        ("Cover image", "cover_image", None),
        ("Overwrite output", "overwrite", (False, True)),
        ("VAAPI device", "vaapi_device", None),
        ("FFmpeg", "ffmpeg", None),
        ("ffprobe", "ffprobe", None),
        ("x264 compression effort", "encode_speed", core.VIDEO_OPTION_CHOICES['encode_speed']),
        ("Noise reduction", "denoise", core.VIDEO_OPTION_CHOICES['denoise']),
        ("Sharpening", "sharpen", core.VIDEO_OPTION_CHOICES['sharpen']),
        ("Colour band reduction", "deband", core.VIDEO_OPTION_CHOICES['deband']),
        ("Resize method", "scaler", core.VIDEO_OPTION_CHOICES['scaler']),
        ("Filter processing", "filter_backend", core.VIDEO_OPTION_CHOICES['filter_backend']),
    )
    SETTING_HELP = {
        "encoder": "AUTO measures valid CPU/GPU pipelines. An explicit encoder stays fixed.",
        "quality": "Controls the target bitrate envelope: HIGH, BALANCED, or COMPACT.",
        "content_tune": "MOVIE uses x264 film; ANIME protects line art; ANIME-GRAIN preserves analog grain.",
        "max_fps": "Preserves source cadence up to this ceiling. Vita output never exceeds 60 fps.",
        "audio_bitrate": "AAC-LC ceiling per track; output never targets more than the measured source rate.",
        "system_load": "BALANCED leaves two CPU cores free (up to 8 threads); LOW uses 2; FULL is unlimited.",
        "tone_map": "HDR-to-SDR curve. MOBIUS is neutral; HABLE protects highlights.",
        "force_hdr": "Treat untagged input as HDR and force the SDR tone-mapping pipeline.",
        "hw_decode": "Attempt hardware decode before software fallback; encoding remains independent.",
        "cover_mode": "AUTO keeps embedded artwork first, then falls back to an SDR frame.",
        "cover_image": "Path to custom artwork; it will be normalized to a 480x272 JPEG.",
        "overwrite": "Allow the validated output to replace an existing target file.",
        "vaapi_device": "Linux VAAPI render node used by AMD or Intel hardware encoding.",
        "ffmpeg": "FFmpeg executable name or absolute path.",
        "ffprobe": "ffprobe executable name or absolute path.",
    }

    def __init__(
        self,
        screen: Any,
        source: Path | None,
        output: Path | None,
        initial: Settings,
        resume_state: Path | None = None,
    ) -> None:
        self.screen = screen
        self.script = Path(__file__).with_name("vitamediadeck_transcoder.py").resolve()
        self.settings = initial
        self.resume_state_path = resume_state.resolve() if resume_state else None
        self.resume_saved_path: Path | None = None
        self.presets = PresetStore()
        self.input_path = source.resolve() if source else None
        self.output_path = output.resolve() if output else None
        if self.input_path and self.output_path is None:
            self.output_path = (
                core.default_batch_output(self.input_path)
                if self.input_path.is_dir()
                else core.default_output(self.input_path)
            )
        if self.input_path and self.input_path.is_file() and self.output_path and not resume_state and self.output_path != self.input_path:
            self.output_path = workflow.output_file(self.output_path).resolve()
        self.input_payload: dict[str, Any] | None = None
        self.output_payload: dict[str, Any] | None = None
        self.info: core.MediaInfo | None = None
        self.capabilities: core.Capabilities | None = None
        self.batch_sources: list[Path] = []
        self.batch_settings: dict[Path, Settings] = {}
        self.batch_selection_index = 0
        self.batch_edit_source: Path | None = None
        self.batch_plan_path: Path | None = None
        self.batch_enabled: dict[Path, bool] = {}
        self.batch_metadata: dict[Path, dict[str, Any]] = {}
        self.batch_weights = (0.0, 1.0, 1.0)
        self.preview_backend = os.environ.get("VMD_DETECTED_PREVIEW", "unicode")
        self.preview_samples: dict[Path, int | None] = {}
        self.inline_request = None
        self.inline_rendered = None
        self.inline_surface = None
        self.audio_preview = None
        self.audio_segments = {}
        self.audio_sample_indices = {}
        self.audio_track_indices = {}
        self.audio_signature = None
        self.av_inline = None
        self.av_inline_key = None
        self.av_inline_started = 0
        self.editor_primary = True
        self.pause_started: float | None = None
        self.active_entries: list[dict[str, Any]] | None = None
        if self.resume_state_path and self.input_path and self.input_path.is_dir():
            self.restore_batch_settings(self.resume_state_path)
        self.probe_error = ""
        self.status = self.presets.error or "Choose an input video or folder with I."
        self.tab = 0
        self.setting_index = 0
        self.preset_index = 0
        self.track_selection_index = 0
        self.track_choices: list[tuple[str, int]] = []
        self.selected_audio_tracks: set[int] = set()
        self.selected_subtitle_tracks: set[int] = set()
        self.selection_source: Path | None = None
        self.stream_cursor_line = 0
        self.scroll = {name: 0 for name in TABS}
        self.logs: deque[str] = deque(maxlen=4000)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.paused = False
        self.suspended_processes: list[Any] = []
        self.worker: threading.Thread | None = None
        self.progress_seconds: float | None = None
        self.live = LiveProgress()
        self.frame = 0
        self.running = True
        self._configure_screen()

    def restore_batch_settings(self, state_path: Path) -> None:
        """Restore per-file TUI options carried by an interrupted folder conversion."""
        try:
            state = core.load_resume_state(state_path)
        except core.TranscodeError:
            return
        for item in state.get("entries") or []:
            if not isinstance(item, dict) or not isinstance(item.get("settings"), dict):
                continue
            source = Path(str(item.get("source") or "")).expanduser().resolve()
            self.batch_settings[source] = Settings.from_dict(item["settings"])
            self.batch_enabled[source] = item.get("enabled", True)

    def active_settings(self) -> Settings:
        if self.batch_edit_source is None:
            return self.settings
        if self.batch_edit_source not in self.batch_sources:
            self.batch_edit_source = None
            return self.settings
        if self.batch_edit_source not in self.batch_settings:
            self.batch_settings[self.batch_edit_source] = Settings.from_dict(asdict(self.settings))
        return self.batch_settings[self.batch_edit_source]

    def settings_for_batch_source(self, source: Path) -> Settings:
        return self.batch_settings.get(source, self.settings)

    def reset_batch_source_settings(self, source: Path) -> None:
        self.batch_settings.pop(source, None)

    def _configure_screen(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.screen.timeout(100)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            import vmd_theme
            vmd_theme.configure(curses)
            self.screen.bkgd(" ", self.color(9))

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if curses.has_colors() else 0

    def add(self, y: int, x: int, value: Any, style: int = 0, width: int | None = None) -> None:
        height, columns = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= columns:
            return
        text = str(value).replace("\t", "    ")
        available = max(0, columns - x - 1)
        if width is not None:
            available = min(available, max(0, width))
        if available:
            try:
                # Python curses counts the addnstr limit as encoded bytes on
                # some terminals, which cuts multi-byte HUD glyphs in half.
                # Slice Unicode text first and let curses write it atomically.
                self.screen.addstr(y, x, text[:available], style)
            except curses.error:
                pass

    def box(
        self,
        top: int,
        left: int,
        height: int,
        width: int,
        title: str = "",
        accent: int = 1,
    ) -> None:
        if height < 2 or width < 2:
            return
        style = self.color(accent)
        heading = f" {title.upper()} " if title else ""
        lead = "╭─◢" + heading
        trail = "┐"
        lattice = "·" * max(0, width - len(lead) - len(trail))
        self.add(top, left, lead + lattice + trail, style | curses.A_BOLD, width)
        for row in range(top + 1, top + height - 1):
            edge = "│" if row in (top + 1, top + height - 2) else "┊"
            self.add(row, left, edge, style)
            self.add(row, left + width - 1, edge, style)
        self.add(top + height - 1, left, "╰" + "·" * (width - 3) + "◣", style, width)

    def fill(self, y: int, x: int, width: int, style: int) -> None:
        self.add(y, x, " " * max(0, width), style, width)

    def meter(
        self,
        y: int,
        x: int,
        width: int,
        ratio: float,
        active: int = 3,
    ) -> None:
        width = max(4, width)
        ratio = max(0.0, min(1.0, ratio))
        core_width = max(1, width - 2)
        filled = int(round(core_width * ratio))
        self.add(y, x, "‹", self.color(7) | curses.A_BOLD)
        self.add(y, x + 1, "━" * filled, self.color(active) | curses.A_BOLD, core_width)
        self.add(y, x + 1 + filled, "┈" * (core_width - filled), self.color(8), core_width - filled)
        if 0 < filled < core_width:
            self.add(y, x + filled, "◆", self.color(13) | curses.A_BOLD)
        self.add(y, x + width - 1, "›", self.color(7) | curses.A_BOLD)

    def centered(self, y: int, left: int, width: int, text: str, style: int = 0) -> None:
        self.add(y, left + max(0, (width - len(text)) // 2), text, style, width)

    def signal_carrier(
        self,
        y: int,
        left: int,
        width: int,
    ) -> None:
        """Draw a static particle data bus; motion is reserved for active systems."""
        if width < 4:
            return
        dust = "·" * width
        self.add(y, left, dust, self.color(8), width)
        self.add(y, left, "◇", self.color(13) | curses.A_BOLD)
        if width >= 28:
            center = left + width // 2
            self.add(y, center - 4, "˙ · ◈ · ˙", self.color(7) | curses.A_BOLD, 9)
        if width >= 52:
            self.add(y, left + width // 4, "˙•˙", self.color(4), 3)
            self.add(y, left + (width * 3) // 4, "˙•˙", self.color(4), 3)
        self.add(y, left + width - 1, "◇", self.color(13) | curses.A_BOLD)

    def draw_particle_visualization(
        self,
        top: int,
        left: int,
        width: int,
        ratio: float,
        active: bool,
        compact: bool = False,
    ) -> None:
        """Render a localized point-cloud core; it moves only during conversion."""
        ratio = max(0.0, min(1.0, ratio))
        percent = f"{ratio * 100:05.1f}%"
        phase = (self.frame // 4) % 5 if active else 0
        clouds = (
            "·  ˙   •", "˙ ·  ◦  ", "•   · ˙ ", "  ◦ ·  ˙", "˙  •  · ",
        )
        left_cloud = clouds[phase]
        right_cloud = clouds[(-phase - 1) % len(clouds)]

        if compact or width < 42:
            rows = (
                (f"{left_cloud}  ╭··◇··╮  {right_cloud}", 7, False),
                (f"·  ‹ ◈ {percent} ◈ ›  ·", 3, True),
                (f"{right_cloud}  ╰··◇··╯  {left_cloud}", 4, False),
            )
        else:
            rows = (
                (f"{left_cloud}       ˙       {right_cloud}", 8, False),
                (f"  {clouds[(phase + 1) % 5]}   · ˙ • ˙ ·   {clouds[(-phase - 2) % 5]}", 4, False),
                (f"{clouds[(phase + 2) % 5]}   ╭···◇···╮   {clouds[(-phase - 3) % 5]}", 7, False),
                (f"{clouds[(phase + 3) % 5]}  ‹ · ◈ {percent} ◈ · ›  {clouds[(-phase - 4) % 5]}", 3, True),
                (f"{clouds[(phase + 4) % 5]}   ╰···◇···╯   {clouds[(-phase - 5) % 5]}", 13, False),
                (f"  {clouds[phase]}   · ˙ • ˙ ·   {clouds[(-phase - 1) % 5]}", 4, False),
                (f"{right_cloud}       ˙       {left_cloud}", 8, False),
            )

        for row, (line, pair, bold) in enumerate(rows):
            style = self.color(pair) | (curses.A_BOLD if bold else 0)
            start = left + max(0, (width - len(line)) // 2)
            self.add(top + row, start, line, style, width)
            marker = line.find(percent)
            if marker >= 0:
                self.add(
                    top + row,
                    start + marker,
                    percent,
                    self.color(11) | curses.A_BOLD,
                    len(percent),
                )

    def tape_transport(
        self,
        y: int,
        left: int,
        width: int,
        active: bool,
    ) -> None:
        """Render the only ambient animation: a reel-to-reel encode transport."""
        if not active:
            self.add(
                y,
                left,
                "READY TO CONVERT",
                self.color(8) | curses.A_BOLD,
                width,
            )
            return
        if self.paused:
            ratio_text = f"{self.live.overall_ratio * 100:05.1f}%"
            line = f"Ⅱ PAUSED  [◴]{'─' * max(5, width - 31)}[◴]  {ratio_text}"
            self.add(y, left, line, self.color(7) | curses.A_BOLD, width)
            return
        reel_frames = ("◴", "◷", "◶", "◵")
        step = (self.frame // 2) % len(reel_frames)
        left_reel = reel_frames[step]
        right_reel = reel_frames[-step % len(reel_frames)]
        ratio_text = f"{self.live.overall_ratio * 100:05.1f}%"
        prefix = f"● REC  [{left_reel}]"
        suffix = f"[{right_reel}]  {ratio_text}"
        belt_width = max(5, width - len(prefix) - len(suffix) - 2)
        belt = ["─"] * belt_width
        for particle in range(2, belt_width, 7):
            belt[particle] = "·"
        belt[(self.frame // 2) % belt_width] = "◆"
        line = f"{prefix}{''.join(belt)}{suffix}"
        self.add(y, left, line, self.color(13) | curses.A_BOLD, width)
        self.add(y, left, "● REC", self.color(11) | curses.A_BOLD)
        self.add(
            y,
            left + max(0, min(width - len(ratio_text), len(line) - len(ratio_text))),
            ratio_text,
            self.color(14) | curses.A_BOLD,
            len(ratio_text),
        )

    def draw_particle_strip(
        self,
        y: int,
        left: int,
        width: int,
        active: bool,
    ) -> None:
        """Add a compact point-cloud signature without animating the frame."""
        if width < 88:
            return
        patterns = (
            "· ˙ • ◦ ◈ ◦ • ˙ ·",
            "˙ • ◦ ◈ ◦ • ˙ ·  ",
            "• ◦ ◈ ◦ • ˙ ·  ˙ ",
            "◦ ◈ ◦ • ˙ ·  ˙ • ",
        )
        phase = (self.frame // 4) % len(patterns) if active else 0
        label = patterns[phase]
        self.add(
            y,
            left + width - len(label),
            label,
            self.color(7) | curses.A_BOLD,
            len(label),
        )

    def run(self) -> None:
        if self.input_path:
            self.probe_current()
            recovery = workflow.find_recovery(self.output_path, self.input_path.is_dir()) if self.output_path else None
            if not self.resume_state_path and recovery and recovery.is_file():
                question = ('Legacy conversion found. Resume with its original saved paths?' if workflow.legacy_state(recovery)
                            else 'Saved conversion found. Restore its queue and settings?')
                if self.confirm(question):
                    state = core.load_resume_state(recovery)
                    self.resume_state_path = recovery
                    self.output_path = Path(state['output_path']).expanduser().resolve()
                    if state.get("settings"):
                        self.settings = Settings.from_dict(state["settings"])
                    self.restore_batch_settings(recovery)
                    order = [Path(item["source"]) for item in state["entries"]]
                    if self.input_path.is_dir():
                        self.batch_sources = order
            if self.resume_state_path and self.input_path.is_file():
                entry = core.load_resume_state(self.resume_state_path)["entries"][0]
                if "audio_tracks" in entry:
                    self.selected_audio_tracks = set(entry["audio_tracks"])
                if "subtitle_tracks" in entry:
                    self.selected_subtitle_tracks = set(entry["subtitle_tracks"])
            elif self.resume_state_path and self.input_path.is_dir():
                self.batch_sources = [Path(item["source"]) for item in core.load_resume_state(self.resume_state_path)["entries"]]
        try:
            av_preview.run_view(self, primary=True)
        finally:
            if self.audio_preview:
                self.audio_preview.close()
            if self.av_inline:
                self.av_inline.close()
            self.delete_inline_image()

    def draw(self) -> None:
        self.screen.erase()
        self.frame += 1
        height, width = self.screen.getmaxyx()
        for row in range(height):
            self.fill(row, 0, width - 1, self.color(9))
        if height < 22 or width < 76:
            if self.inline_rendered is not None:
                self.delete_inline_image()
                self.screen.clearok(True)
            self.inline_surface = None
            self.add(0, 2, "△ VITA MEDIA DECK", self.color(13) | curses.A_BOLD)
            self.signal_carrier(1, 2, max(1, width - 5))
            self.add(3, 2, "TERMINAL TOO SMALL", self.color(5) | curses.A_BOLD)
            self.add(5, 2, "Resize terminal to at least 76 x 22.", self.color(9))
            self.add(6, 2, f"Current grid: {width} x {height}", self.color(8))
            self.screen.refresh()
            return
        active = bool(self.process and self.process.poll() is None)
        state = "PAUSED" if active and self.paused else "CONVERTING" if active else "READY"
        self.add(0, 2, "△ VITA MEDIA DECK", self.color(13) | curses.A_BOLD)
        self.add(0, 22, "VIDEO TRANSCODER", self.color(8))
        state_style = self.color(7) if self.paused else self.color(11) if active else self.color(13)
        self.add(0, width - len(state) - 5, f"◈ {state}", state_style | curses.A_BOLD)
        self.signal_carrier(1, 1, width - 3)
        source_name = self.input_path.name if self.input_path else "NO INPUT SELECTED"
        target_name = self.output_path.name if self.output_path else "NO OUTPUT SELECTED"
        self.add(2, 2, f"IN  › {source_name}", self.color(8), max(10, width // 2 - 4))
        self.add(2, width // 2, f"OUT › {target_name}", self.color(8), width // 2 - 3)
        cursor = 2
        compact_names = ("HOME", "STREAMS", "SET", "PRESET", "BATCH", "LOG")
        for index, name in enumerate(TABS):
            nav_name = compact_names[index] if width < 110 else name.upper()
            label = f" {index + 1:02d}·{nav_name} "
            style = self.color(2) | curses.A_BOLD if index == self.tab else self.color(12)
            self.add(4, cursor, label, style)
            cursor += len(label) + 2
        self.add(5, 2, "V AV editor | [ ] Previous/next sample | < > Skip 10 | Tab Pages", self.color(4), width - 4)
        content_top = 6
        content_height = height - 11
        module_title = f"{self.tab + 1:02d} / {TABS[self.tab]}"
        self.box(content_top, 1, content_height, width - 2, module_title, 12)
        editor_width = width - 6
        editor_height = content_height - 2
        self.inline_surface = None
        embedded = self.tab in (1, 2, 3, 4) and self.input_path and editor_height >= 16
        if embedded:
            preferred_height = {
                1: 14,
                2: len(self.SETTING_ROWS) + 8,
                3: max(9, len(self.presets.rows()) + 3),
                4: max(9, min(16, len(self.batch_sources) + 6)),
            }[self.tab]
            # Parameter names, values and descriptions keep the full width.
            # Reserve a separate, non-overlapping image area underneath.
            reserved_preview_height = max(6, min(14, int(editor_height * .4 + .5)))
            editor_height = min(preferred_height, max(9, editor_height - reserved_preview_height - 1))
            preview_top = content_top + 1 + editor_height + 1
            preview_height = content_height - 2 - editor_height - 1
            self.draw_inline_preview(preview_top, 3, preview_height, editor_width)
        elif self.inline_rendered is not None:
            self.delete_inline_image()
            self.screen.clearok(True)
        if self.tab in (1, 2, 3, 4) and self.input_path and not embedded:
            self.add(5, 2, "V Full preview | Increase window height for the preview below the menu", self.color(4), width - 4)
        if self.tab == 0:
            self.draw_overview(content_top + 1, 3, content_height - 2, width - 6)
        elif self.tab == 1:
            self.draw_streams(content_top + 1, 3, editor_height, editor_width)
        elif self.tab == 2:
            self.draw_settings(content_top + 1, 3, editor_height, editor_width)
        elif self.tab == 3:
            self.draw_presets(content_top + 1, 3, editor_height, editor_width)
        elif self.tab == 4:
            self.draw_batch(content_top + 1, 3, editor_height, editor_width)
        else:
            self.draw_log(content_top + 1, 3, content_height - 2, width - 6)
        status_style = self.color(5) if any(token in self.status.lower() for token in ("fail", "error", "could not")) else self.color(4)
        self.add(height - 4, 2, "◢ STATUS", status_style | curses.A_BOLD)
        self.add(height - 4, 11, self.status.upper(), status_style, width - 13)
        self.signal_carrier(height - 2, 1, width - 3)
        footer = (
            " I Input  O Output  U Update  V Preview  T Transcode  P Pause/Resume  A Abort  Tab Pages  Q Exit "
            if width >= 108
            else " I Input O Output U Update V Preview T Run P Pause A Abort Tab Pages Q Exit "
        )
        self.fill(height - 1, 0, width - 1, self.color(6))
        self.add(height - 1, 2, footer, self.color(6) | curses.A_BOLD, width - 4)
        self.screen.refresh()
        self.paint_inline_image()

    def preview_subject(self):
        pinned = getattr(self, '_conversion_preview_subject', None)
        if self.worker and self.worker.is_alive() and pinned:
            return pinned
        if not self.input_path:
            return None, self.settings
        if not self.input_path.is_dir():
            return self.input_path, self.settings
        if self.worker and self.worker.is_alive() and self.live.batch_source:
            source = self.input_path / self.live.batch_source
            return source, self.settings_for_batch_source(source)
        if not self.batch_sources:
            return None, self.settings
        source = self.batch_sources[min(self.batch_selection_index, len(self.batch_sources) - 1)]
        if self.tab in (2, 3) and self.batch_edit_source in self.batch_sources:
            source = self.batch_edit_source
        elif self.tab in (2, 3):
            return source, self.settings
        return source, self.settings_for_batch_source(source)

    def delete_inline_image(self):
        if self.preview_backend == "kitty":
            sys.stdout.write("\x1b_Ga=d,d=I,i=31415,q=2\x1b\\")
            sys.stdout.flush()
        self.inline_rendered = None

    def draw_inline_preview(self, top, left, height, width):
        source, settings = self.preview_subject()
        self.add(top, left, "Video preview · encoded frame", self.color(4), width)
        if source is None:
            self.add(top + 1, left, "Select a video first.", self.color(8), width)
            return
        index = self.preview_samples.get(source)
        image_rows = max(1, height - 4)
        rows = max(1, min(image_rows, int((width - 2) * 544 / 960 / 2 + .5)))
        columns = max(4, min(width - 2, int(rows * 2 * 960 / 544 + .5)))
        image_top = top + 2 + (image_rows - rows) // 2
        image_left = left + (width - columns) // 2
        pw = columns if self.preview_backend == "unicode" else min(1920, columns * 8)
        ph = rows * 2 if self.preview_backend == "unicode" else max(2, pw * 544 // 960)
        from types import SimpleNamespace
        try:
            if self.input_path.is_dir():
                metadata = self.batch_metadata.get(source, {})
                info = SimpleNamespace(audio_stream_count=metadata.get("audio_count", 0),
                                       subtitle_codecs=[None] * metadata.get("subtitle_count", 0))
            else:
                info = self.info
            audio, subtitles = av_preview.selection(self, settings, info) if info else ([], [])
            stat = source.stat()
            key = (source, stat.st_size, stat.st_mtime_ns, json.dumps(asdict(settings), sort_keys=True), index, tuple(audio), tuple(subtitles), pw, ph)
            if key != self.av_inline_key:
                if self.av_inline:
                    self.av_inline.close(wait=False)
                self.av_inline = av_preview.Clip(source, asdict(settings), index, audio, subtitles, pw, ph, self.preview_backend)
                self.av_inline_key = key
                self.av_inline_started = 0
            clip = self.av_inline
            error = clip.error
            frame = None
            if clip.ready():
                position = clip.position()
                number = min(len(clip.frames) - 1, int(position * clip.display_fps))
                caption = " / ".join(av_preview.cue_at(lane.cues, position) for lane in clip.subtitle_lanes if lane.enabled)
                frame = SimpleNamespace(timestamp=clip.start + position, encoder=clip.encoder,
                                        payload=clip.frames[number], number=number, caption=caption)
        except OSError as exc:
            key, frame, error = None, None, str(exc)
        surface_key = (key, self.tab, image_top, image_left, columns, rows)
        if surface_key != self.inline_request:
            self.delete_inline_image()
            self.screen.clearok(True)
            self.inline_request = surface_key
        self.add(top, left, f"Converted clip · muted · V opens synchronized AV editor · {source.name}", self.color(4), width)
        label = "Default · 20% of video" if index is None else f"Sample {index + 1}/100 · {index * 100 / 99:.1f}%"
        if frame:
            label += f" · {frame.timestamp:.2f}s · {frame.encoder} · {settings.quality}"
            self.inline_surface = ((surface_key, frame.number), frame, image_top, image_left, columns, rows)
            if frame.caption:
                label += " | " + frame.caption
        elif error:
            for offset, line in enumerate(textwrap.wrap(error, max(1, width))[:image_rows]):
                self.add(top + 2 + offset, left, line, self.color(5), width)
        else:
            self.add(top + 2, left, self.av_inline.state if self.av_inline else "Preparing AV clip...", self.color(8), width)
        self.add(top + 1, left, label, self.color(4), width)
        self.add(top + height - 2, left, "[ ] Video < > Skip 10 V Full | { } Audio J Track L Play/Stop", self.color(4), width)
        audio = self.audio_context()
        if audio:
            _, _, ordinal, sample, (start, length), _ = audio
            status = self.audio_preview.status if self.audio_preview else "L Listen to converted AAC"
            self.add(top + height - 1, left, f"Audio {sample + 1}/10 · Track {ordinal + 1} · {start:.2f}s + {length:.2f}s | {status}", self.color(8), width)
        else:
            self.add(top + height - 1, left, "Audio preview needs audio and a known duration.", self.color(8), width)

    def audio_context(self):
        source, settings = self.preview_subject()
        if source is None:
            return None
        if self.input_path and self.input_path.is_dir():
            metadata = self.batch_metadata.get(source, {})
            count, duration = metadata.get("audio_count", 0), metadata.get("duration")
        else:
            count = self.info.audio_stream_count if self.info else 0
            duration = self.info.duration if self.info else None
        if not count or not duration:
            return None
        ordinal = self.audio_track_indices.get(source, 0) % count
        if self.tab == 1 and self.track_choices:
            kind, highlighted = self.track_choices[self.track_selection_index]
            if kind == "audio":
                ordinal = highlighted
        self.audio_track_indices[source] = ordinal
        try:
            stat = source.stat()
            segments_key = (source, stat.st_size, stat.st_mtime_ns, duration)
        except OSError:
            return None
        if segments_key not in self.audio_segments:
            self.audio_segments[segments_key] = audio_preview.random_segments(duration)
        sample = self.audio_sample_indices.get(source, 0)
        segment = self.audio_segments[segments_key][sample]
        signature = (source, stat.st_size, stat.st_mtime_ns, ordinal, sample, settings.audio_bitrate, settings.ffmpeg, settings.ffprobe)
        if self.audio_signature != signature:
            if self.audio_preview and self.audio_preview.active:
                self.audio_preview.stop()
            self.audio_signature = signature
        return source, settings, ordinal, sample, segment, count

    def handle_audio_key(self, key):
        context = self.audio_context()
        if context is None:
            self.status = "Audio preview needs a selected audio track and known video duration."
            return
        source, settings, ordinal, sample, segment, count = context
        if key in (ord("{"), ord("}")):
            if self.audio_preview:
                self.audio_preview.stop()
            self.audio_sample_indices[source] = max(0, min(9, sample + (-1 if key == ord("{") else 1)))
        elif key in (ord("j"), ord("J")):
            if self.audio_preview:
                self.audio_preview.stop()
            self.audio_track_indices[source] = (ordinal + 1) % count
            if self.tab == 1:
                self.track_selection_index = self.track_choices.index(("audio", self.audio_track_indices[source]))
        else:
            if self.audio_preview is None:
                self.audio_preview = audio_preview.AudioPreview()
            if self.audio_preview.active:
                self.audio_preview.stop()
            else:
                self.audio_preview.play(source, asdict(settings), ordinal, segment)
            self.status = self.audio_preview.status

    def paint_inline_image(self):
        if self.inline_surface is None:
            return
        key, frame, top, left, columns, rows = self.inline_surface
        if key == self.inline_rendered:
            return
        if hasattr(frame, "payload"):
            if self.preview_backend in ("kitty", "iterm2"):
                payload = getattr(preview, self.preview_backend)(frame.payload, columns, rows)
            else:
                payload = frame.payload
        elif self.preview_backend in ("kitty", "iterm2"):
            payload = getattr(preview, self.preview_backend)(frame.png, columns, rows)
        else:
            payload = getattr(preview, self.preview_backend)(frame.rgb, frame.width, frame.height)
        sys.stdout.write("\x1b7")
        if self.preview_backend == "unicode":
            for row, line in enumerate(payload.split("\r\n")):
                sys.stdout.write(f"\x1b[{top + row + 1};{left + 1}H" + line)
        else:
            sys.stdout.write(f"\x1b[{top + 1};{left + 1}H" + payload)
        sys.stdout.write("\x1b8")
        sys.stdout.flush()
        self.inline_rendered = key

    def draw_overview(self, top: int, left: int, height: int, width: int) -> None:
        if width >= 72 and height >= 11:
            source_width = max(34, int(width * 0.44))
            target_width = width - source_width - 1
            self.draw_source_module(top, left, height, source_width)
            self.draw_target_module(top, left + source_width + 1, height, target_width)
            return
        lines = self.overview_lines()
        offset = min(self.scroll["Overview"], max(0, len(lines) - height))
        for row, (label, value, style) in enumerate(lines[offset : offset + height]):
            marker = "//" if label else ""
            self.add(top + row, left, f"{marker} {label:<15}", self.color(8) | curses.A_BOLD, 18)
            self.add(top + row, left + 19, value, style, width - 19)

    def draw_source_module(self, top: int, left: int, height: int, width: int) -> None:
        self.box(top, left, height, width, "INPUT VIDEO", 12)
        inner = width - 6
        if self.input_path and self.input_path.is_dir():
            self.centered(top + 3, left + 2, width - 4, "BATCH FOLDER SELECTED", self.color(13) | curses.A_BOLD)
            self.centered(
                top + 5,
                left + 2,
                width - 4,
                f"{len(self.batch_sources)} VIDEO(S) FOUND RECURSIVELY",
                self.color(4) if self.batch_sources else self.color(5),
            )
            self.add(top + 7, left + 3, f"ROOT {self.input_path}", self.color(8), inner)
            self.add(top + 9, left + 3, "FILES KEEP THEIR SUBFOLDER LAYOUT", self.color(8), inner)
            if self.probe_error:
                self.add(top + 11, left + 3, self.probe_error, self.color(5), inner)
            return
        if not self.info or not self.input_payload:
            self.centered(top + 3, left + 2, width - 4, "NO VIDEO SELECTED", self.color(5) | curses.A_BOLD)
            self.centered(top + 5, left + 2, width - 4, "I SELECT VIDEO  ·  U UPDATE", self.color(8))
            if self.probe_error:
                self.add(top + 7, left + 3, self.probe_error, self.color(5), inner)
            return
        fmt = self.input_payload.get("format") or {}
        self.add(top + 2, left + 3, "INPUT ANALYZED", self.color(13) | curses.A_BOLD)
        self.add(top + 2, left + width - 15, "◈ VERIFIED", self.color(4) | curses.A_BOLD, 12)
        resolution = f"{self.info.width} x {self.info.height}"
        self.add(top + 4, left + 3, resolution, self.color(13) | curses.A_BOLD, inner)
        self.add(
            top + 5,
            left + 3,
            f"{self.info.video_codec.upper()}  ·  {float(self.info.fps):.3f} FPS  ·  {self.info.pixel_format.upper()}",
            self.color(9),
            inner,
        )
        color_mode = "HDR" if self.info.hdr else "SDR"
        scan = "INTERLACED" if self.info.interlaced else "PROGRESSIVE"
        self.add(
            top + 7,
            left + 3,
            f"{color_mode}  ›  {scan}  ›  {self.info.color_space.upper()}",
            self.color(4) if self.info.hdr else self.color(8),
            inner,
        )
        self.add(
            top + 9,
            left + 3,
            f"A·{self.info.audio_stream_count:02d}  S·{len(self.info.subtitle_codecs):02d}  "
            f"T·{self.info.attachment_count:02d}  C·{1 if self.info.cover_stream_index is not None else 0:02d}",
            self.color(4) | curses.A_BOLD,
            inner,
        )
        if height >= 15:
            self.add(
                top + 11,
                left + 3,
                f"T {duration_text(fmt.get('duration'))}   Σ {self.rate_text(fmt.get('bit_rate'))}",
                self.color(8),
                inner,
            )
        if height >= 17 and self.input_path:
            self.add(
                top + 13,
                left + 3,
                f"SIZE {bytes_text(self.input_path.stat().st_size)}  ·  {fmt.get('format_name', 'unknown')}",
                self.color(8),
                inner,
            )

    def draw_target_module(self, top: int, left: int, height: int, width: int) -> None:
        self.box(top, left, height, width, "PS VITA OUTPUT", 14)
        inner = width - 6
        if self.input_path and self.input_path.is_dir() and self.output_path:
            self.centered(top + 3, left + 2, width - 4, "BATCH OUTPUT READY", self.color(13) | curses.A_BOLD)
            self.centered(top + 5, left + 2, width - 4, "ONE VITAMEDIADECK MKV PER VIDEO", self.color(4))
            self.add(top + 7, left + 3, f"ROOT {self.output_path}", self.color(8), inner)
            self.add(top + 9, left + 3, "ORIGINAL SUBFOLDERS ARE PRESERVED", self.color(8), inner)
            return
        if not self.info:
            self.centered(top + 3, left + 2, width - 4, "OUTPUT NOT READY", self.color(5) | curses.A_BOLD)
            self.centered(top + 5, left + 2, width - 4, "SELECT AND ANALYZE A VIDEO", self.color(8))
            return
        max_fps = core.fraction_from_text(self.settings.max_fps) or core.MAX_FPS
        fps = core.target_fps(self.info.fps, max_fps)
        output_width, output_height = core.fitted_dimensions(self.info)
        target, maximum, _ = core.bitrate_plan(
            output_width,
            output_height,
            fps,
            self.settings.quality,
            self.settings.content_tune,
        )
        engine = self.settings.encoder.upper()
        if self.capabilities:
            try:
                engine = core.encoder_candidates(
                    self.settings.encoder,
                    self.capabilities,
                    Path(self.settings.vaapi_device),
                )[0].upper()
            except core.TranscodeError:
                engine = "NO COMPATIBLE ENCODER"
        if self.settings.cover_mode == "auto" and self.info.cover_stream_index is not None:
            target_badge = "PS VITA READY · EMBEDDED COVER"
        elif self.settings.cover_mode == "auto":
            target_badge = "PS VITA READY · GENERATED COVER"
        elif self.settings.cover_mode == "custom":
            target_badge = "PS VITA READY · CUSTOM COVER"
        else:
            target_badge = "PS VITA READY · NO COVER"
        self.centered(
            top + 2,
            left + 2,
            width - 4,
            target_badge,
            self.color(4) | curses.A_BOLD,
        )
        active_conversion = bool(self.process and self.process.poll() is None)
        field_ratio = self.live.overall_ratio if active_conversion else target / maximum
        rich_field = height >= 27 and width >= 42
        self.centered(
            top + 3,
            left + 2,
            width - 4,
            "OUTPUT PROFILE",
            self.color(7) | curses.A_BOLD,
        )
        self.draw_particle_visualization(
            top + 4,
            left + 2,
            width - 4,
            field_ratio,
            active_conversion,
            compact=not rich_field,
        )

        if rich_field:
            if active_conversion:
                self.tape_transport(top + 12, left + 2, width - 4, True)
            else:
                self.centered(
                    top + 12,
                    left + 2,
                    width - 4,
                    "READY TO CONVERT",
                    self.color(8) | curses.A_BOLD,
                )
            format_row = top + 14
            primary_row = top + 16
            secondary_row = top + 17
            auxiliary_row = top + 19
            estimate_row = top + 21
        else:
            format_row = top + 7
            primary_row = top + 9
            secondary_row = top + 10
            auxiliary_row = top + 12
            estimate_row = top + 14

        self.centered(
            format_row,
            left + 2,
            width - 4,
            f"{output_width}×{output_height}  ·  H.264 HIGH  ·  AAC-LC",
            self.color(3) | curses.A_BOLD,
        )
        if active_conversion:
            self.add(primary_row, left + 3, f"PHASE › {self.live.phase_label}", self.color(13), inner)
            self.add(
                secondary_row,
                left + 3,
                f"FRAME {self.live.frame}  ·  FPS {self.live.fps}  ·  SPEED {self.live.speed}",
                self.color(8),
                inner,
            )
        else:
            self.add(primary_row, left + 3, f"FRAME RATE › {float(fps):.3f} FPS", self.color(8), inner)
            self.add(secondary_row, left + 3, f"ENCODER    › {engine}", self.color(8), inner)

        if auxiliary_row < top + height - 1:
            if active_conversion:
                eta = duration_text(self.live.eta_seconds) if self.live.eta_seconds is not None else "--:--:--"
                self.add(
                    auxiliary_row,
                    left + 3,
                    f"MEDIA {duration_text(self.live.media_seconds)}  ·  ETA {eta}",
                    self.color(4) | curses.A_BOLD,
                    inner,
                )
            else:
                if self.settings.cover_mode == "auto":
                    cover = "EMBEDDED COVER" if self.info.cover_stream_index is not None else "AUTO FRAME"
                else:
                    cover = self.settings.cover_mode.upper()
                self.add(
                    auxiliary_row,
                    left + 3,
                    f"{self.settings.quality.upper()}  ·  {self.settings.content_tune.upper()}  ·  {cover}  ·  "
                    f"A{len(self.selected_audio_tracks):02d} S{len(self.selected_subtitle_tracks):02d}",
                    self.color(8),
                    inner,
                )
        if estimate_row < top + height - 1:
            duration = self.info.duration or 0
            audio_bitrates = self.selected_audio_bitrates()
            estimate = duration * (target + sum(audio_bitrates) * 1000) / 8
            self.add(estimate_row, left + 3, f"ESTIMATED SIZE › {bytes_text(estimate)}", self.color(7), inner)

    def overview_lines(self) -> list[tuple[str, str, int]]:
        source = str(self.input_path) if self.input_path else "not selected"
        output = str(self.output_path) if self.output_path else "not selected"
        lines: list[tuple[str, str, int]] = [
            ("Input", source, 0),
            ("Output", output, 0),
        ]
        if self.input_path and self.input_path.is_file():
            lines.append(("Input size", bytes_text(self.input_path.stat().st_size), 0))
        if self.input_path and self.input_path.is_dir():
            lines.extend(
                [
                    ("Mode", "recursive folder conversion", self.color(4)),
                    ("Videos found", str(len(self.batch_sources)), 0),
                    ("Output layout", "mirrors the input subfolders", 0),
                ]
            )
        if self.probe_error:
            lines.append(("Inspection error", self.probe_error, self.color(5)))
        if self.input_path and self.input_path.is_dir():
            return lines
        if not self.info or not self.input_payload:
            lines.append(("Media", "press I to inspect the selected input", curses.A_DIM))
            return lines
        fmt = self.input_payload.get("format") or {}
        lines.extend(
            [
                ("Container", str(fmt.get("format_long_name") or fmt.get("format_name") or "unknown"), 0),
                ("Duration", duration_text(fmt.get("duration")), 0),
                ("Input bitrate", self.rate_text(fmt.get("bit_rate")), 0),
                ("Video", f"{self.info.video_codec}, {self.info.width}x{self.info.height}, {float(self.info.fps):.3f} fps", 0),
                ("Pixel format", self.info.pixel_format, 0),
                ("Color", f"{self.info.color_primaries}/{self.info.color_transfer}/{self.info.color_space} - {'HDR' if self.info.hdr else 'SDR'}", self.color(4) if self.info.hdr else 0),
                ("Scan", "interlaced" if self.info.interlaced else "progressive", 0),
                ("Audio tracks", str(self.info.audio_stream_count), 0),
                ("Subtitle tracks", str(len(self.info.subtitle_codecs)), 0),
                ("Attachments", str(self.info.attachment_count), 0),
                (
                    "Embedded cover",
                    self.info.cover_name if self.info.cover_stream_index is not None else "not present",
                    self.color(3) if self.info.cover_stream_index is not None else self.color(8),
                ),
            ]
        )
        lines.append(("", "", 0))
        lines.extend(self.output_plan_lines())
        if self.output_path and self.output_path.is_file():
            lines.extend(
                [
                    ("", "", 0),
                    ("Existing output", bytes_text(self.output_path.stat().st_size), self.color(3)),
                ]
            )
            lines.extend(self.actual_output_lines())
        return lines

    def actual_output_lines(self) -> list[tuple[str, str, int]]:
        if not self.output_payload:
            return [("Output details", "press I after selecting an existing output", curses.A_DIM)]
        streams = self.output_payload.get("streams") or []
        video = next(
            (
                item
                for item in streams
                if item.get("codec_type") == "video"
                and not int((item.get("disposition") or {}).get("attached_pic") or 0)
            ),
            None,
        )
        cover = any(
            item.get("codec_type") == "video"
            and int((item.get("disposition") or {}).get("attached_pic") or 0)
            for item in streams
        )
        audio_count = sum(item.get("codec_type") == "audio" for item in streams)
        subtitle_count = sum(item.get("codec_type") == "subtitle" for item in streams)
        fmt = self.output_payload.get("format") or {}
        rows: list[tuple[str, str, int]] = [
            ("Output container", str(fmt.get("format_long_name") or fmt.get("format_name") or "unknown"), 0),
            ("Output duration", duration_text(fmt.get("duration")), 0),
            ("Output bitrate", self.rate_text(fmt.get("bit_rate")), 0),
        ]
        if self.output_path and self.output_path.is_file():
            output_size = self.output_path.stat().st_size
            rows.append(("Output size", bytes_text(output_size), self.color(3)))
            if self.input_path and self.input_path.is_file() and self.input_path.stat().st_size:
                input_size = self.input_path.stat().st_size
                delta = (1.0 - output_size / input_size) * 100.0
                label = f"{abs(delta):.1f}% {'smaller' if delta >= 0 else 'larger'} than source"
                rows.append(("Storage change", label, self.color(3) if delta >= 0 else self.color(4)))
        if video:
            fps = video.get("avg_frame_rate") or video.get("r_frame_rate") or "unknown"
            rows.append(
                (
                    "Actual video",
                    f"{video.get('codec_name', 'unknown')} {video.get('profile', '')}, "
                    f"{video.get('width', '?')}x{video.get('height', '?')}, {fps} fps, "
                    f"{video.get('pix_fmt', 'unknown')}",
                    self.color(3),
                )
            )
            rows.append(
                (
                    "Video signal",
                    f"level {video.get('level', '?')}, {video.get('color_space', '?')}/"
                    f"{video.get('color_transfer', '?')}, range {video.get('color_range', '?')}",
                    0,
                )
            )
        for number, audio in enumerate(
            (item for item in streams if item.get("codec_type") == "audio"),
            start=1,
        ):
            bit_rate = core.stream_bit_rate(audio)
            bit_rate_text = (
                f"{bit_rate / 1000:.0f} kb/s"
                if bit_rate is not None
                else f"target {self.settings.audio_bitrate} kb/s"
            )
            rows.append(
                (
                    f"Audio {number:02d}",
                    f"{audio.get('codec_name', '?').upper()} {audio.get('channels', '?')}ch "
                    f"{audio.get('sample_rate', '?')} Hz, {bit_rate_text}, "
                    f"{duration_text(core.stream_duration(audio))}",
                    self.color(3),
                )
            )
        rows.extend(
            [
                ("Actual tracks", f"{audio_count} audio, {subtitle_count} subtitle", 0),
                ("Actual cover", "embedded" if cover else "not present", 0),
            ]
        )
        return rows

    def output_plan_lines(self) -> list[tuple[str, str, int]]:
        assert self.info is not None
        max_fps = core.fraction_from_text(self.settings.max_fps) or core.MAX_FPS
        fps = core.target_fps(self.info.fps, max_fps)
        width, height = core.fitted_dimensions(self.info)
        target, maximum, _ = core.bitrate_plan(
            width,
            height,
            fps,
            self.settings.quality,
            self.settings.content_tune,
        )
        if self.settings.cover_mode == "auto":
            cover = (
                f"reuse {self.info.cover_name}"
                if self.info.cover_stream_index is not None
                else "generate cover.jpg from video frame"
            )
        else:
            cover = self.settings.cover_mode
        encoders = "not inspected"
        if self.capabilities:
            try:
                encoders = ", ".join(
                    core.encoder_candidates(
                        self.settings.encoder,
                        self.capabilities,
                        Path(self.settings.vaapi_device),
                    )
                )
            except core.TranscodeError as exc:
                encoders = str(exc)
        duration = self.info.duration or 0
        audio_bitrates = self.selected_audio_bitrates()
        estimated = duration * (target + sum(audio_bitrates) * 1000) / 8
        tuning_detail = (
            f"x264 tune={core.X264_CONTENT_TUNES[self.settings.content_tune]}"
            if self.settings.encoder == "x264"
            else f"{self.settings.content_tune} bitrate curve · hardware-native HQ"
        )
        return [
            ("Output profile", f"H.264 High, {width}x{height}, {float(fps):.3f} fps", self.color(3)),
            ("Video bitrate", f"{target / 1_000_000:.1f} Mb/s target, {maximum / 1_000_000:.1f} Mb/s max", 0),
            (
                "Content tuning",
                tuning_detail,
                self.color(3),
            ),
            (
                "System load",
                f"{self.settings.system_load}; "
                + (
                    "unlimited"
                    if core.resource_thread_limit(self.settings.system_load) == 0
                    else f"up to {core.resource_thread_limit(self.settings.system_load)} FFmpeg threads"
                ),
                0,
            ),
            (
                "Audio output",
                f"{len(self.selected_audio_tracks)}/{self.info.audio_stream_count} selected · "
                f"AAC-LC stereo 48 kHz @ {', '.join(f'{rate} kb/s' for rate in audio_bitrates) or 'none'}",
                0,
            ),
            (
                "Subtitle output",
                f"{len(self.selected_subtitle_tracks)}/{len(self.info.subtitle_codecs)} selected",
                0,
            ),
            ("A/V protection", "timestamp repair + full-duration padding/trim + track validation", self.color(3)),
            ("Cover output", cover, 0),
            ("Encoder order", encoders, 0),
            ("Estimated size", (bytes_text(estimated) + " (duration x total bitrate)") if duration else "unknown duration", curses.A_DIM),
        ]

    def selected_audio_bitrates(self) -> tuple[int, ...]:
        if not self.info:
            return ()
        return core.target_audio_bitrates(
            self.info,
            sorted(self.selected_audio_tracks),
            self.settings.audio_bitrate,
        )

    @staticmethod
    def rate_text(value: Any) -> str:
        try:
            return f"{int(value) / 1_000_000:.2f} Mb/s"
        except (TypeError, ValueError):
            return "unknown"

    def draw_streams(self, top: int, left: int, height: int, width: int) -> None:
        lines = self.stream_lines()
        self.add(
            top,
            left,
            "TRACK SELECTION   SPACE TOGGLE  ·  E ALL AUDIO  ·  S ALL SUBTITLES  ·  X CLEAR TYPE",
            self.color(1) | curses.A_BOLD,
            width,
        )
        self.add(
            top + 1,
            left,
            f"INCLUDED IN OUTPUT   AUDIO {len(self.selected_audio_tracks):02d}  ·  "
            f"SUBTITLE {len(self.selected_subtitle_tracks):02d}",
            self.color(4) | curses.A_BOLD,
            width,
        )
        list_height = max(1, height - 3)
        maximum = max(0, len(lines) - list_height)
        offset = min(self.scroll["Streams"], maximum)
        if self.track_choices:
            if self.stream_cursor_line < offset:
                offset = self.stream_cursor_line
            elif self.stream_cursor_line >= offset + list_height:
                offset = self.stream_cursor_line - list_height + 1
            self.scroll["Streams"] = max(0, min(offset, maximum))
        for row, (text, style) in enumerate(lines[offset : offset + list_height]):
            self.add(top + 3 + row, left, text, style, width)

    def stream_lines(self) -> list[tuple[str, int]]:
        if self.input_path and self.input_path.is_dir():
            return [
                ("Batch defaults preserve all compatible audio and subtitles.", self.color(8)),
                ("V opens the AV editor to inspect/exclude tracks for the selected media.", curses.A_DIM),
            ]
        if not self.input_payload:
            return [("Inspect an input file to view every stream.", curses.A_DIM)]
        self.stream_cursor_line = 0
        rows = self.payload_stream_lines(self.input_payload, "INPUT STREAMS", selectable=True)
        if self.output_payload:
            rows.append(("", 0))
            rows.extend(self.payload_stream_lines(self.output_payload, "OUTPUT STREAMS"))
        elif self.output_path and self.output_path.is_file():
            rows.extend([("", 0), ("Output exists but could not be inspected.", self.color(5))])
        return rows

    def payload_stream_lines(
        self,
        payload: dict[str, Any],
        heading: str,
        selectable: bool = False,
    ) -> list[tuple[str, int]]:
        rows: list[tuple[str, int]] = [
            (f"◢ {heading}  ···············································", self.color(1) | curses.A_BOLD),
            ("", 0),
        ]
        audio_ordinal = 0
        subtitle_ordinal = 0
        for stream in payload.get("streams") or []:
            index = stream.get("index", "?")
            kind = str(stream.get("codec_type") or "unknown").upper()
            codec = stream.get("codec_long_name") or stream.get("codec_name") or "unknown"
            signal = {"VIDEO": "VID", "AUDIO": "AUD", "SUBTITLE": "SUB", "ATTACHMENT": "ATT"}.get(kind, "DAT")
            signal_style = {
                "VIDEO": self.color(4),
                "AUDIO": self.color(1),
                "SUBTITLE": self.color(3),
                "ATTACHMENT": self.color(7),
            }.get(kind, self.color(8))
            choice: tuple[str, int] | None = None
            selected = True
            if kind == "AUDIO":
                choice = ("audio", audio_ordinal)
                selected = audio_ordinal in self.selected_audio_tracks
                audio_ordinal += 1
            elif kind == "SUBTITLE":
                choice = ("subtitle", subtitle_ordinal)
                selected = subtitle_ordinal in self.selected_subtitle_tracks
                subtitle_ordinal += 1
            cursor = (
                selectable
                and choice is not None
                and self.track_choices
                and choice == self.track_choices[self.track_selection_index]
            )
            if cursor:
                self.stream_cursor_line = len(rows)
            if selectable and choice is not None:
                prefix = f"{'◇' if cursor else ' '} [{'◆' if selected else ' '}]"
                style = self.color(2) | curses.A_BOLD if cursor else (
                    signal_style | curses.A_BOLD if selected else self.color(8)
                )
            else:
                prefix = "     "
                style = signal_style | curses.A_BOLD
            rows.append(
                (
                    f"{prefix} {signal}·{str(index).zfill(2)}  ›  {str(codec).upper()}",
                    style,
                )
            )
            details: list[str] = []
            if stream.get("codec_type") == "video":
                details.extend(
                    [
                        f"{stream.get('width', '?')}x{stream.get('height', '?')}",
                        str(stream.get("pix_fmt") or "unknown pixel format"),
                        f"{stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '?'} fps",
                        str(stream.get("profile") or "unknown profile"),
                    ]
                )
                if int((stream.get("disposition") or {}).get("attached_pic") or 0):
                    details.append("attached cover")
            elif stream.get("codec_type") == "audio":
                bit_rate = core.stream_bit_rate(stream)
                if bit_rate is not None:
                    bit_rate_text = f"{bit_rate / 1000:.0f} kb/s"
                elif not selectable:
                    bit_rate_text = f"target {self.settings.audio_bitrate} kb/s"
                else:
                    bit_rate_text = "bitrate unavailable"
                details.extend(
                    [
                        f"{stream.get('channels', '?')} channels",
                        f"{stream.get('sample_rate', '?')} Hz",
                        bit_rate_text,
                        duration_text(core.stream_duration(stream)),
                    ]
                )
            elif stream.get("codec_type") == "subtitle":
                details.append(tag_text(stream))
            elif stream.get("codec_type") == "attachment":
                tags = stream.get("tags") or {}
                details.extend([str(tags.get("filename") or "unnamed"), str(tags.get("mimetype") or "unknown MIME")])
            if stream.get("codec_type") in {"video", "audio"}:
                details.append(tag_text(stream))
                disposition = stream.get("disposition") or {}
                flags = [name for name in ("default", "forced", "hearing_impaired") if int(disposition.get(name) or 0)]
                if flags:
                    details.append("flags=" + ",".join(flags))
            rows.append(("         " + "  ·  ".join(details), self.color(8)))
            rows.append(("         · · · · · · · · · · · · · · · · · · · · · · ·", self.color(7)))
        chapters = payload.get("chapters") or []
        rows.append((f"CHAPTERS › {len(chapters):02d}", self.color(4) | curses.A_BOLD))
        return rows

    def draw_settings(self, top: int, left: int, height: int, width: int) -> None:
        settings = self.active_settings()
        editing = (
            f"MEDIA › {self.batch_edit_source.relative_to(self.input_path)}"
            if self.batch_edit_source and self.input_path
            else "GLOBAL DEFAULTS"
        )
        self.add(top, left, "◢ CONVERSION SETTINGS", self.color(1) | curses.A_BOLD, width)
        self.add(top + 1, left, editing, self.color(4) | curses.A_BOLD, width)
        self.add(
            top + 2,
            left,
            "UP/DOWN SELECT  ·  LEFT/RIGHT MODIFY  ·  G APPLY FIELD TO ALL  ·  B GLOBAL DEFAULTS",
            self.color(8),
            width,
        )
        list_height = max(1, height - 8)
        label_width = min(23, max(16, width // 4))
        longest_value = max(len(str(getattr(settings, attribute))) + 4 for _, attribute, _ in self.SETTING_ROWS)
        value_width = max(12, min(max(24, longest_value), width - label_width - 32))
        description_left = left + label_width + value_width + 4
        description_width = max(1, width - label_width - value_width - 4)
        self.add(top + 3, left, "Parameter", self.color(8), label_width)
        self.add(top + 3, left + label_width + 2, "Value", self.color(8), value_width)
        self.add(top + 3, description_left, "Effect of this value", self.color(8), description_width)
        offset = min(
            max(0, self.setting_index - list_height + 1),
            max(0, len(self.SETTING_ROWS) - list_height),
        )
        for visible, (label, attribute, choices) in enumerate(self.SETTING_ROWS[offset : offset + list_height]):
            index = offset + visible
            selected = index == self.setting_index
            style = self.color(2) | curses.A_BOLD if selected else 0
            value = getattr(settings, attribute)
            if isinstance(value, bool):
                value = "ENABLED" if value else "DISABLED"
            elif attribute == 'max_fps':
                value = ui.fps_label(value)
            if attribute == "cover_image" and settings.cover_mode != "custom":
                value = "NOT USED"
                style = self.color(8) if not selected else style
            marker = "◇" if selected else "·"
            value_text = f"‹ {value} ›" if choices is not None and selected else str(value)
            row = top + 4 + visible
            self.add(row, left, f"{marker} {label}", style, label_width)
            self.add(row, left + label_width + 2, value_text, style, value_width)
            self.add(row, description_left, ui.setting_description(settings, attribute), self.color(4) if selected else self.color(8), description_width)
        _, attribute, _ = self.SETTING_ROWS[self.setting_index]
        if height >= 8:
            detail = ui.setting_description(settings, attribute)
            selected_value = str(getattr(settings, attribute))
            if len(selected_value) > value_width:
                detail = f"{selected_value} — {detail}"
            for row, line in enumerate(textwrap.wrap(detail, max(1, width - 2))[:3]):
                self.add(top + height - 3 + row, left, line, self.color(4), width)

    def draw_presets(self, top: int, left: int, height: int, width: int) -> None:
        rows = self.presets.rows()
        if rows:
            self.preset_index = min(self.preset_index, len(rows) - 1)
        offset = min(max(0, self.preset_index - height + 3), max(0, len(rows) - max(1, height - 3)))
        self.add(top, left, "◢ PRESETS", self.color(1) | curses.A_BOLD, width)
        self.add(top + 1, left, "ENTER APPLY  ·  N SAVE CURRENT  ·  D DELETE CUSTOM PRESET", self.color(8), width)
        self.add(top + 2, left, f"PRESET FILE › {self.presets.path}", self.color(7), width)
        for visible, (name, custom, settings) in enumerate(rows[offset : offset + max(0, height - 3)]):
            index = offset + visible
            marker = "CUSTOM" if custom else "BUILT-IN"
            style = self.color(2) | curses.A_BOLD if index == self.preset_index else 0
            selector = "◇" if index == self.preset_index else "·"
            tuning = (
                core.X264_CONTENT_TUNES[settings.content_tune].upper()
                if settings.encoder == "x264"
                else f"{settings.content_tune.upper()} / HW HQ"
            )
            self.add(
                top + 3 + visible,
                left,
                f"{selector} M{index + 1:02d}  {name.upper():<28}  "
                f"‹{marker} · {tuning}›",
                style,
                width,
            )

    def draw_batch(self, top: int, left: int, height: int, width: int) -> None:
        if not self.input_path or not self.input_path.is_dir():
            self.add(top, left, "◢ BATCH MEDIA", self.color(1) | curses.A_BOLD, width)
            self.add(top + 2, left, "SELECT A DIRECTORY WITH I, THEN PRESS D IN THE BROWSER.", self.color(8), width)
            return
        self.add(top, left, "◢ DISCOVERED MEDIA INSIDE SELECTED DIRECTORY", self.color(1) | curses.A_BOLD, width)
        self.add(top + 1, left, f"ROOT › {self.input_path}", self.color(8), width)
        self.add(
            top + 2,
            left,
            "SPACE INCLUDE  +/- REORDER  ENTER SETTINGS  V PREVIEW  F RETRY FAILED  R/G RESET",
            self.color(8),
            width,
        )
        if not self.batch_sources:
            self.add(top + 4, left, "NO SUPPORTED VIDEO FOUND BELOW THIS DIRECTORY.", self.color(5), width)
            return
        self.batch_selection_index = min(self.batch_selection_index, len(self.batch_sources) - 1)
        list_height = max(1, height - 6)
        offset = min(
            max(0, self.batch_selection_index - list_height + 1),
            max(0, len(self.batch_sources) - list_height),
        )
        for row, source in enumerate(self.batch_sources[offset : offset + list_height]):
            index = offset + row
            selected = index == self.batch_selection_index
            style = self.color(2) | curses.A_BOLD if selected else 0
            setting = self.settings_for_batch_source(source)
            origin = "CUSTOM" if source in self.batch_settings else "GLOBAL"
            relative = source.relative_to(self.input_path)
            meta = self.batch_metadata.get(source, {})
            state = meta.get("status", "pending") if self.batch_enabled.get(source, True) else "excluded"
            summary = f"{state} {duration_text(meta.get('duration'))} {setting.encoder}/{setting.quality}/{ui.fps_label(setting.max_fps)}"
            marker = "◇" if selected else "·"
            self.add(
                top + 4 + row,
                left,
                f"{marker} {index + 1:03d} {relative}  [{origin} {summary}]",
                style,
                width,
            )
        selected_source = self.batch_sources[self.batch_selection_index]
        meta = self.batch_metadata.get(selected_source, {})
        self.add(top + height - 2, left, f"PATH › {selected_source}", self.color(8), width)
        self.add(top + height - 1, left, str(meta.get("error") or meta.get("log") or "V: filtered frame + conversion plan"), self.color(4), width)

    def draw_log(self, top: int, left: int, height: int, width: int) -> None:
        lines = list(self.logs)
        if not lines:
            lines = ["NO CONVERSION LOG YET  ·  PRESS T TO START"]
        active = bool(self.process and self.process.poll() is None)
        animating = active and not self.paused
        self.add(top, left, "◢ CONVERSION LOG", self.color(13) | curses.A_BOLD, width)
        self.draw_particle_strip(top, left, width, animating)
        self.draw_phase_rail(top + 1, left, width)
        self.tape_transport(top + 2, left, width, active)
        if active and self.paused:
            self.add(
                top + 3,
                left,
                f"PAUSED › {self.live.phase_label}  ·  PRESS P TO RESUME",
                self.color(7) | curses.A_BOLD,
                width,
            )
        elif active:
            self.add(
                top + 3,
                left,
                f"CURRENT STEP › {self.live.phase_label}  ·  {self.live.detail or 'processing'}",
                self.color(13) | curses.A_BOLD,
                width,
            )
        elif self.live.phase == "complete":
            complete_text = (
                f"BATCH COMPLETE  ·  {self.live.batch_total} OUTPUT(S) VERIFIED"
                if self.live.batch_total
                else "CONVERSION COMPLETE  ·  OUTPUT VERIFIED"
            )
            self.add(top + 3, left, complete_text, self.color(3) | curses.A_BOLD, width)
        else:
            self.add(top + 3, left, "READY  ·  PRESS T TO START", self.color(8), width)

        meter_width = max(8, width - 27)
        self.add(top + 4, left, "TOTAL", self.color(14) | curses.A_BOLD, 9)
        self.meter(top + 4, left + 10, meter_width, self.live.overall_ratio, 14)
        self.add(
            top + 4,
            left + 11 + meter_width,
            f"{self.live.overall_ratio * 100:05.1f}%",
            self.color(14) | curses.A_BOLD,
            8,
        )
        local_label = (
            f"EP {self.live.batch_index:02d}/{self.live.batch_total:02d}"
            if self.live.batch_total
            else "LOCAL"
        )
        self.add(top + 5, left, local_label, self.color(13) | curses.A_BOLD, 9)
        self.meter(top + 5, left + 10, meter_width, self.live.local_ratio, 4)
        self.add(
            top + 5,
            left + 11 + meter_width,
            f"{self.live.local_ratio * 100:05.1f}%",
            self.color(13) | curses.A_BOLD,
            8,
        )
        self.add(
            top + 6,
            left,
            f"F {self.live.frame:>8}  FPS {self.live.fps:>6}  Q {self.live.quality:>5}  "
            f"× {self.live.speed:>7}  SIZE {self.live.size:>10}  BITRATE {self.live.bitrate:>10}",
            self.color(4),
            width,
        )
        eta = duration_text(self.live.eta_seconds) if self.live.eta_seconds is not None else "--:--:--"
        batch_item = (
            f"  ·  ITEM {self.live.batch_index}/{self.live.batch_total} {self.live.batch_source}"
            if self.live.batch_total
            else ""
        )
        self.add(
            top + 7,
            left,
            f"MEDIA {duration_text(self.live.media_seconds)}  ·  ETA {eta}  ·  "
            "VIDEO AND AUDIO PROCESSED SEPARATELY"
            + batch_item,
            self.color(8),
            width,
        )
        log_height = max(1, height - 9)
        maximum = max(0, len(lines) - log_height)
        offset = max(0, maximum - self.scroll["Log"])
        for row, line in enumerate(lines[offset : offset + log_height]):
            style = self.color(5) if "error" in line.lower() or "failed" in line.lower() else 0
            self.add(top + 9 + row, left, f"{offset + row + 1:04d} ┊ {line}", style, width)

    def draw_phase_rail(self, row: int, left: int, width: int) -> None:
        if self.live.phase == "complete":
            current_index = len(PHASE_ORDER)
        else:
            try:
                current_index = PHASE_ORDER.index(self.live.phase)
            except ValueError:
                current_index = -1
        column = left
        for index, phase in enumerate(PHASE_ORDER):
            if index < current_index or self.live.phase == "complete":
                token, style = f"◆{PHASE_SHORT[phase]}", self.color(3) | curses.A_BOLD
            elif index == current_index:
                token, style = f"◇{PHASE_SHORT[phase]}", self.color(11) | curses.A_BOLD
            else:
                token, style = f"·{PHASE_SHORT[phase]}", self.color(8)
            if column + len(token) > left + width:
                break
            self.add(row, column, token, style, len(token))
            column += len(token) + 3

    def handle_key(self, key: int) -> None:
        if key in (ord("q"), ord("Q")):
            if not (self.process and self.process.poll() is None):
                if not self.confirm("Exit VitaMediaDeck Transcoder?", accept_label='EXIT', reject_label='STAY'):
                    return
            # Recheck after the modal: a worker may have launched FFmpeg while
            # the user was reading the exit confirmation.
            if self.process and self.process.poll() is None:
                save_resume = self.confirm("Save a resume file before aborting?")
                if save_resume:
                    try:
                        resume_path = self.save_resume_state()
                    except (OSError, ValueError, core.TranscodeError) as exc:
                        self.status = f"Could not save resume file: {exc}"
                        return
                    self.logs.append(f"Resume file saved: {resume_path}")
                if not self.confirm("Abort conversion and quit?", accept_label='ABORT + EXIT', reject_label='STAY'):
                    return
                self.cancel_conversion()
                if self.worker:
                    self.worker.join(timeout=5)
                    if self.worker.is_alive():
                        self.status = "Waiting for conversion to stop; checkpoint is saved."
                        return
            elif self.worker and self.worker.is_alive():
                self.status = "Conversion is starting. Press Q again to save or abort once it starts."
                return
            self.running = False
            return
        if key == 9:
            self.tab = (self.tab + 1) % len(TABS)
            return
        if key == curses.KEY_RIGHT and self.tab != 2:
            self.tab = (self.tab + 1) % len(TABS)
            return
        if key == KEY_BACK_TAB:
            self.tab = (self.tab - 1) % len(TABS)
            return
        if ord("1") <= key <= ord(str(len(TABS))):
            self.tab = key - ord("1")
            return
        if key in (ord("v"), ord("V")):
            self.show_preview()
            return
        if key in (ord("l"), ord("L"), ord("j"), ord("J"), ord("{"), ord("}")):
            self.handle_audio_key(key)
            return
        if key in (ord("["), ord("]"), ord("<"), ord(">")):
            source, _ = self.preview_subject()
            if source:
                current = self.preview_samples.get(source)
                current = 20 if current is None else current
                step = {ord("["): -1, ord("]"): 1, ord("<"): -10, ord(">"): 10}[key]
                self.preview_samples[source] = max(0, min(99, current + step))
                self.status = f"Preview sample {self.preview_samples[source] + 1}/100."
            return
        if self.worker and self.worker.is_alive() and key not in (ord("p"), ord("P"), ord("a"), ord("A")):
            if self.tab in (1, 2, 3, 4) or key in (ord("i"), ord("I"), ord("o"), ord("O"), ord("u"), ord("U")):
                self.status = "Queue and settings are frozen while converting. P pauses; A aborts."
                return
        if key in (ord("i"), ord("I")):
            self.choose_input()
        elif key in (ord("o"), ord("O")):
            self.edit_output()
        elif key in (ord("u"), ord("U")):
            self.probe_current()
        elif key in (ord("t"), ord("T")):
            self.start_conversion()
        elif key in (ord("p"), ord("P")):
            self.toggle_pause()
        elif key in (ord("a"), ord("A")):
            self.cancel_conversion()
        elif self.tab == 1:
            self.handle_streams_key(key)
        elif self.tab == 2:
            self.handle_settings_key(key)
        elif self.tab == 3:
            self.handle_presets_key(key)
        elif self.tab == 4:
            self.handle_batch_key(key)
        else:
            self.handle_scroll_key(key)

    def handle_streams_key(self, key: int) -> None:
        if not self.track_choices:
            self.status = "No selectable audio or subtitle tracks in this source."
            return
        if key == curses.KEY_UP:
            self.track_selection_index = (self.track_selection_index - 1) % len(self.track_choices)
            return
        if key == curses.KEY_DOWN:
            self.track_selection_index = (self.track_selection_index + 1) % len(self.track_choices)
            return
        if key in (curses.KEY_PPAGE, curses.KEY_HOME):
            self.track_selection_index = 0
            return
        if key in (curses.KEY_NPAGE, curses.KEY_END):
            self.track_selection_index = len(self.track_choices) - 1
            return
        kind, ordinal = self.track_choices[self.track_selection_index]
        if key == ord(" "):
            selected = (
                self.selected_audio_tracks
                if kind == "audio"
                else self.selected_subtitle_tracks
            )
            if ordinal in selected:
                selected.remove(ordinal)
                action = "omitted"
            else:
                selected.add(ordinal)
                action = "preserved"
            self.status = f"{kind.title()} track {ordinal} will be {action}."
        elif key in (ord("e"), ord("E")):
            assert self.info is not None
            self.selected_audio_tracks = set(range(self.info.audio_stream_count))
            self.status = "Every audio track will be preserved."
        elif key in (ord("s"), ord("S")):
            assert self.info is not None
            self.selected_subtitle_tracks = set(range(len(self.info.subtitle_codecs)))
            self.status = "Every subtitle track will be preserved."
        elif key in (ord("x"), ord("X")):
            if kind == "audio":
                self.selected_audio_tracks.clear()
            else:
                self.selected_subtitle_tracks.clear()
            self.status = f"Every {kind} track will be omitted."

    def handle_scroll_key(self, key: int) -> None:
        name = TABS[self.tab]
        if name == "Log":
            if key == curses.KEY_UP:
                self.scroll[name] += 1
            elif key == curses.KEY_DOWN:
                self.scroll[name] = max(0, self.scroll[name] - 1)
            elif key == curses.KEY_PPAGE:
                self.scroll[name] += 10
            elif key == curses.KEY_NPAGE:
                self.scroll[name] = max(0, self.scroll[name] - 10)
            elif key == curses.KEY_END:
                self.scroll[name] = 0
            return
        if key == curses.KEY_UP:
            self.scroll[name] = max(0, self.scroll[name] - 1)
        elif key == curses.KEY_DOWN:
            self.scroll[name] += 1
        elif key == curses.KEY_PPAGE:
            self.scroll[name] = max(0, self.scroll[name] - 10)
        elif key == curses.KEY_NPAGE:
            self.scroll[name] += 10
        elif key == curses.KEY_END and name == "Log":
            self.scroll[name] = 0

    def handle_settings_key(self, key: int) -> None:
        if key in (ord("b"), ord("B")):
            self.batch_edit_source = None
            self.status = "Editing global defaults; custom media keep their overrides."
            return
        if key == curses.KEY_UP:
            self.setting_index = (self.setting_index - 1) % len(self.SETTING_ROWS)
            return
        if key == curses.KEY_DOWN:
            self.setting_index = (self.setting_index + 1) % len(self.SETTING_ROWS)
            return
        _, attribute, choices = self.SETTING_ROWS[self.setting_index]
        settings = self.active_settings()
        if key in (ord("g"), ord("G")):
            if not self.input_path or not self.input_path.is_dir() or self.batch_edit_source is None:
                self.status = "Select a media item from Batch before propagating a setting."
                return
            value = getattr(settings, attribute)
            applied = 0
            for source in self.batch_sources:
                if attribute == "force_hdr" and value and not self.batch_metadata.get(source, {}).get("hdr"):
                    continue
                if attribute == "cover_image" and value and not Path(str(value)).expanduser().is_file():
                    continue
                target = self.batch_settings.setdefault(source, Settings.from_dict(asdict(self.settings)))
                setattr(target, attribute, value)
                applied += 1
            self.status = f"Applied {attribute}: {applied} item(s); skipped {len(self.batch_sources) - applied} incompatible/unknown item(s)."
            return
        if choices is not None and key in (curses.KEY_LEFT, curses.KEY_RIGHT, 10, 13, ord(" ")):
            if attribute == 'audio_bitrate':
                plan = getattr(self, '_av_plan', {})
                sources = plan.get('tracks') or plan.get('all_audio', [])
                source_rates = [track.get('source_bps') for track in sources]
                values = core.audio_bitrate_choices(source_rates)
                current = settings.audio_bitrate
                if key == curses.KEY_LEFT:
                    settings.audio_bitrate = next((value for value in reversed(values) if value < current), current)
                else:
                    settings.audio_bitrate = next((value for value in values if value > current), current)
                self.status = f'Audio target {settings.audio_bitrate} kb/s per stereo track; source/codec limit {values[-1]} kb/s.'
                return
            if attribute == 'max_fps':
                current = core.fraction_from_text(settings.max_fps)
                candidates = list(reversed(choices)) if key == curses.KEY_LEFT else list(choices)
                settings.max_fps = next((value for value in candidates if current is None or
                    (core.fraction_from_text(value) < current if key == curses.KEY_LEFT else core.fraction_from_text(value) > current)), settings.max_fps)
                self.status = f'FPS ceiling: {ui.fps_label(settings.max_fps)}.'
                return
            values = list(choices)
            current = getattr(settings, attribute)
            try:
                index = values.index(current)
            except ValueError:
                index = 0
            direction = -1 if key == curses.KEY_LEFT else 1
            setattr(settings, attribute, values[max(0, min(len(values) - 1, index + direction))])
            self.status = f"Changed {attribute}. Preview updates automatically; 7 opens output review."
        elif choices is None and key in (10, 13):
            if attribute == "cover_image" and settings.cover_mode != "custom":
                self.status = "Set Cover to custom first."
                return
            current = str(getattr(settings, attribute))
            value = self.prompt(f"{attribute}", current)
            if value is not None:
                setattr(settings, attribute, value)

    def handle_presets_key(self, key: int) -> None:
        rows = self.presets.rows()
        if key == curses.KEY_UP and rows:
            self.preset_index = (self.preset_index - 1) % len(rows)
        elif key == curses.KEY_DOWN and rows:
            self.preset_index = (self.preset_index + 1) % len(rows)
        elif key in (10, 13) and rows:
            name, _, settings = rows[self.preset_index]
            selected = Settings.from_dict(asdict(settings))
            if self.batch_edit_source is None:
                self.settings = selected
            else:
                self.batch_settings[self.batch_edit_source] = selected
            self.status = f"Loaded preset: {name}"
        elif key in (ord("n"), ord("N")):
            name = self.prompt("New preset name", "")
            if name is not None:
                try:
                    self.presets.save(name, self.active_settings())
                    self.status = f"Saved preset: {name.strip()}"
                except (OSError, ValueError) as exc:
                    self.status = str(exc)
        elif key in (ord("d"), ord("D")) and rows:
            name, custom, _ = rows[self.preset_index]
            if not custom:
                self.status = "Built-in presets cannot be deleted."
            elif self.confirm(f"Delete custom preset '{name}'?"):
                try:
                    self.presets.delete(name)
                    self.preset_index = max(0, self.preset_index - 1)
                    self.status = f"Deleted preset: {name}"
                except OSError as exc:
                    self.status = f"Could not delete preset: {exc}"

    def handle_batch_key(self, key: int) -> None:
        if not self.batch_sources:
            self.status = "No batch media is available. Select a directory and inspect it first."
            return
        if key == curses.KEY_UP:
            self.batch_selection_index = (self.batch_selection_index - 1) % len(self.batch_sources)
            return
        if key == curses.KEY_DOWN:
            self.batch_selection_index = (self.batch_selection_index + 1) % len(self.batch_sources)
            return
        if key in (curses.KEY_PPAGE, curses.KEY_HOME):
            self.batch_selection_index = 0
            return
        if key in (curses.KEY_NPAGE, curses.KEY_END):
            self.batch_selection_index = len(self.batch_sources) - 1
            return
        source = self.batch_sources[self.batch_selection_index]
        if key == ord(" "):
            self.batch_enabled[source] = not self.batch_enabled.get(source, True)
        elif key in (ord("+"), ord("-")):
            target = max(0, min(len(self.batch_sources) - 1, self.batch_selection_index + (1 if key == ord("+") else -1)))
            self.batch_sources[self.batch_selection_index], self.batch_sources[target] = self.batch_sources[target], source
            self.batch_selection_index = target
        elif key in (ord("f"), ord("F")):
            recovery = workflow.find_recovery(self.output_path, True) if self.output_path else None
            if recovery and recovery.is_file():
                state = core.load_resume_state(recovery)
                failed = {Path(item["source"]) for item in state["entries"] if item.get("status") in {"failed", "interrupted", "running", "pending"}}
                self.batch_enabled = {item: item in failed for item in self.batch_sources}
                self.resume_state_path = recovery
                self.status = f"Retry queue: {len(failed)} unfinished/failed item(s). Press T."
        elif key in (10, 13):
            self.batch_edit_source = source
            self.tab = 2
            self.status = f"Editing settings for {source.relative_to(self.input_path)}. Press G to propagate a field."
        elif key in (ord("r"), ord("R")):
            self.reset_batch_source_settings(source)
            if self.batch_edit_source == source:
                self.batch_edit_source = None
            self.status = f"{source.relative_to(self.input_path)} now inherits global settings."
        elif key in (ord("g"), ord("G")):
            self.batch_settings.clear()
            self.batch_edit_source = None
            self.status = f"All {len(self.batch_sources)} media item(s) now inherit global settings."

    def choose_input(self) -> None:
        start = self.input_path.parent if self.input_path else Path.cwd()
        chosen = self.file_browser(start)
        if chosen is None:
            return
        if self.audio_preview:
            self.audio_preview.stop()
        self.input_path = chosen.resolve()
        self.resume_state_path = None
        self.batch_settings.clear()
        self.batch_edit_source = None
        self.batch_selection_index = 0
        self.batch_plan_path = None
        self.active_entries = None
        self.batch_enabled.clear()
        self.batch_metadata.clear()
        self.output_path = (
            core.default_batch_output(self.input_path)
            if self.input_path.is_dir()
            else core.default_output(self.input_path)
        )
        self.input_payload = None
        self.output_payload = None
        self.info = None
        self.batch_sources = []
        self.probe_current()

    def planned_resume_entries(self) -> list[dict[str, Any]]:
        if self.worker and self.worker.is_alive() and self.active_entries is not None:
            return self.active_entries
        if not self.input_path or not self.output_path:
            raise ValueError("Select an input and output before saving a resume file.")
        if self.input_path.is_dir():
            sources = self.batch_sources or core.batch_video_sources(self.input_path)
            if not sources:
                raise ValueError("No supported videos are available for batch resume.")
            saved_targets = ({Path(item['source']): item['output'] for item in workflow.read_json(self.resume_state_path)['entries']}
                             if workflow.legacy_state(self.resume_state_path) else {})
            return [
                {
                    "source": str(source),
                    "output": saved_targets.get(source, str(core.batch_output_path(self.input_path, self.output_path, source))),
                    "settings": asdict(self.settings_for_batch_source(source)),
                    "enabled": self.batch_enabled.get(source, True),
                    "fingerprint": workflow.fingerprint(source),
                }
                for source in sources
            ]
        if not self.input_path.is_file():
            raise ValueError("The selected input file no longer exists.")
        return [{"source": str(self.input_path), "output": str(self.output_path),
                 "fingerprint": workflow.fingerprint(self.input_path),
                 "audio_tracks": sorted(self.selected_audio_tracks),
                 "subtitle_tracks": sorted(self.selected_subtitle_tracks)}]

    def recovery_path(self) -> Path | None:
        if not self.output_path or not self.input_path:
            return None
        if workflow.legacy_state(self.resume_state_path):
            return self.resume_state_path
        return workflow.recovery_path(self.output_path, self.input_path.is_dir())

    def show_preview(self) -> None:
        if self.av_inline:
            self.av_inline.close(wait=False)
            self.av_inline = None
            self.av_inline_key = None
        av_preview.run_view(self)

    def save_resume_state(self) -> Path:
        if not self.input_path or not self.output_path:
            raise ValueError("Select an input and output before saving a resume file.")
        resume_path = self.recovery_path()
        assert resume_path is not None
        entries = self.planned_resume_entries()
        # Retain the identity of an old output while saving an active/resumed
        # replacement. Otherwise recovery could mistake it for the new result.
        active = bool(self.worker and self.worker.is_alive())
        existing_state = (resume_path if active and resume_path.is_file() else self.resume_state_path)
        if existing_state and existing_state.is_file() and (self.resume_state_path or active):
            saved = workflow.read_json(existing_state)
            if saved.get("input_path") == str(self.input_path) and saved.get("output_path") == str(self.output_path):
                old_entries = {item["source"]: item for item in saved.get("entries", [])}
                for entry in entries:
                    old = old_entries.get(entry["source"], {})
                    if old.get("fingerprint") == entry.get("fingerprint") and "previous_output" in old:
                        entry["previous_output"] = old["previous_output"]
        payload = {
            "version": core.RESUME_STATE_VERSION,
            "application": "VitaMediaDeck Transcoder",
            "layout": 'legacy' if workflow.legacy_state(self.resume_state_path) else workflow.LAYOUT,
            "mode": "batch" if self.input_path.is_dir() else "file",
            "input_path": str(self.input_path),
            "output_path": str(self.output_path),
            "settings": asdict(self.settings),
            "entries": entries,
            "saved_at": int(time.time()),
        }
        resume_path.parent.mkdir(parents=True, exist_ok=True)
        workflow.atomic_json(resume_path, payload)
        try:
            resume_path.chmod(0o600)
        except OSError:
            pass
        self.resume_state_path = resume_path.resolve()
        self.resume_saved_path = self.resume_state_path
        return self.resume_state_path

    def save_batch_plan(self, replace_outputs: set[Path] | None = None) -> Path:
        if not self.input_path or not self.input_path.is_dir() or not self.output_path:
            raise ValueError("Select a directory and output folder before saving a batch plan.")
        plan_path = workflow.support_directory(self.output_path, True) / "batch-plan.json"
        entries = self.planned_resume_entries()
        for entry in entries:
            if Path(entry["output"]) in (replace_outputs or set()):
                entry["settings"]["overwrite"] = True
        payload = {
            "version": core.RESUME_STATE_VERSION,
            "application": "VitaMediaDeck Transcoder", "layout": workflow.LAYOUT,
            "mode": "batch",
            "settings": asdict(self.settings),
            "input_path": str(self.input_path),
            "output_path": str(self.output_path),
            "entries": entries,
        }
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        workflow.atomic_json(plan_path, payload)
        try:
            plan_path.chmod(0o600)
        except OSError:
            pass
        self.batch_plan_path = plan_path.resolve()
        return self.batch_plan_path

    def edit_output(self) -> None:
        current = str(self.output_path or "")
        is_batch = bool(self.input_path and self.input_path.is_dir())
        value = self.prompt("Output folder" if is_batch else "Output .mkv path", current)
        if value:
            candidate = Path(value).expanduser()
            if not is_batch and candidate.suffix.lower() != ".mkv":
                candidate = candidate.with_suffix(".mkv")
            candidate = candidate.resolve()
            self.output_path = (candidate if is_batch or candidate == self.input_path else workflow.output_file(candidate).resolve())
            self.status = "Output folder updated." if is_batch else "Output path updated."

    def probe_current(self) -> None:
        if not self.input_path or not (self.input_path.is_file() or self.input_path.is_dir()):
            self.status = "Select a valid input video or folder first."
            return
        if self.input_path.is_dir():
            self.info = None
            self.input_payload = None
            self.output_payload = None
            self.capabilities = None
            self.track_choices = []
            self.selected_audio_tracks.clear()
            self.selected_subtitle_tracks.clear()
            discovered = core.batch_video_sources(self.input_path)
            self.batch_sources = [item for item in self.batch_sources if item in discovered] + [
                item for item in discovered if item not in self.batch_sources
            ]
            try:
                _, probe, _ = core.resolve_media_tools(self.settings.ffmpeg, self.settings.ffprobe)
                for item in self.batch_sources:
                    try:
                        info = core.probe_media(probe, item, self.settings_for_batch_source(item).force_hdr)
                        self.batch_metadata[item] = {"duration": info.duration, "hdr": info.hdr, "audio_count": info.audio_stream_count, "subtitle_count": len(info.subtitle_codecs), "status": "pending", "_media_info": info}
                    except (core.TranscodeError, OSError, ValueError) as exc:
                        self.batch_metadata[item] = {"duration": None, "status": "invalid", "error": str(exc)}
            except core.TranscodeError as exc:
                self.status = str(exc)
            self.batch_settings = {
                source: settings
                for source, settings in self.batch_settings.items()
                if source in self.batch_sources
            }
            if self.batch_edit_source not in self.batch_sources:
                self.batch_edit_source = None
            self.batch_selection_index = min(
                self.batch_selection_index,
                max(0, len(self.batch_sources) - 1),
            )
            if not self.batch_sources:
                self.probe_error = "No supported video files found recursively."
                self.status = "No videos found in the selected folder."
            else:
                self.probe_error = ""
                self.status = (
                    f"Batch ready: {len(self.batch_sources)} video(s) found. "
                    "Press T to convert recursively."
                )
            return
        self.status = "Inspecting media and FFmpeg capabilities..."
        if self.editor_primary:
            self.add(1, 2, self.status, self.color(4))
            self.screen.refresh()
        else:
            self.draw()
        try:
            ffmpeg, ffprobe, tool_origin = core.resolve_media_tools(
                self.settings.ffmpeg,
                self.settings.ffprobe,
            )
            self.settings.ffmpeg = ffmpeg
            self.settings.ffprobe = ffprobe
            self.info = core.probe_media(ffprobe, self.input_path, self.settings.force_hdr)
            payload = core.run_capture(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-show_chapters",
                    "-of",
                    "json",
                    str(self.input_path),
                ],
                "ffprobe",
            )
            self.input_payload = json.loads(payload)
            audio_count = self.info.audio_stream_count
            subtitle_count = len(self.info.subtitle_codecs)
            if self.selection_source != self.input_path:
                self.selected_audio_tracks = set(range(audio_count))
                self.selected_subtitle_tracks = set(range(subtitle_count))
                self.track_selection_index = 0
            else:
                self.selected_audio_tracks.intersection_update(range(audio_count))
                self.selected_subtitle_tracks.intersection_update(range(subtitle_count))
            self.track_choices = [
                *[("audio", ordinal) for ordinal in range(audio_count)],
                *[("subtitle", ordinal) for ordinal in range(subtitle_count)],
            ]
            if self.track_choices:
                self.track_selection_index = min(
                    self.track_selection_index,
                    len(self.track_choices) - 1,
                )
            self.selection_source = self.input_path
            self.capabilities = core.discover_capabilities(ffmpeg)
            self.inspect_output()
            self.probe_error = ""
            self.status = (
                f"Inspection complete with {tool_origin}. "
                "Review Info, Video, Audio and Subtitles."
            )
        except (core.TranscodeError, OSError, json.JSONDecodeError) as exc:
            self.info = None
            self.input_payload = None
            self.capabilities = None
            self.probe_error = str(exc).replace("\n", " ")
            self.status = "Inspection failed. See Info."

    def start_conversion(self) -> None:
        if self.worker and self.worker.is_alive() or self.process and self.process.poll() is None:
            self.status = "A conversion is already running."
            return
        # A finished worker may have queued its final event after the last UI
        # refresh. Consume it before resetting the next conversion to zero.
        self.drain_events()
        if not self.input_path or not (self.input_path.is_file() or self.input_path.is_dir()) or not self.output_path:
            self.status = "Select and inspect an input video or folder first."
            return
        is_batch = self.input_path.is_dir()
        if not is_batch and not workflow.legacy_state(self.resume_state_path) and self.output_path.parent in self.input_path.parents:
            self.status = 'Choose a conversion folder outside the original file’s folder.'
            return
        if is_batch and not any(self.batch_enabled.get(item, True) for item in self.batch_sources):
            self.status = "Enable at least one item with Enter in Queue."
            return
        if is_batch and not self.batch_sources:
            self.status = "No videos found in the selected folder."
            return
        if is_batch and (self.output_path == self.input_path or self.input_path in self.output_path.parents):
            self.status = "Choose an output folder outside the input folder."
            return
        settings_to_check = [self.settings]
        if is_batch:
            settings_to_check.extend(self.batch_settings.values())
        if any(
            setting.cover_mode == "custom"
            and not Path(setting.cover_image).expanduser().is_file()
            for setting in settings_to_check
        ):
            self.status = "Select a valid custom cover image in General."
            return
        try:
            entries = self.planned_resume_entries()
            if any(Path(entry["source"]).resolve() == Path(entry["output"]).resolve()
                   for entry in entries if entry.get("enabled", True)):
                self.status = "Input and output paths must be different."
                return
            conflicts = {Path(entry["output"]) for entry in entries
                         if entry.get("enabled", True) and Path(entry["output"]).exists()
                         and not entry.get("settings", asdict(self.settings)).get("overwrite", False)}
            replace_outputs: set[Path] = set()
            if conflicts and not self.resume_state_path:
                first = sorted(conflicts)[0].name
                if not self.confirm(f"Replace {len(conflicts)} existing output file(s), including {first}? "
                                    "Each old file is kept until its replacement passes validation."):
                    self.status = "Conversion cancelled; existing outputs were kept."
                    return
                replace_outputs = conflicts
            # Fresh TUI snapshot includes order, exclusions and per-file settings.
            # Validate the original manifest before refreshing its fingerprint.
            if self.resume_state_path:
                core.resume_entries(self.resume_state_path, "batch" if is_batch else "file", self.input_path, self.output_path)
                self.save_resume_state()
            batch_plan = self.save_batch_plan(replace_outputs) if is_batch and not self.resume_state_path else None
            if not is_batch and not self.resume_state_path:
                previous = self.resume_state_path
                self.save_resume_state()
                self.resume_state_path = previous
        except (OSError, ValueError, core.TranscodeError) as exc:
            self.status = f"Could not save per-media batch settings: {exc}"
            return
        command = (
            self.settings.command(
                self.script,
                self.input_path,
                self.output_path,
                resume_state=self.resume_state_path,
                batch_plan=batch_plan,
            )
            if is_batch
            else self.settings.command(
                self.script,
                self.input_path,
                self.output_path,
                sorted(self.selected_audio_tracks),
                sorted(self.selected_subtitle_tracks),
                self.resume_state_path,
            )
        )
        if not is_batch and replace_outputs and "--overwrite" not in command:
            command.append("--overwrite")
        self.logs.clear()
        self.logs.append("$ " + core.command_text(command))
        preview_source, preview_settings = self.preview_subject()
        self._conversion_preview_subject = (preview_source, Settings.from_dict(asdict(preview_settings)))
        self.progress_seconds = None
        self.live = LiveProgress(started_at=time.monotonic())
        self.batch_weights = (0.0, 1.0, 1.0)
        self.paused = False
        self.suspended_processes.clear()
        self.tab = TABS.index("Log")
        self.status = "Starting conversion..."
        self.active_entries = self.planned_resume_entries()
        self.worker = threading.Thread(target=self.conversion_worker, args=(command,), daemon=True)
        self.worker.start()

    def conversion_worker(self, command: list[str]) -> None:
        creationflags = 0
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        session_log = None
        try:
            if self.output_path:
                log_path = (workflow.support_directory(self.output_path, True) / "session.log"
                            if self.input_path and self.input_path.is_dir()
                            else workflow.media_support_directory(self.output_path) / "transcode.log")
                if workflow.legacy_state(self.resume_state_path):
                    log_path = (workflow.legacy_directory(self.output_path, True) / 'session.log'
                                if self.input_path and self.input_path.is_dir() else workflow.legacy_directory(self.output_path) / 'transcode.log')
                log_path.parent.mkdir(parents=True, exist_ok=True)
                session_log = log_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=0,
                creationflags=creationflags,
                env={**os.environ, "VMD_MACHINE_PROGRESS": "1"},
                **kwargs,
            )
            self.process = process
            self.events.put(("started", process.pid))
            assert process.stdout is not None
            buffer = ""
            while True:
                chunk = process.stdout.read(512)
                if not chunk:
                    break
                if session_log:
                    session_log.write(chunk)
                    session_log.flush()
                buffer += chunk
                parts = re.split(r"[\r\n]", buffer)
                buffer = parts.pop()
                for line in parts:
                    if line.strip():
                        self.events.put(("line", line.rstrip()))
            if buffer.strip():
                self.events.put(("line", buffer.rstrip()))
            code = process.wait()
            self.events.put(("done", code))
        except OSError as exc:
            self.events.put(("failure", str(exc)))
        finally:
            if session_log:
                session_log.close()

    def drain_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "started":
                self.status = (
                    "Conversion paused. Press P to resume or A to abort."
                    if self.paused
                    else f"Conversion running (PID {value})..."
                )
            elif kind == "line":
                line = str(value)
                if line.startswith(core.PROGRESS_PREFIX):
                    try:
                        self.handle_machine_progress(
                            json.loads(line[len(core.PROGRESS_PREFIX) :])
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        self.logs.append(f"Malformed progress event: {line}")
                else:
                    self.logs.append(line)
                    self.update_ffmpeg_stats(line)
            elif kind == "done":
                self.process = None
                self.paused = False
                self.suspended_processes.clear()
                recovery = self.recovery_path()
                if self.input_path and self.input_path.is_dir() and recovery and recovery.is_file():
                    state = workflow.read_json(recovery)
                    self.batch_metadata = {Path(item["source"]): {**self.batch_metadata.get(Path(item["source"]), {}), **item} for item in state["entries"]}
                if value == 0:
                    self.live.phase = "complete"
                    self.live.phase_label = "COMPLETE"
                    self.live.phase_ratio = 1.0
                    self.live.local_ratio = 1.0
                    self.live.overall_ratio = 1.0
                    self.live.eta_seconds = 0.0
                    self.status = (
                        f"Batch completed: {self.live.batch_total} output(s) validated successfully."
                        if self.live.batch_total
                        else "Conversion completed and validated successfully."
                    )
                    self.inspect_output()
                else:
                    self.live.phase = "error"
                    self.status = f"Conversion failed with exit status {value}. See Log."
            elif kind == "failure":
                self.process = None
                self.paused = False
                self.suspended_processes.clear()
                self.logs.append(f"Error: {value}")
                self.status = f"Could not start conversion: {value}"

    def apply_batch_context(self, payload: dict[str, Any]) -> None:
        """Reset the movie timeline and select duration weights before each item."""
        try:
            index, total = int(payload.get("batch_index") or 0), int(payload.get("batch_total") or 0)
        except (TypeError, ValueError):
            return
        if not (1 <= index <= total):
            return
        new_item = index != self.live.batch_index or total != self.live.batch_total
        weights = tuple(video_progress.finite_number(payload.get(key), fallback) for key, fallback in
                        (("batch_completed_weight", index - 1), ("batch_weight", 1), ("batch_total_weight", total)))
        self.batch_weights = weights
        self.live.batch_index, self.live.batch_total = index, total
        self.live.batch_source = str(payload.get("batch_source") or "")
        if new_item:
            self.live.phase_ratio = self.live.local_ratio = self.live.media_seconds = 0.0
            self.live.video_fps, self.live.video_finished = 0.0, False
            self.live.media_duration = video_progress.finite_number(payload.get("batch_duration")) or None
            self.live.eta_seconds = None
            self.progress_seconds = None
            self.live.frame = self.live.fps = self.live.quality = self.live.size = self.live.bitrate = self.live.speed = "-"
            self.set_local_progress(0.0)
            item = self.input_path / self.live.batch_source if self.input_path else None
            sources = getattr(self, 'batch_sources', [])
            if item in sources:
                self.batch_selection_index = sources.index(item)
                if self.batch_edit_source is not None:
                    self.batch_edit_source = item

    def set_local_progress(self, ratio: float) -> None:
        self.live.local_ratio = max(self.live.local_ratio, max(0.0, min(1.0, video_progress.finite_number(ratio))))
        if self.live.batch_total:
            completed, current, total = self.batch_weights
            self.live.overall_ratio = min(1.0, (completed + current * self.live.local_ratio) / max(1.0, total))
        else:
            self.live.overall_ratio = self.live.local_ratio

    def handle_machine_progress(self, payload: dict[str, Any]) -> None:
        self.apply_batch_context(payload)
        phase, state = str(payload.get("phase") or "unknown"), str(payload.get("state") or "start")
        detail = str(payload.get("detail") or "")
        if phase == "queue":
            if self.input_path and self.input_path.is_dir():
                self.batch_metadata.setdefault(self.input_path / self.live.batch_source, {}).update(status=state, error=detail)
            if state in {"done", "skipped"}:
                self.set_local_progress(1.0)
            elif state == "failed":
                # This queue item has been attempted; do not light its unencoded frames.
                completed, current, total = self.batch_weights
                self.live.overall_ratio = min(1.0, (completed + current) / max(1.0, total))
            self.status = f"{self.live.batch_source}: {state}. {detail}"
            return
        self.live.phase = phase
        self.live.phase_label = PHASE_RANGES.get(phase, (0, 0, phase.upper()))[2]
        self.live.detail = detail
        if phase in {"error", "cancelled"}:
            self.live.phase_label = "FAILED" if phase == "error" else "CANCELLED"
            self.status = f"{self.live.phase_label}: {detail}"
            return
        if self.input_path and self.input_path.is_dir() and self.live.batch_source:
            self.batch_metadata.setdefault(self.input_path / self.live.batch_source, {})["status"] = "done" if phase == "complete" else "running"
        if phase == "transcode" and state in {"video_start", "fallback"}:
            self.live.local_ratio = self.live.phase_ratio = self.live.media_seconds = 0.0
            self.live.video_finished = False
            self.live.frame = "0"
            self.live.video_fps = video_progress.finite_number(payload.get("video_fps"))
            self.live.media_duration = video_progress.finite_number(payload.get("duration")) or self.live.media_duration
            self.set_local_progress(0.0)
        elif phase == "transcode" and state == "progress" and not self.live.video_finished:
            self.live.media_duration = video_progress.finite_number(payload.get("duration")) or self.live.media_duration
            self.live.video_fps = video_progress.finite_number(payload.get("video_fps")) or self.live.video_fps
            if payload.get("frame") is not None and self.live.video_fps > 0:
                self.live.frame = str(payload["frame"])
                seconds, ratio = video_progress.encoded_position(payload["frame"], self.live.video_fps, self.live.media_duration)
            else:
                seconds = video_progress.finite_number(payload.get("media_seconds"))
                ratio = video_progress.finite_number(payload.get("progress"))
            self.live.media_seconds = max(self.live.media_seconds, seconds)
            self.set_local_progress(ratio or 0.0)
            self.live.phase_ratio = self.live.local_ratio
        elif phase == "transcode" and state in {"video_done", "done"}:
            self.live.video_finished = True
            self.live.media_seconds = self.live.media_duration or self.live.media_seconds
            self.live.phase_ratio = 1.0
            self.set_local_progress(1.0)
        elif phase == "transcode" and state == "stage":
            self.live.phase_label = "ENCODING AUDIO" if "AUDIO PASS" in detail else "ASSEMBLING OUTPUT"
        elif phase == "complete":
            self.live.video_finished = True
            self.live.phase_ratio = 1.0
            self.set_local_progress(1.0)
        # Analysis, loading, preflight and packaging do not advance video progress.
        self.status = self.progress_status()

    def update_ffmpeg_stats(self, line: str) -> None:
        if self.live.phase != "transcode" or self.live.video_finished or "AUDIO PASS" in self.live.detail or "FINAL REMUX" in self.live.detail:
            return
        frames = video_progress.frame_count(line)
        if frames is None:
            return
        for attribute, pattern in {"fps": r"fps=\s*([^\s]+)", "quality": r"q=\s*([^\s]+)",
                "size": r"size=\s*([^\s]+)", "bitrate": r"bitrate=\s*([^\s]+)", "speed": r"speed=\s*([^\s]+)"}.items():
            match = re.search(pattern, line)
            if match:
                setattr(self.live, attribute, match.group(1))
        self.live.frame = str(frames)
        fps = self.live.video_fps or float(getattr(self.info, 'fps', 0) or 0)
        duration = self.live.media_duration or getattr(self.info, 'duration', None)
        seconds, ratio = video_progress.encoded_position(frames, fps, duration)
        self.live.media_seconds = self.progress_seconds = max(self.live.media_seconds, seconds)
        self.live.media_duration = duration
        self.set_local_progress(ratio or 0.0)
        self.live.phase_ratio = self.live.local_ratio
        speed = video_progress.finite_number(self.live.speed.rstrip("x"))
        self.live.eta_seconds = max(0.0, duration - seconds) / speed if duration and speed > 0 else None
        self.status = self.progress_status()

    def progress_status(self) -> str:
        local_label = (
            f"ITEM {self.live.batch_index}/{self.live.batch_total}"
            if self.live.batch_total
            else "LOCAL"
        )
        if self.paused:
            return (
                f"PAUSED: {local_label} {self.live.local_ratio * 100:.1f}%"
                f" · OVERALL {self.live.overall_ratio * 100:.1f}% · PRESS P TO RESUME"
            )
        detail = f" · {self.live.detail}" if self.live.detail else ""
        if self.live.started_at and self.live.overall_ratio > 0.02 and not self.paused:
            elapsed = time.monotonic() - self.live.started_at
            if elapsed > 10:
                estimate = elapsed * (1 - self.live.overall_ratio) / self.live.overall_ratio
                detail += f" · EST. QUEUE ETA {duration_text(estimate)}"
            else:
                detail += " · ETA CALIBRATING"
        return (
            f"{self.live.phase_label}: {local_label} {self.live.local_ratio * 100:.1f}%"
            f" · OVERALL {self.live.overall_ratio * 100:.1f}%{detail}"
        )

    def inspect_output(self) -> None:
        if not self.output_path or not self.output_path.is_file():
            return
        try:
            ffprobe = core.resolve_tool(self.settings.ffprobe, "ffprobe")
            payload = core.run_capture(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-show_chapters",
                    "-of",
                    "json",
                    str(self.output_path),
                ],
                "output inspection",
            )
            self.output_payload = json.loads(payload)
        except (core.TranscodeError, OSError, json.JSONDecodeError) as exc:
            self.logs.append(f"Output inspection warning: {exc}")

    def cancel_conversion(self) -> None:
        process = self.process
        if not process or process.poll() is not None:
            self.status = "No conversion is running."
            return
        try:
            if self.paused:
                self.resume_conversion(update_status=False)
            if os.name == "nt":
                ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
                if ctrl_break is not None:
                    process.send_signal(ctrl_break)
                else:
                    process.terminate()
            else:
                os.killpg(process.pid, signal.SIGINT)
            self.status = "Cancellation requested..."
        except OSError as exc:
            self.status = f"Could not cancel conversion: {exc}"

    def toggle_pause(self) -> None:
        process = self.process
        if not process or process.poll() is not None:
            self.status = "No conversion is running."
            return
        if self.paused:
            self.resume_conversion()
        else:
            self.pause_conversion()

    def pause_conversion(self) -> None:
        process = self.process
        if not process or process.poll() is not None or self.paused:
            return
        try:
            if os.name == "nt":
                if psutil is None:
                    raise RuntimeError(
                        "Pause on Windows requires psutil; install requirements-tui.txt."
                    )
                parent = psutil.Process(process.pid)
                targets = [*reversed(parent.children(recursive=True)), parent]
                suspended: list[Any] = []
                try:
                    for target in targets:
                        target.suspend()
                        suspended.append(target)
                except Exception:
                    for target in suspended:
                        try:
                            target.resume()
                        except Exception:
                            pass
                    raise
                self.suspended_processes = suspended
            else:
                os.killpg(process.pid, signal.SIGSTOP)
            self.paused = True
            self.pause_started = time.monotonic()
            self.live.eta_seconds = None
            self.logs.append("Conversion paused by user.")
            self.status = "Conversion paused. Press P to resume or A to abort."
        except (OSError, RuntimeError) as exc:
            self.status = f"Could not pause conversion: {exc}"
        except Exception as exc:
            self.status = f"Could not pause conversion: {exc}"

    def resume_conversion(self, update_status: bool = True) -> None:
        process = self.process
        if not process or process.poll() is not None or not self.paused:
            return
        try:
            if os.name == "nt":
                for target in self.suspended_processes:
                    try:
                        target.resume()
                    except Exception as exc:
                        if psutil is None or not isinstance(
                            exc, (psutil.NoSuchProcess, psutil.ZombieProcess)
                        ):
                            raise
                self.suspended_processes.clear()
            else:
                os.killpg(process.pid, signal.SIGCONT)
            self.paused = False
            if self.pause_started is not None:
                self.live.started_at += time.monotonic() - self.pause_started
                self.pause_started = None
            if update_status:
                self.logs.append("Conversion resumed by user.")
                self.status = f"Conversion resumed: {self.progress_status()}"
        except OSError as exc:
            self.status = f"Could not resume conversion: {exc}"
        except Exception as exc:
            self.status = f"Could not resume conversion: {exc}"

    def prompt(self, title: str, initial: str) -> str | None:
        value = list(initial)
        cursor = len(value)
        while True:
            height, width = self.screen.getmaxyx()
            box_width = max(30, min(width - 6, 100))
            left = (width - box_width) // 2
            top = max(2, height // 2 - 2)
            self.box(top, left, 5, box_width, f"EDIT {title}", 7)
            visible_width = box_width - 6
            start = max(0, cursor - visible_width + 1)
            shown = "".join(value[start : start + visible_width])
            self.fill(top + 2, left + 3, visible_width, self.color(10))
            self.add(top + 2, left + 3, shown, self.color(10) | curses.A_BOLD, visible_width)
            self.add(top + 3, left + 3, "[ENTER] APPLY  ·  [ESC] CANCEL", self.color(8), visible_width)
            try:
                curses.curs_set(1)
                self.screen.move(top + 2, left + 3 + min(cursor - start, visible_width - 1))
            except curses.error:
                pass
            self.screen.refresh()
            key = self.screen.getch()
            if key in (10, 13):
                curses.curs_set(0)
                return "".join(value).strip()
            if key == 27:
                curses.curs_set(0)
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8) and cursor > 0:
                cursor -= 1
                del value[cursor]
            elif key == curses.KEY_DC and cursor < len(value):
                del value[cursor]
            elif key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(value), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(value)
            elif 32 <= key <= 126:
                value.insert(cursor, chr(key))
                cursor += 1

    def confirm(self, message: str, *, accept_label='CONTINUE', reject_label='CANCEL') -> bool:
        height, width = self.screen.getmaxyx()
        av_preview.clear_images(self)
        self.screen.erase()
        box_width = min(width - 6, max(44, min(100, len(message) + 6)))
        lines = textwrap.wrap(message, max(1, box_width - 6))[:max(1, height - 8)]
        left = (width - box_width) // 2
        box_height = len(lines) + 4
        top = max(1, (height - box_height) // 2)
        self.box(top, left, box_height, box_width, "CONFIRM", 5)
        for row, line in enumerate(lines):
            self.add(top + 1 + row, left + 3, line, self.color(9) | curses.A_BOLD, box_width - 6)
        self.add(top + box_height - 2, left + 3, f"[Y] {accept_label}  ·  [N/ESC] {reject_label}", self.color(8), box_width - 6)
        self.screen.refresh()
        while True:
            key = self.screen.getch()
            if key in (ord("y"), ord("Y")):
                return True
            if key in (ord("n"), ord("N"), 27):
                return False

    def file_browser(self, start: Path) -> Path | None:
        current = start.resolve()
        selected = 0
        offset = 0
        show_hidden = False
        while True:
            try:
                entries = sorted(
                    (
                        item
                        for item in current.iterdir()
                        if show_hidden or not item.name.startswith(".")
                    ),
                    key=lambda item: (not item.is_dir(), item.name.lower()),
                )
            except OSError as exc:
                self.status = f"Cannot open {current}: {exc}"
                current = current.parent
                continue
            entries = [current.parent, *entries]
            selected = min(selected, len(entries) - 1)
            height, width = self.screen.getmaxyx()
            list_height = height - 8
            offset = min(max(0, selected - list_height + 1), max(0, len(entries) - list_height))
            self.screen.erase()
            for row in range(height):
                self.fill(row, 0, width - 1, self.color(9))
            self.fill(0, 0, width - 1, self.color(6))
            self.add(0, 2, "SELECT INPUT VIDEO", self.color(6) | curses.A_BOLD)
            self.add(2, 2, "PATH", self.color(7) | curses.A_BOLD)
            self.add(2, 10, current, self.color(9), width - 12)
            self.add(3, 2, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", self.color(1), width - 4)
            for row, item in enumerate(entries[offset : offset + list_height]):
                index = offset + row
                if index == 0:
                    label = "UP  // .."
                elif item.is_dir():
                    label = f"DIR // {item.name}"
                else:
                    marker = "VID" if item.suffix.lower() in VIDEO_EXTENSIONS else "FIL"
                    try:
                        size = bytes_text(item.stat().st_size)
                    except OSError:
                        size = "NO ACCESS"
                    label = f"{marker} // {item.name}  [{size}]"
                style = self.color(2) | curses.A_BOLD if index == selected else 0
                selector = "▶ " if index == selected else "  "
                self.add(4 + row, 2, selector + label, style, width - 4)
            self.fill(height - 2, 0, width - 1, self.color(6))
            self.add(
                height - 2,
                1,
                " [ENTER] SELECT/OPEN   [D] SELECT FOLDER   [BACKSPACE] PARENT   [H] HIDDEN   [ESC] CANCEL ",
                self.color(6) | curses.A_BOLD,
                width - 2,
            )
            self.screen.refresh()
            key = self.screen.getch()
            if key == 27:
                return None
            if key == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif key == curses.KEY_DOWN:
                selected = min(len(entries) - 1, selected + 1)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - list_height)
            elif key == curses.KEY_NPAGE:
                selected = min(len(entries) - 1, selected + list_height)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                current = current.parent
                selected = 0
            elif key in (ord("h"), ord("H")):
                show_hidden = not show_hidden
                selected = 0
            elif key in (ord("d"), ord("D")):
                return current
            elif key in (10, 13):
                chosen = entries[selected]
                if chosen.is_dir():
                    current = chosen.resolve()
                    selected = 0
                elif chosen.is_file():
                    return chosen


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Terminal UI for VitaMediaDeck Transcoder")
    parser.add_argument("--version", action="version", version=f"VitaMediaDeck Transcoder {core.APP_VERSION}")
    parser.add_argument("input", type=Path, nargs="?", help="optional input video or folder")
    parser.add_argument("output", type=Path, nargs="?", help="optional output .mkv path or folder")
    parser.add_argument("--preset", help="load a built-in or saved preset")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable or path")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable or path")
    parser.add_argument("--resume-state", type=Path, help="load a saved interrupted conversion")
    parser.add_argument("--list-presets", action="store_true", help="list presets without opening the UI")
    return parser.parse_args(argv)


def selected_settings(args: argparse.Namespace, store: PresetStore) -> Settings:
    settings = Settings(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
    if not args.preset:
        return settings
    for name, _, preset in [*store.rows(), *((name, False, preset) for name, preset in LEGACY_PRESETS.items())]:
        if name.lower() == args.preset.lower():
            settings = Settings.from_dict(asdict(preset))
            settings.ffmpeg = args.ffmpeg
            settings.ffprobe = args.ffprobe
            return settings
    raise SystemExit(f"Unknown preset: {args.preset}. Use --list-presets to see available names.")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    store = PresetStore()
    if args.list_presets:
        for name, custom, _ in store.rows():
            print(f"{name}\t{'custom' if custom else 'built-in'}\t{ui.preset_hint(name)}")
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Error: the terminal UI requires an interactive terminal.", file=sys.stderr)
        return 2
    os.environ["VMD_DETECTED_PREVIEW"] = preview.detect_backend()
    preview.detect_cell_size()
    if args.resume_state:
        if args.input or args.output or args.preset:
            raise SystemExit("--resume-state cannot be combined with input, output, or --preset.")
        state = core.load_resume_state(args.resume_state)
        saved_settings = state.get("settings")
        if not isinstance(saved_settings, dict):
            raise SystemExit("The resume state does not contain valid conversion settings.")
        settings = Settings.from_dict(saved_settings)
        source = Path(str(state.get("input_path") or "")).expanduser()
        output = Path(str(state.get("output_path") or "")).expanduser()
    else:
        settings = selected_settings(args, store)
        source = args.input
        output = args.output

    app_holder: dict[str, TerminalApp] = {}

    def wrapped(screen: Any) -> None:
        app = TerminalApp(screen, source, output, settings, args.resume_state)
        app_holder["app"] = app
        app.run()

    try:
        curses.wrapper(wrapped)
    except KeyboardInterrupt:
        return 130
    saved_resume = app_holder.get("app")
    if saved_resume and saved_resume.resume_saved_path:
        print(f"Resume file saved: {saved_resume.resume_saved_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
