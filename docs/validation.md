# Validation

These scripts generate synthetic media and leave the original sources unchanged.
Run them from the repository root, using a **different, nonexistent output
directory for every invocation**. Use a temporary directory outside your media
library. Requirements are FFmpeg, FFprobe and, for AV checks, FFplay.

## Fast checks

```sh
python3 -m compileall -q .
python3 vitamediadeck_transcoder.py --version
python3 vitamediadeck_tui.py --version
python3 vitamediadeck_transcoder.py --help
python3 vitamediadeck_tui.py --help
python3 scripts/validate-editor-ui.py
python3 scripts/validate-parameter-controls.py
python3 scripts/validate-processing-policy.py
```

These cover navigation, fixed text/image regions, contextual estimates,
high defaults, monotonic controls, fractional FPS, confirmation dialogs,
responsive lists and CPU/GPU selection policy.

## Media checks

| Script | Coverage |
| --- | --- |
| `validate-media.py` | Landscape/portrait/anamorphic aspect ratio, variable cadence, HDR, stream selection and batch recovery |
| `validate-output-storage.py` | Per-conversion folders, atomic overwrite, output conflicts, generated-directory exclusions and legacy recovery |
| `validate-progress.py` | Sequential queue progress, reset to zero, restart and full-video progress strip |
| `validate-av-preview.py` | Converted streams, subtitle times, AAC targets, PCM pause/resume and native thumbnail resolution |
| `validate-preview-fresh.py` | Re-encoding after every edit, returning to earlier settings, displayed/output pixel equality, and cancelled pending/active workers |
| `validate-comparison.py` | Original/output split pixels on Kitty, iTerm2, Sixel and Unicode; fresh source decoding and mouse/keyboard routing |
| `validate-conversion-preview.py` | Frozen preview during conversion, no competing source decoders, batch transitions and waveform behavior |
| `validate-enhancements.py` | Actual filter output, FPS, settings propagation and retained playback position |
| `validate-vita-presets.py` | Vita HW/SW preset command/filter settings and actual encoded streams |

For example:

```sh
python3 scripts/validate-preview-fresh.py /absolute/path/to/new-fresh-results
python3 scripts/validate-output-storage.py /absolute/path/to/new-storage-results
```

Each media script writes `validation.json` to its output directory. Read its
contents and command exit status before calling a check passed. The AV test
uses SDL's dummy audio driver; it does not play through physical speakers.

## Mac GPU checks

```sh
python3 scripts/validate-gpu-pipelines.py /absolute/path/to/new-gpu-results
```

This requires a compatible FFmpeg build and an accessible desktop GPU. It
executes Apple filter/encoding combinations rather than inferring support from
compiled filter names. A headless failure is not evidence that a local desktop
GPU path is unsupported. See [GPU processing](gpu-processing.md).

## Remaining runtime checks

Automated host checks are separate from physical Vita playback, terminal image
presentation, speaker synchronization and Windows executable acceptance. Test
demanding scenes on the intended Vita decoder, including seeks, subtitles,
multiple audio tracks and sustained playback. Test the actual terminal and its
mouse/image protocol before describing those interactions as manually verified.
