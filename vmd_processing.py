"""Measure complete FFmpeg pipelines, including transfers; always measure current choices.

Auto is a throughput policy, not a claim of identical pixels across different
CPU/GPU algorithms. Explicit settings remain authoritative.
"""
from pathlib import Path
import re
import subprocess
import threading
import time

_lock = threading.RLock()
VERSION = 4


def _measure(ffmpeg, source, info, caps, encoder, hardware, fps, quality, content, tone, device, load, options, cancel_event=None):
    import vitamediadeck_transcoder as core
    command = core.build_command(ffmpeg, source, '-', info, caps, encoder, hardware, 960, 544, fps,
        quality, content, 192, tone, device, True, system_load=load, video_options=options)
    length = min(2.0, max(.1, (info.duration or 3) * .7))
    command[command.index('-t') + 1] = f'{length:.6f}'
    at = command.index('-i')
    command[at:at] = ['-ss', f'{(info.duration or 0) * .2:.6f}']
    began = time.monotonic()
    last_poll = began
    try:
        process = subprocess.Popen(core.prioritized_command(command, load), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, **core.process_priority_kwargs(load))
        while True:
            now = time.monotonic()
            if now - last_poll > 1.0:
                # The complete worker/process group can be suspended by P.
                # Do not score that stopped interval as GPU/CPU processing.
                began += now - last_poll - .1
            last_poll = now
            if cancel_event and cancel_event.is_set():
                process.terminate()
                process.communicate()
                raise core.TranscodeError('Preview cancelled during calibration.')
            if now - began > 25:
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(command, 25)
            try:
                stdout, stderr = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                continue
        frames = re.findall(r'(?m)^frame=(\d+)$', stdout)
        count = int(frames[-1]) if frames else 0
        valid = process.returncode == 0 and count >= max(1, int(float(fps) * length * .65)) and not core.filter_output_failed(stderr)
        return dict(seconds=round(time.monotonic() - began, 4) if valid else None, frames=count,
                    encoder=encoder, hw_decode=hardware, options=options,
                    error='' if valid else stderr[-400:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(seconds=None, frames=0, encoder=encoder, hw_decode=hardware, options=options, error=str(exc))


def resolve(ffmpeg, source, info, caps, encoder, hardware, fps, quality, content, tone, device, load, options, cancel_event=None):
    """Choose filters for a fixed encoder; require 15% gain to leave CPU."""
    import vitamediadeck_transcoder as core
    with _lock:
        backend, scaler = options['filter_backend'], options['scaler']
        base = {**options, 'filter_backend': 'cpu' if backend == 'auto' else backend,
                'scaler': 'lanczos' if scaler == 'auto' else scaler}
        if backend != 'auto' and scaler != 'auto':
            return base, dict(version=VERSION, options=base, policy='Explicit settings', trials=[])
        candidates = [base]
        # scale_vt exposes no Lanczos-quality interpolation setting. A timing
        # win must not silently soften High/Maximum while comparing quality.
        scalers = ['apple'] if scaler == 'auto' and quality not in ('high', 'maximum') and 'scale_vt' in caps.filters else []
        backends = ['apple-gpu'] if backend == 'auto' and 'coreimage' in caps.filters and (
            options['denoise'] in ('light', 'medium', 'strong') or options['sharpen'] != 'off') else []
        for resize in [base['scaler'], *scalers]:
            for filters in [base['filter_backend'], *backends]:
                candidate = {**base, 'scaler': resize, 'filter_backend': filters}
                if candidate not in candidates:
                    candidates.append(candidate)
        trials = []
        for candidate in candidates:
            if cancel_event and cancel_event.is_set():
                raise core.TranscodeError('Preview cancelled during calibration.')
            try:
                trials.append(_measure(ffmpeg, source, info, caps, encoder, hardware, fps, quality, content, tone, device, load, candidate, cancel_event))
            except core.TranscodeError as exc:
                trials.append(dict(seconds=None, frames=0, encoder=encoder, hw_decode=hardware, options=candidate, error=str(exc)))
        valid = [trial for trial in trials if trial['seconds'] is not None]
        winner = min(valid, key=lambda trial: trial['seconds']) if valid else None
        baseline = next((trial for trial in valid if trial['options'] == base), None)
        if baseline and winner and winner is not baseline and winner['seconds'] > baseline['seconds'] * .85:
            winner = baseline
        chosen = winner['options'] if winner else base
        report = dict(version=VERSION, options=chosen, seconds=winner['seconds'] if winner else None,
                      policy=('High/Maximum Auto preserves Lanczos; measured GPU filter mixtures' if scaler == 'auto' and quality in ('high', 'maximum')
                              else 'Measured complete pipeline; 15% gain threshold'), trials=trials)
        return dict(chosen), report


def select(ffmpeg, source, info, caps, requested, prefer_hw, fps, quality, content, tone, device, load, options, cancel_event=None):
    """Rank encoders/decoders together with filter pipelines for Auto."""
    if content == 'vita-sw' and requested == 'auto':
        requested = 'x264'
    import vitamediadeck_transcoder as core
    with _lock:
        trials = []
        for encoder in core.encoder_candidates(requested, caps, Path(device)):
            for hardware in ([True, False] if prefer_hw and not info.hdr and encoder != 'vaapi' else [False]):
                if cancel_event and cancel_event.is_set():
                    raise core.TranscodeError('Preview cancelled during calibration.')
                chosen, report = resolve(ffmpeg, source, info, caps, encoder, hardware, fps, quality, content, tone, device, load, options, cancel_event)
                matching = next((trial for trial in report['trials'] if trial['options'] == chosen and trial['seconds'] is not None), None)
                trial = dict(matching or _measure(ffmpeg, source, info, caps, encoder, hardware, fps, quality, content, tone, device, load, chosen, cancel_event))
                trial['filter_trials'] = report['trials']
                trials.append(trial)
        valid = [trial for trial in trials if trial['seconds'] is not None]
        if not valid:
            raise core.TranscodeError('No valid processing pipeline. Check selected filters and GPU availability.')
        winner = min(valid, key=lambda trial: trial['seconds'])
        hardware_encoders = [trial for trial in valid if trial['encoder'] != 'x264']
        first = min(hardware_encoders, key=lambda trial: trial['seconds']) if hardware_encoders else valid[0]
        if winner['seconds'] > first['seconds'] * .85:
            winner = first
        report = dict(version=VERSION, encoder=winner['encoder'], hw_decode=winner['hw_decode'], options=winner['options'],
                      seconds=winner['seconds'], policy='Measured pipeline; prefer hardware within 15%', trials=trials)
        return core.EncoderPlan(winner['encoder'], core.ENCODER_NAMES[winner['encoder']], winner['hw_decode'], winner['options']), report
