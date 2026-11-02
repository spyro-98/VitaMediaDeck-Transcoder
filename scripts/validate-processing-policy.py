#!/usr/bin/env python3
"""Deterministic policy checks complement the real GPU/CPU timing tests."""
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vitamediadeck_transcoder as core
import vmd_processing as policy


def main():
    caps = core.Capabilities(frozenset({'h264_videotoolbox', 'libx264'}), frozenset({'coreimage', 'scale_vt'}), frozenset({'videotoolbox'}))
    info = SimpleNamespace(width=1920, height=1080, hdr=False, interlaced=False)
    fixed = {**core.video_options({}), 'filter_backend': 'cpu', 'scaler': 'lanczos'}
    old = dict(encoder='x264', quality='high', content_tune='movie', audio_bitrate=192)
    restored = core.with_batch_settings(core.parse_args(['/tmp/source.mkv']), old)
    assert restored.filter_backend == 'cpu' and restored.scaler == 'lanczos'
    args = ('ffmpeg', Path('/tmp/source'), info, caps)
    tail = (Fraction(30), 'high', 'movie', 'mobius', Path('/dev/null'), 'balanced')
    def measure(times):
        def run(*values):
            encoder, hardware, options = values[4], values[5], values[12]
            value = times(encoder, hardware, options)
            return dict(seconds=value, frames=60 if value is not None else 0, encoder=encoder,
                        hw_decode=hardware, options=options, error='' if value else 'GPU not available')
        return run
    for cpu, expected in ((1.0, 'videotoolbox'), (.76, 'videotoolbox'), (.4, 'x264')):
        with patch.object(policy, '_measure', measure(lambda enc, hw, opt: cpu if enc == 'x264' else .8)):
            chosen, report = policy.select(*args, 'auto', False, *tail, fixed)
            assert chosen.key == expected and chosen.filter_options == fixed
    auto = {**fixed, 'filter_backend': 'auto', 'scaler': 'auto', 'denoise': 'light'}
    def mixture(enc, hw, opt):
        return {('cpu', 'lanczos'): 1., ('apple-gpu', 'lanczos'): .7,
                ('cpu', 'apple'): .9, ('apple-gpu', 'apple'): 1.2}[opt['filter_backend'], opt['scaler']]
    with patch.object(policy, '_measure', measure(mixture)):
        chosen, report = policy.resolve(*args, 'x264', False, *tail, auto)
        assert chosen['filter_backend'] == 'apple-gpu' and chosen['scaler'] == 'lanczos'
        assert all(trial['options']['scaler'] == 'lanczos' for trial in report['trials'])
    for quality in ('high', 'maximum'):
        requested = (Fraction(30), quality, *tail[2:])
        with patch.object(policy, '_measure', measure(mixture)):
            chosen, report = policy.resolve(*args, 'videotoolbox', False, *requested, auto)
            assert chosen['scaler'] == 'lanczos'
            assert all(trial['options']['scaler'] != 'apple' for trial in report['trials'])
    requested = (Fraction(30), 'balanced', *tail[2:])
    with patch.object(policy, '_measure', measure(lambda enc, hw, opt: .3 if opt['scaler'] == 'apple' else 1)):
        chosen, _ = policy.resolve(*args, 'videotoolbox', False, *requested, {**auto, 'denoise': 'off'})
        assert chosen['scaler'] == 'apple'
    with patch.object(policy, '_measure', measure(lambda enc, hw, opt: 1. if opt['filter_backend'] == 'cpu' and opt['scaler'] == 'lanczos' else None)):
        selected, report = policy.select(*args, 'x264', False, *tail, auto)
        assert selected.filter_options['filter_backend'] == 'cpu'
        assert all(trial['seconds'] is None for trial in report['trials'][0]['filter_trials'] if trial['options']['filter_backend'] == 'apple-gpu')
    with patch.object(policy, '_measure') as run:
        chosen, _ = policy.resolve(*args, 'videotoolbox', False, *tail, fixed)
        assert chosen == fixed and not run.called
    with patch.object(policy, '_measure', wraps=measure(mixture)) as run:
        first, _ = policy.resolve(*args, 'x264', False, *tail, auto)
        count = run.call_count
        second, _ = policy.resolve(*args, 'x264', False, *tail, auto)
        assert first == second and count > 0 and run.call_count == 2 * count
    print('Passed: fixed encoder, measured mixtures, 15% threshold, hardware ties, no reused processing decisions.')


if __name__ == '__main__':
    main()
