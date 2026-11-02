"""Atomic queue checkpoints shared by the CLI and terminal interface."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


SUPPORT_FOLDER = "VitaMediaDeck-support"
LAYOUT = "per-conversion-v1"


def output_file(requested: Path) -> Path:
    """Put the finished video in its own directory, without nesting twice."""
    return requested if requested.parent.name.casefold() == requested.stem.casefold() else requested.parent / requested.stem / requested.name


def support_directory(output: Path, is_batch: bool = False) -> Path:
    """Keep conversion artifacts together on the output filesystem."""
    return output if is_batch else output.parent


def media_support_directory(output: Path, batch_root: Path | None = None) -> Path:
    return output.parent


def legacy_directory(output: Path, is_batch: bool = False, batch_root: Path | None = None) -> Path:
    """Only old saved jobs retain their original artifact locations."""
    if is_batch:
        return output / SUPPORT_FOLDER
    root = (batch_root or output.parent) / SUPPORT_FOLDER
    return root / 'media' / (output.relative_to(batch_root) if batch_root else output.name)


def legacy_state(path: Path | None) -> bool:
    return bool(path and path.is_file() and read_json(path).get('layout') != LAYOUT)


def is_generated_directory(directory: Path) -> bool:
    try:
        state = read_json(directory / 'resume.json')
        return state.get('application') == 'VitaMediaDeck Transcoder' and state.get('layout') == LAYOUT
    except (OSError, ValueError):
        return False


def recovery_path(output: Path, is_batch: bool = False) -> Path:
    directory = support_directory(output, True) if is_batch else media_support_directory(output)
    return directory / "resume.json"


def find_recovery(output: Path, is_batch: bool = False) -> Path:
    """Find a job checkpoint, including older layouts for explicit recovery."""
    current = recovery_path(output, is_batch)
    old_output = output.parent.parent / output.name if not is_batch and output.parent.name.casefold() == output.stem.casefold() else output
    candidates = (current, legacy_directory(old_output, is_batch) / 'resume.json',
                  old_output / '.vitamediadeck-resume.json' if is_batch else old_output.with_suffix('.resume.json'))
    return next((path for path in candidates if path.is_file()), current)


def fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def weight(duration: float | None) -> float:
    return max(1.0, duration or 60.0)
