# VitaMediaDeck Transcoder

Standalone Python utility that converts videos on macOS, Windows, and Linux
into files optimised for VitaMediaDeck on PlayStation Vita. It is an external
host tool: it is not included in the VPK.

![VitaMediaDeck Transcoder overview](docs/images/tui-overview.png)

![VitaMediaDeck Transcoder conversion log](docs/images/tui-conversion.png)

## What it produces

FFmpeg-compatible input files are converted to a seekable Matroska file with:

- H.264 High Profile, Level 3.1 up to 30 fps or Level 3.2 above 30 fps;
- a 960×544 PS Vita canvas with preserved aspect ratio and padding if needed;
- 8-bit BT.709 `yuv420p` video at the source frame rate, up to 60 fps; it
  never creates extra frames above the source cadence;
- every selected source audio track re-encoded as supported AAC-LC mono/stereo
  at 48 kHz, with its target bitrate capped at the measured source-track
  bitrate;
- selected subtitle tracks, chapters, language/title metadata, and compatible
  Matroska font attachments;
- an embedded 480×272 `cover.jpg`, using existing artwork first and a bounded
  representative video window as fallback. Near-black artwork is rejected and
  retried at a distant timestamp so it remains visible on the Vita OLED theme.

Matroska is deliberate: unlike MP4, it can retain formats such as ASS, PGS,
VobSub, and subtitle-font attachments. VitaMediaDeck demuxes H.264/AAC from
Matroska through either playback backend.

## Requirements

- Python 3.9 or newer;
- `ffmpeg` and `ffprobe` on `PATH`;
- current graphics drivers for hardware encoding.

The automatic encoder order is VideoToolbox on macOS, NVENC on NVIDIA,
AMF on AMD, VAAPI on Linux AMD/Intel, then `libx264` as the reliable fallback.
The tool runs a short frame-counted preflight and tries the next safe path if
an encoder cannot produce valid frames.

For 4K HDR input, FFmpeg must provide `zscale` and `tonemap`:

```sh
ffmpeg -hide_banner -filters | grep -E 'zscale|tonemap'
```

On macOS, Homebrew's `ffmpeg-full` is detected automatically when installed.

```sh
brew install ffmpeg-full
```

## Troubleshooting

If FFprobe reports an error such as `0x00 at pos 0 invalid as first byte of an
EBML number`, the selected file is not a valid Matroska file at byte zero. It
is normally a corrupt or incomplete copy, not a missing FFmpeg dependency.
Select the actual video rather than a macOS `._` sidecar file, then copy or
download the source again before converting it.

## Quick start

Convert a video with the recommended Vita profile:

```sh
python3 vitamediadeck_transcoder.py input.mkv
```

Choose an output path, inspect the planned FFmpeg command, or select an
encoder explicitly:

```sh
python3 vitamediadeck_transcoder.py input.mp4 output.vitamediadeck.mkv
python3 vitamediadeck_transcoder.py input.mov --dry-run
python3 vitamediadeck_transcoder.py input.mkv --encoder x264
python3 vitamediadeck_transcoder.py anime.mkv --encoder x264 --content-tune anime
```

Launch the terminal interface:

```sh
python3 vitamediadeck_tui.py
```

Or start it with a video and a preset:

```sh
python3 vitamediadeck_tui.py input.mkv output.vitamediadeck.mkv
python3 vitamediadeck_tui.py input.mkv --preset "Balanced"
```

On Windows only, install the `curses` compatibility package once:

```powershell
py -m pip install -r requirements-tui.txt
py vitamediadeck_tui.py
```

### Windows executable

Every published GitHub release includes `VitaMediaDeck-Transcoder.exe`, a
console executable that opens the terminal interface directly. FFmpeg and
FFprobe remain external dependencies: install them and make both available on
`PATH` before launching the executable.

To build it locally on Windows:

