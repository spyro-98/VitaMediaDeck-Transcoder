#!/usr/bin/env python3
"""Generate a small regression corpus, encode it, and decode every output.

This checks host-side media contracts. Console playback must be checked on Vita.
Usage: python3 scripts/validate-media.py /absolute/path/to/new-results-folder
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
import vmd_workflow as workflow
import vmd_preview as preview


def run(command: list[str], log: Path) -> None:
    with log.open("ab") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); see {log}")


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    inputs, outputs = root / "inputs", root / "outputs"
    inputs.mkdir()
    ffmpeg, ffprobe, _ = core.resolve_media_tools("ffmpeg", "ffprobe")
    subtitles = root / "subtitles.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:00,900\nSubtitle check\n", encoding="utf-8")
    samples = [
        ("landscape", "640x360", []),
        ("portrait", "360x640", []),
        ("anamorphic", "720x480", ["-vf", "setsar=8/9"]),
        ("vfr", "640x360", ["-vf", "select='if(lt(t,0.5),1,not(mod(n,2)))'", "-fps_mode", "vfr"]),
        ("tracks", "640x360", []),
        ("hdr", "640x360", ["-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020",
                              "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                              "-x264-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"]),
    ]
    results = []
    for name, dimensions, options in samples:
        source = inputs / f"{name}.mkv"
        command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={dimensions}:rate=30:duration=1",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1"]
        if name == "tracks":
            command += ["-i", str(subtitles), "-map", "0:v", "-map", "1:a", "-map", "1:a", "-map", "2:s", "-c:s", "srt"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", *options, str(source)]
        run(command, root / f"{name}.log")
    env = {**os.environ, "VMD_MACHINE_PROGRESS": "1"}
    with (root / "batch.log").open("w") as log:
        result = subprocess.run([sys.executable, str(Path(core.__file__)), str(inputs), str(outputs),
                                 "--encoder", "x264", "--no-hw-decode", "--no-cover", "--system-load", "low"],
                                stdout=log, stderr=subprocess.STDOUT, env=env)
    if result.returncode:
        raise RuntimeError(f"Batch failed; see {root / 'batch.log'}")
    for source in sorted(inputs.glob("*.mkv")):
        output = core.batch_output_path(inputs, outputs, source)
        info = core.probe_media(ffprobe, source, False)
        payload = core.validate_output(ffprobe, output, info.audio_stream_count, len(info.subtitle_codecs),
                                       core.target_fps(info.fps, core.MAX_FPS), False, info.duration)
        run([ffmpeg, "-v", "error", "-i", str(output), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
            root / "decode.log")
        crop = subprocess.run([ffmpeg, "-hide_banner", "-i", str(output), "-vf", "cropdetect=limit=24:round=2",
                               "-an", "-f", "null", "-"], capture_output=True, text=True)
        regions = re.findall(r"crop=(\d+):(\d+):\d+:\d+", crop.stderr)
        if source.stem in {"portrait", "anamorphic", "landscape"}:
            expected_width, expected_height = core.content_dimensions(info)
            if not regions or any(abs(actual - expected) > 8 for actual, expected in
                                  zip(map(int, regions[-1]), (expected_width, expected_height))):
                raise RuntimeError(f"Unexpected active image bounds for {source.stem}: {regions[-1:]}")
        results.append({"sample": source.stem, "status": "validated and decoded", "streams": len(payload["streams"])})
    checkpoint = workflow.recovery_path(outputs, True)
    with (root / "resume.log").open("w") as log:
        result = subprocess.run([sys.executable, str(Path(core.__file__)), str(inputs), str(outputs),
                                 "--resume-state", str(checkpoint)], stdout=log, stderr=subprocess.STDOUT)
    if result.returncode or (root / "resume.log").read_text().count("SKIPPED:") != len(samples):
        raise RuntimeError("Completed batch resume did not skip every validated output")
    rgb = bytes([255, 0, 0, 0, 255, 0] * 6)
    assert preview.sixel(rgb, 2, 6).startswith("\x1bPq")
    assert "▀" in preview.unicode(rgb, 2, 6)
    assert "f=100" in preview.kitty(b"png", 2, 2)
    assert "inline=1" in preview.iterm2(b"png", 2, 2)
    workflow.atomic_json(root / "validation.json", {"samples": results, "resume": "all skipped",
                                                   "terminal_protocols": "serialization checked; visual QA pending",
                                                   "vita_playback": "hardware check required"})
    print(root / "validation.json")


if __name__ == "__main__":
    main()
