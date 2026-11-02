# VitaMediaDeck Transcoder

**1.1.0-beta.1** · `beta` branch · based on `dev`

A terminal AV editor and FFmpeg transcoder for preparing movies and series for
[VitaMediaDeck on PlayStation Vita](https://github.com/spyro-98/VitaMediaDeck).
Adjust video and audio settings, compare the converted preview with the source,
choose tracks and review the result before converting the complete file.

This runs on your **computer**. It is a separate host tool, not part of the VPK.
The editor changes conversion settings; it does not cut or modify your source.

[Getting started](#getting-started) · [Controls](#editor-controls) ·
[Presets](#vita-playback-presets) · [Folders and recovery](#folders-and-recovery) ·
[Changelog](CHANGELOG.md) · [Validation](docs/validation.md)

## What is new in this beta

- Always-visible AV preview with manually started playback, synchronized audio,
  captions and filmstrip. Parameters occupy the left quarter of the editor;
  video uses the remaining width, with track lanes below.
- Original on the left, processed output on the right: toggle the comparison,
  drag its divider and keep your position while adjusting settings.
- Nine numbered sections, clear focus, categorized parameters and compact
  source → output information. Presets show what they change before application.
- A fresh preview after every parameter or track change, including returning to
  an earlier configuration. **No preview-result cache.**
- Vita HW/SW playback presets, optional filters and measured CPU/GPU selection.
- Per-video output folders, safe replacement, resumable queues and progress
  that starts at zero for every video.

This is a prerelease. Host checks and physical Vita/terminal testing are
distinct; see [validation scope](docs/validation.md#remaining-runtime-checks).

## Getting started

Requirements: **Python 3.9+**, FFmpeg and FFprobe. **FFplay** is required for
synchronized AV preview with audio. Use an interactive terminal and a large
window for the editor. The selected FFmpeg build must support your source and
chosen encoder/filters; HDR conversion needs `zscale` and `tonemap`.

```sh
git clone --branch beta https://github.com/spyro-98/VitaMediaDeck-Transcoder.git
cd VitaMediaDeck-Transcoder
python3 vitamediadeck_tui.py
```

Open a movie or series folder directly, optionally selecting a preset:

```sh
python3 vitamediadeck_tui.py "Movie.mkv"
python3 vitamediadeck_tui.py "My Series" "Converted Series"
python3 vitamediadeck_tui.py "Movie.mkv" --preset "Vita HW - Medium"
python3 vitamediadeck_tui.py --list-presets
```

On macOS, a Homebrew `ffmpeg-full` installation is detected automatically:

```sh
brew install ffmpeg-full
```

On Windows, install the terminal dependencies, then start with `py`:

```powershell
py -m pip install -r requirements-tui.txt
py vitamediadeck_tui.py
```

Linux users can run the same Python entry points with an FFmpeg installation
that includes FFprobe and FFplay. `--ffmpeg` and `--ffprobe` accept explicit paths.

Check the version with either entry point:

```sh
python3 vitamediadeck_tui.py --version
python3 vitamediadeck_transcoder.py --version
```

Keep **all Python files in this repository together** when running from source.
The editor and conversion worker share the `vmd_*.py` modules.

## Editor controls

The editor opens on the source information. The top menu contains:

| Section | Purpose |
| --- | --- |
| **1 Info** | File, video, colour, audio, subtitle and attachment metadata |
| **2 Presets** | Descriptions, setting changes and estimated current → preset results |
| **3 General** | Resource policy, tools, cover and output options |
| **4 Video** | Encoder, quality, frame rate, HDR, resizing and optional filters |
| **5 Audio** | Inclusion, source/output channels and bitrate per selected track |
| **6 Subtitles** | Included/excluded tracks and synchronized caption activity |
| **7 Review** | Changed parameters, source/output summary, retained tracks and size estimates |
| **8 Queue** | Recursive file list, per-media settings, inclusion, order and progress |
| **9 Log** | Processing output and detailed errors |

The active tab, panel border and highlighted row identify focus. Long parameters
wrap and use pages; preset/queue lists use the available height. The minimum
layout is 76 columns × 22 rows.

| Key | Action |
| --- | --- |
| `1`–`9`, Tab / Shift-Tab | Select a section / move focus |
| ↑ / ↓ | Select a parameter, preset or track |
| ← / →, `-` / `+` | Decrease / increase a parameter; numeric values stop at their limits |
| Enter | Edit a value, apply a preset or toggle the selected track/item |
| PageUp / PageDown | Page through focused content/details |
| `I` / `O` / `U` | Choose input / output / refresh inspection and preview |
| `N` | Save a named preset |
| `D` in Presets | Delete the selected custom preset |
| `S` in General/Video/Audio | Apply the field or bitrate to compatible queued files |
| `R` in Queue | Restore global defaults for the selected file |
| `T` | Open Review; press again in Review to start conversion |
| `P` / `A` | Pause or resume full conversion / abort it |
| `Q` | Confirm exit; if converting, offer a recovery file before cancelling |

In **Audio** or **Subtitles**, select a track and press **Enter** to include or
exclude it. Delete/Backspace excludes the selected track; **X** excludes all
and **E** includes all in that section. Excluded lanes are dimmed. **J** changes
the listening track without changing export inclusion.

### Preview playback and comparison

| Key | Action |
| --- | --- |
| Space / `V` | Play, pause or replay; playback never starts automatically |
| `Z` | Toggle Fit window / Vita 960×544 size, reduced if needed |
| `C` | Toggle original/processed comparison, retaining the divider position |
| `,` / `.` | Move the divider left / right |
| Mouse click / drag in video | Position the divider, when supported by the terminal |
| `[` / `]` | Previous / next source sample |
| `<` / `>` | Move ten samples backward / forward |
| `G` | Choose sample 1–100 |
| Home / End | First / last previewable part of the movie |
| `{` / `}` | Previous / next random audio excerpt |
| `L` | Play or stop the selected converted audio excerpt |
| `H` | Filter support and processing alternatives; `/` searches filters |

Navigation offers **100 evenly spaced source positions**, initially at 20% of
the movie. Each preview converts up to six seconds. Ten random audio excerpts
provide additional listening points within the source.

Every setting or inclusion change rebuilds video, selected audio, subtitles,
frames, thumbnails, waveforms and comparison data from the original. Automatic
CPU/GPU choices are measured again. Superseded workers are cancelled, old
images are cleared and only the completed current preview can be displayed or
played. Playback time and comparison-divider position are retained.

Temporary files and buffers belong only to the current sample. Replay, pause
and divider movement use that sample; they do not retrieve earlier settings.
Preview caches from older versions are ignored. Named presets and recovery
files remain supported.

The right side is decoded from the **actual encoded preview**. The left fits
the source to the same canvas without selected enhancement filters; HDR needs
a fixed conversion for the SDR terminal. Text subtitle lanes show timed plain
captions. Bitmap subtitles show timed graphic activity rather than OCR text;
retained ASS styling remains in the converted file.

The waveform indicates sound level, **not quality or progress**. Audio drives
the AV clock. Pause closes the player; resume uses PCM trimmed to the held
position. One included track is auditioned at a time, while all are exported.

Kitty graphics, iTerm2 inline images, Sixel and coloured Unicode half blocks
are chosen according to the terminal. Override with `VMD_PREVIEW=kitty`,
`iterm2`, `sixel` or `unicode`. Unicode is the fallback for unsupported terminals
and multiplexers. Vita-size display is approximate if cell dimensions are unknown.

## Vita playback presets

**HW and SW refer to the decoder on the Vita**, independently of the computer's
encoding hardware. Choose the corresponding playback decoder in VitaMediaDeck.

| Preset | Video quality | FPS ceiling | AAC ceiling per included track |
| --- | --- | --- | --- |
| Vita HW - Max perceptual | Maximum | 60 | 576 kb/s |
| Vita HW - Medium | Balanced | 60 | 160 kb/s |
| Vita HW - Light | Compact | 30 | 128 kb/s |
| Vita SW - Max perceptual | Maximum | 30 | 576 kb/s |
| Vita SW - Medium | Balanced | 30 | 160 kb/s |
| Vita SW - Light | Compact | 30 | 128 kb/s |

Audio targets are capped by each source track's measured bitrate. SW presets
use x264 fast-decode tuning, no B-frames and one reference frame to reduce
decoder work. These are starting points, not a guarantee for every Vita/source/
clock combination. Lower bitrate alone does not ensure lower decoding cost.

**Max perceptual** is the highest configured detail budget, not lossless output
or reconstruction of absent source detail. Other presets address film grain,
animation, noisy footage, gradients, soft sources and Mac GPU paths. Read the
description and current → preset comparison before application.

### Quality, bitrate and filters

- Initial settings favor **Maximum**, up to 60 fps, source-capped 576 kb/s audio
  and full processing resources. Enhancement filters start **off**.
- Maximum targets about 3.0 Mb/s at 24 fps, 3.5 Mb/s at 30 fps and 7.0 Mb/s
  at 60 fps. These are configured budgets, not guaranteed achieved bitrates.
- Bitrates are not directly comparable across codecs. A larger H.264 file
  cannot restore detail already lost in an HEVC source.
- Audio bitrate applies to **one complete stereo track**, not each channel.
  AC3 640 kb/s across six channels → AAC 192 kb/s across two channels gives
  arithmetic averages of 106.7 → 96 kb/s/channel. Bits are shared dynamically;
  this calculation does not measure perceived quality.
- Denoising can remove grain and texture, sharpening can add halos, and
  debanding can soften gradients. Compare filters before using them.
  No MLX enhancement model is bundled.
- Size/compression estimates use duration, video budgets, retained audio and
  optional preview measurements. A short sample cannot predict every scene;
  estimated and measured results are labelled separately.

**Auto** measures complete compatible pipelines: decoding, filters, CPU/GPU
transfers and encoding. Close timings favor hardware encoding; filter
alternatives need a 15% timing advantage. High/Maximum automatic resizing keeps
Lanczos. An explicit encoder stays fixed while Auto filters/resizing can still
be measured.

VideoToolbox is Apple's hardware H.264 encoder. NVENC, AMF and VAAPI cover
compatible NVIDIA, AMD and Linux hardware. x264 encodes on the CPU. Compatible
Apple resizing/Core Image filters can run on GPU even with x264; operations
such as the selected HDR tone mapper or standard NLMeans remain on CPU.
CPU/GPU filters may use different algorithms and produce different pixels.
See [GPU processing and limitations](docs/gpu-processing.md).

## Folders and recovery

Each converted video has its own folder. **The original stays outside it.**

```text
Movies/
  Movie.mkv
  Movie.vitamediadeck/
    Movie.vitamediadeck.mkv
    resume.json
    report.json
    transcode.log                 # TUI/batch log, when present
```

An explicit output `Exports/Film.mkv` becomes `Exports/Film/Film.mkv`.
Specifying that final path again does not nest it twice. New jobs do not create
a shared `VitaMediaDeck-support` folder.

Directory conversion recursively scans **only the selected root**. Siblings,
parent folders, external symlink targets and recognized generated conversion
folders are excluded. Season/subfolder paths are preserved:

```text
My Series/Season 01/Episode 01.mkv
Converted Series/Season 01/Episode 01.vitamediadeck/
  Episode 01.vitamediadeck.mkv
  resume.json
  report.json
  transcode.log
```

Review discovered paths in **Queue**. Each included file can have its own
settings and track selection. Batch-level `resume.json`, `report.json`,
`batch-plan.json` and `session.log` live in the selected batch output root;
each episode also has its own conversion data.

During conversion, the preview stays frozen and its audio stops. Playback and
preview regeneration are locked to avoid competing with encoding. The
filmstrip becomes a full-video progress strip: dark at **0%**, lighting from
left to right as frames are encoded. Loading/calibration do not count as video
progress. Every queued video starts at zero; queue progress is duration-weighted.
Audio, assembly and validation are separate phases.

For an existing output the TUI asks for confirmation; the CLI requires
`--overwrite`. Replacement happens **only after validation**. Cancellation or
failure preserves the old finished video; sources cannot be overwritten.

Press **Q** during conversion to save recovery data and cancel before exiting.
Resume with:

```sh
python3 vitamediadeck_tui.py --resume-state "/path/to/conversion/resume.json"
```

Recovery requires the same paths and unchanged sources. Completed outputs are
validated and skipped. Converter partials are removed; an incomplete episode
restarts from its beginning. Older saved jobs retain their paths and remain readable.

## Command-line conversion

The noninteractive worker supports individual videos and recursive folders:

```sh
python3 vitamediadeck_transcoder.py "Movie.mkv"
python3 vitamediadeck_transcoder.py "My Series" "Converted Series"
python3 vitamediadeck_transcoder.py "Movie.mkv" --dry-run
python3 vitamediadeck_transcoder.py "Movie.mkv" --encoder x264 --quality balanced
python3 vitamediadeck_transcoder.py "Movie.mkv" --audio-track 0 --no-subtitles
python3 vitamediadeck_transcoder.py "Movie.mkv" --system-load balanced
```

CLI track indices are **zero-based**. Repeat `--audio-track` or `--subtitle-track`
for multiple tracks; all are selected by default. Use `--no-audio` or
`--no-subtitles` to exclude an entire type. CLI recovery uses the same paths:

```sh
python3 vitamediadeck_transcoder.py "My Series" "Converted Series" \
  --resume-state "Converted Series/resume.json"
```

Run either entry point with `--help` for its options. Named presets belong to
the editor; the worker accepts their individual settings as flags.

## Output contract

FFmpeg determines input support. Common sources include MKV/MP4/MOV/WebM/AVI,
HEVC, H.264, AV1, VP9, MPEG video and multichannel/lossless audio. Single-file
inputs are probed; folder discovery uses the supported media extension list.

| Component | Result |
| --- | --- |
| Container | Seekable Matroska (`.mkv`) |
| Video | H.264 High Profile, Level 3.1 through 30 fps / 3.2 above 30 fps |
| Image | 960×544 canvas, preserved source aspect ratio with padding |
| Colour | 8-bit `yuv420p`, BT.709 limited range; detected HDR is tone-mapped to SDR |
| Frame rate | Original cadence capped by the selected ceiling, never increased, at most 60 fps |
| Audio | Selected tracks converted to AAC-LC, 48 kHz stereo, source-capped target bitrate |
| Subtitles | Selected tracks retained; MOV text/generic text/WebVTT become SRT |
| Metadata | Chapters, language/title metadata and compatible font attachments retained |
| Cover | Embedded 480×272 `cover.jpg`, from custom image, existing artwork or a frame |

## Troubleshooting and validation

- Unsupported image protocols fall back to Unicode. Use `VMD_PREVIEW=unicode`
  to investigate terminal compatibility; preview audio requires FFplay.
- Missing encoders/filters appear in **Log**. Compiled GPU filter names do not
  prove driver/device availability; Auto performs runtime trials.
- Invalid EBML/Matroska headers can indicate a damaged/incomplete source or a
  macOS `._` sidecar. Select the real media before retrying.
- Low quality can hide grain; enhancement filters can remove it. Use the split
  and disable filters when preserving texture matters.

See [validation commands and proof boundaries](docs/validation.md) for the
automated checks. The Windows packaging workflow builds a console executable;
FFmpeg tools remain external dependencies. To build locally:

```powershell
py -m pip install -r requirements-tui.txt pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

Output: `artifacts/windows/VitaMediaDeck-Transcoder.exe`. A beta branch push does
not itself publish a GitHub release or certify that executable.

## Related projects

- [VitaMediaDeck](https://github.com/spyro-98/VitaMediaDeck): PS Vita media browser/player.
- [vita-hw-decoder](https://github.com/spyro-98/vita-hw-decoder): hardware playback backend.
- [vita-sw-decoder](https://github.com/spyro-98/vita-sw-decoder): software playback backend.
- [vita-https](https://github.com/spyro-98/vita-https): HTTPS and seekable remote transport.
