"""Video progress counts encoded frames, never analysis stages or input clocks."""
import math
import re


def finite_number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def encoded_position(frames, fps, duration=None):
    frames, fps = max(0, finite_number(frames)), finite_number(fps)
    seconds = frames / fps if fps > 0 else 0.0
    duration = finite_number(duration)
    if duration > 0:
        seconds = min(seconds, duration)
        return seconds, min(1.0, seconds / duration)
    return seconds, None


def frame_count(line):
    match = re.search(r'(?:^|\s)frame=\s*(\d+)', line)
    return int(match.group(1)) if match else None