```powershell
py -m pip install -r requirements-tui.txt pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

The output is `artifacts\windows\VitaMediaDeck-Transcoder.exe`.

## Terminal interface

The TUI includes a file browser, input/output inspection, encoder and quality
settings, editable presets, per-track audio/subtitle selection, live FFmpeg
logs, progress, speed, bitrate, ETA, cancellation, and final validation.

| Key | Action |
| --- | --- |
| `O` / `U` | Select input / edit output path |
| `I` / `R` / `C` | Analyze / start / cancel conversion |
| `Tab`, `Shift+Tab`, `1`–`5` | Change page |
| Arrow keys, `Space` | Navigate and toggle a selected stream |
| `A`, `S`, `X` | Select all audio, all subtitles, or clear a stream type |
| `N`, `D` | Save or delete a custom preset |
| `Q` | Quit |

## Quality, HDR, and system load

The default **Vita Perceptual Max** profile targets approximately 2.4 Mb/s at
24 fps, 2.8 Mb/s at 30 fps, and 5.6 Mb/s at 60 fps. Use `--quality balanced`
or `--quality compact` when smaller files matter more.

### Content-specific tuning

The compatibility contract does not change between presets: every output
remains 960×544 H.264 High, `yuv420p`, source cadence up to 60 fps, and AAC-LC.
Only the encoder's perceptual decisions and bitrate curve change.

| Built-in preset | Use it for | x264 tuning | High-quality target at 24 / 30 / 60 fps |
| --- | --- | --- | --- |
| **Vita Movie Max** | Live action, digital cinema, normal television | `film`, CRF 18 | 2.4 / 2.8 / 5.6 Mb/s |
| **Vita Anime Max** | Clean digital anime, cartoons, flat colours and line art | `animation`, CRF 17.5 | 2.2 / 2.6 / 5.2 Mb/s |
| **Vita Anime Grain** | Older scanned anime with intentional photographic grain | `grain`, CRF 18 | 2.8 / 3.2 / 6.0 Mb/s |

Use **Anime Max** for most modern animation. Use **Anime Grain** only when the
source visibly contains real grain that should remain; it is intentionally
larger. Do not use the grain preset merely because an old source contains
compression noise, as preserving that noise wastes bitrate. **Movie Max** is
the normal live-action choice.

These three presets select software x264 because `film`, `animation`, and
`grain` are x264 content tunes. With VideoToolbox, NVENC, AMF, or VAAPI,
`--content-tune` still selects the corresponding safe bitrate curve, while the
hardware encoder continues using its native high-quality mode. Keep **Vita
Perceptual Max** for the fastest automatic hardware-first conversion.

Long conversions default to `--system-load balanced`: two CPU cores remain
available, heavy filters are capped, and macOS uses Utility QoS. Use
`--system-load low` for a lighter desktop impact, or `--system-load full` when
the computer is dedicated to conversion.

HDR sources are tone-mapped to 8-bit BT.709 SDR before H.264 encoding. The
tool stops if the required filters are missing rather than creating a visibly
incorrect file. Use `--force-hdr` for untagged HDR content.

## Tracks and covers

All audio and subtitle tracks are selected by default. In the TUI, select the
tracks on the **Streams** page; the CLI also accepts repeatable zero-based
`--audio-track` and `--subtitle-track` options. `--no-audio` and
`--no-subtitles` omit an entire stream type.

Cover priority is: `--cover-image`, embedded input artwork, then an extracted
SDR video frame. Disable it with `--no-cover`.

The converter encodes video and audio separately, repairs timestamps, aligns
audio to the final video duration, then remuxes the selected streams. It
validates the AAC-LC profile, mono/stereo channel count, 48 kHz sample rate,
duration, and stream contracts before publishing the output.

The video-output audio contract is AAC-LC regardless of whether the source is
AAC, MP3, FLAC, AC-3, DTS, or another format FFmpeg can decode. This is
intentional: direct music-file support in VitaMediaDeck is separate from the
H.264/AAC movie playback contract.

The requested audio bitrate is a ceiling, not an upsampling control. A 192 kb/s
source track therefore produces AAC at no more than 192 kb/s. If a container
omits the stream bitrate, the tool measures the audio packet sizes before
conversion instead of trusting the container's overall bitrate. Similarly, the
source frame rate is preserved (and capped at 60 fps): a 23.976 fps film is not
turned into 60 fps, which would add duplicate/interpolated frames without
improving source detail.

## Related repositories

| Repository | Purpose |
| --- | --- |
| [`VitaMediaDeck`](https://github.com/spyro-98/VitaMediaDeck) | PS Vita media browser and player |
| [`vita-hw-decoder`](https://github.com/spyro-98/vita-hw-decoder) | Hardware H.264/AAC playback backend |
| [`vita-sw-decoder`](https://github.com/spyro-98/vita-sw-decoder) | CPU playback fallback |
| [`vita-https`](https://github.com/spyro-98/vita-https) | HTTPS and seekable remote transport |

Run `python3 vitamediadeck_transcoder.py --help` for every CLI option.
