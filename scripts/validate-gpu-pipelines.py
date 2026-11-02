#!/usr/bin/env python3
"""Mac GPU execution checks; requires access to the desktop GPU, not just filter names."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
import vmd_processing as processing


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    ffmpeg, ffprobe, _ = core.resolve_media_tools('ffmpeg', 'ffprobe')
    source = root / 'source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30:duration=2',
                    '-c:v', 'libx264', '-preset', 'ultrafast', str(source)], check=True)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    info, caps = core.probe_media(ffprobe, source, False), core.discover_capabilities(ffmpeg)
    options = {**core.VIDEO_OPTION_DEFAULTS, 'filter_backend': 'apple-gpu', 'scaler': 'apple', 'denoise': 'light', 'sharpen': 'light'}
    results = []
    for encoder, hardware in [('x264', False), ('videotoolbox', True), ('videotoolbox', False)]:
        for interlaced in (False, True):
            output = root / f'{encoder}-{hardware}-{interlaced}.mkv'
            command = core.build_command(ffmpeg, source, str(output), replace(info, interlaced=interlaced), caps,
                encoder, hardware, 960, 544, Fraction(30), 'high', 'movie', 192, 'mobius', Path('/dev/null'),
                False, audio_tracks=[], subtitle_tracks=[], preserve_extras=False, video_options=options)
            command[-1:-1] = ['-frames:v', '6', '-loglevel', 'error']
            result = subprocess.run(command, capture_output=True, text=True)
            assert result.returncode == 0 and not core.filter_output_failed(result.stderr), result.stderr
            core.validate_output(ffprobe, output, 0, 0, Fraction(30), False, expected_attachment_count=0)
            decoded = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(output), '-map', '0:v', '-f', 'framemd5', '-']).decode()
            checksums = [line.split(',')[-1].strip() for line in decoded.splitlines() if line and not line.startswith('#')]
            assert len(checksums) == 6 and len(set(checksums)) > 1
            assert 'scale_vt' in command[command.index('-vf') + 1] and 'coreimage=' in command[command.index('-vf') + 1]
            if interlaced:
                assert 'yadif_videotoolbox' in command[command.index('-vf') + 1]
            results.append(dict(encoder=encoder, hardware_decode=hardware, interlaced=interlaced,
                                hardware_filters='scale_vt + Core Image' + (' + Metal YADIF' if interlaced else ''), status='passed'))
    selected, report = processing.select(ffmpeg, source, info, caps, 'auto', True, Fraction(30), 'high', 'movie',
                                         'mobius', Path('/dev/null'), 'balanced', core.video_options({}))
    assert selected.filter_options and report['seconds'] is not None
    again, repeated = processing.select(ffmpeg, source, info, caps, 'auto', True, Fraction(30), 'high', 'movie',
                                     'mobius', Path('/dev/null'), 'balanced', core.video_options({}))
    assert repeated['seconds'] is not None and repeated is not report
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    (root / 'validation.json').write_text(json.dumps(dict(cases=results, automatic_selection=report,
        source_unchanged=True, physical_vita_tested=False,
        timing_note='short whole-pipeline sample, including transfers; not a whole-movie speed guarantee'), indent=2) + '\n')
    print(root / 'validation.json')


if __name__ == '__main__':
    main()
