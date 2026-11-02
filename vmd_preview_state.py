"""Preview identities and settings only; no stored rendering results."""
from pathlib import Path
import vitamediadeck_transcoder as core

VIDEO_FIELDS = (*core.VIDEO_OPTION_DEFAULTS, 'encoder', 'quality', 'content_tune', 'max_fps', 'system_load',
                'tone_map', 'force_hdr', 'hw_decode', 'vaapi_device', 'ffmpeg', 'ffprobe')


def fingerprint(path):
    path = Path(path).resolve()
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns
    except OSError:
        return str(path), None, None


def video_settings(settings):
    return tuple((key, settings.get(key)) for key in VIDEO_FIELDS)
