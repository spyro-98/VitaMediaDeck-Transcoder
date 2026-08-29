# VitaMediaDeck Transcoder

Standalone Python 3 utility for converting videos on macOS, Windows, or Linux
before copying or streaming them to a PlayStation Vita. It is an external host
tool: it is not linked into VitaMediaDeck and is never included in the VPK.

The tool accepts any container and video/audio codec that the installed FFmpeg
can decode. It produces a seekable Matroska file with this playback profile:

- H.264 High Profile, Level 3.1 or 3.2;
- fixed 960x544 canvas matching the Vita display;
- original aspect ratio retained, with black padding only when required;
- 8-bit `yuv420p` BT.709 limited-range video;
- source cadence retained as CFR up to 60 fps, including 24000/1001,
  30000/1001, 50, and 60000/1001;
- selected audio tracks converted independently to AAC-LC, stereo, 48 kHz;
- selected subtitle streams retained, along with chapters, language/title metadata,
  and Matroska subtitle-font attachments;
- an embedded 480x272 `cover.jpg` for compatible file browsers and
  media-library views, prioritizing existing input artwork before generating a
  representative SDR frame.

Matroska is intentional: MP4 cannot retain every mainstream subtitle type,
including PGS, VobSub, and ASS font attachments. VitaMediaDeck's hardware and
software packages both demux H.264/AAC from Matroska.

## VitaMediaDeck compatibility

The current VitaMediaDeck player uses the transcoder's output contract directly:

- embedded `cover.jpg` artwork is shown in local and authenticated-remote video
  cells; if a file has no usable cover, the app asynchronously extracts a
  representative H.264 frame;
- every retained AAC track is discovered and can be changed during local,
  WebDAV, SFTP, or SMB playback without restarting the video;
- embedded SubRip, ASS/SSA text, WebVTT, MOV text, generic text, and MicroDVD
  tracks can be selected or disabled from the R1 player panel;
- Western, Cyrillic, Japanese, Chinese, and Korean subtitle text uses the app's
  configurable Inter/native-Vita font stack;
- local and remote files receive separate persistent resume points, with a
  **Start from beginning** action for recovered sessions.

Bitmap subtitle streams such as PGS and VobSub may still be preserved in the
Matroska output, but the current VitaMediaDeck renderer does not display them.
The transcoder remains optional: compatible files can be played without it.

## Related repositories

| Repository | Relationship |
| --- | --- |
| [`VitaMediaDeck`](https://github.com/spyro-98/VitaMediaDeck) | PS Vita application that browses and plays the resulting files |
| [`vita-hw-decoder`](https://github.com/spyro-98/vita-hw-decoder) | Hardware-only H.264/AAC playback backend |
| [`vita-sw-decoder`](https://github.com/spyro-98/vita-sw-decoder) | CPU H.264 compatibility fallback |
| [`vita-https`](https://github.com/spyro-98/vita-https) | Hardened HTTPS and seekable WebDAV Range transport |

## Requirements

- Python 3.9 or newer;
- `ffmpeg` and `ffprobe` available on `PATH`; on macOS, an installed Homebrew
  `ffmpeg-full` pair is detected and preferred automatically;
- the appropriate current graphics driver for hardware encoding.

The automatic encoder order is:

- macOS: `h264_videotoolbox`;
- Windows/Linux with NVIDIA: `h264_nvenc`;
- Windows/Linux with AMD AMF: `h264_amf`;
- Linux with AMD/Intel VAAPI: `h264_vaapi` through
  `/dev/dri/renderD128`;
- all platforms: `libx264` fallback.

The script performs a three-second, frame-counted encoder preflight. It rejects
pipelines that exit successfully without producing enough real video frames,
then tries a safe decode path or the next encoder. Hardware encoding remains
the primary acceleration path. HDR sources use software decoding because the
`zscale`/`tonemap` chain is CPU-based; this avoids timestamp starvation and
shared-GPU-memory pressure while still allowing VideoToolbox, NVENC, AMF, or
VAAPI to encode the result. For SDR, `--no-hw-decode` remains available when a
driver behaves poorly.

During the isolated video pass, a watchdog follows actual changes to the
encoded-frame counter. Because audio is encoded separately, the reported media
clock belongs to the video and cannot run hours ahead while a slow 4K HDR
filter is still starting. A frame counter that remains unchanged for 30 real
seconds after that video clock advances is treated as stalled; the unsafe
attempt is then terminated and retried using an independently preflighted
software-decode or `libx264` fallback. Output duration and stream contracts are
validated before audio encoding, cover generation, or publication, so a
multi-hour source cannot silently become a sub-second video with complete
audio.

## Basic use

```sh
python3 vitamediadeck_transcoder.py "/path/to/input-video.mkv"
```

The default output is created beside the input:

```text
input-video.vitamediadeck.mkv
```

Choose an explicit destination:

```sh
python3 vitamediadeck_transcoder.py input.mp4 output.vitamediadeck.mkv
```

Inspect the selected profile and complete FFmpeg command without converting:

```sh
python3 vitamediadeck_transcoder.py input.mov --dry-run
```

Force a specific encoder or the deterministic software fallback:

```sh
python3 vitamediadeck_transcoder.py input.mkv --encoder nvenc
python3 vitamediadeck_transcoder.py input.mkv --encoder amf
python3 vitamediadeck_transcoder.py input.mkv --encoder vaapi
python3 vitamediadeck_transcoder.py input.mkv --encoder x264
```

The default TUI loadout is **Vita Perceptual Max**, backed by the CLI `high`
profile. It keeps the source cadence and uses this 960x544 video-rate plan:

| Source cadence | Target | VBR ceiling | VBV buffer |
| --- | ---: | ---: | ---: |
| 23.976/24 fps | 2.4 Mb/s | 4.5 Mb/s | 9 Mb |
| 25/29.97/30 fps | 2.8 Mb/s | 4.5 Mb/s | 9 Mb |
| 48/50 fps | 5.0 Mb/s | 8.0 Mb/s | 16 Mb |
| 59.94/60 fps | 5.6 Mb/s | 8.0 Mb/s | 16 Mb |

It uses H.264 High Profile, Level 3.1 up to 30 fps and Level 3.2 above 30 fps,
two B-frames, three reference frames, CABAC, a two-second GOP, and AAC-LC
stereo at 48 kHz and 192 kb/s per retained track. `libx264` uses `slow` with
CRF 18. NVENC uses P6, HQ tuning, VBR, CQ 18, spatial/temporal AQ, and
full-resolution multipass. VideoToolbox, AMF, and VAAPI use their quality/VBR
paths with the same frame-rate-dependent rate limits.

Use `--quality balanced` or `--quality compact` when smaller files are more
important than preserving the perceptual ceiling.

Long encodes default to `--system-load balanced`. It leaves two CPU cores free,
uses at most eight FFmpeg threads, caps heavy filter parallelism at four, and
launches the worker with Utility QoS on macOS. This keeps hardware acceleration
and high throughput without letting a conversion monopolize the desktop. Use
`--system-load low` for two threads and Background QoS, or
`--system-load full` only when the computer is dedicated to the conversion.

## Terminal GUI

Launch the full-screen terminal interface with:

```sh
python3 vitamediadeck_tui.py
```

An input and output can also be supplied when opening it:

```sh
python3 vitamediadeck_tui.py input.mkv output.vitamediadeck.mkv
python3 vitamediadeck_tui.py input.mkv --preset "Balanced"
```

### Interface preview

![VitaMediaDeck Transcoder overview](docs/images/tui-overview.png)

![VitaMediaDeck Transcoder conversion log](docs/images/tui-conversion.png)

The terminal UI provides:

- a dark screen-graphics interface inspired by
  [Stylow's spatial concepts for *Ghost in the Shell*](https://www.pushing-pixels.org/2019/04/19/the-art-and-craft-of-screen-graphics-interview-with-stylow.html),
  using a void-black field, cold cyan and abyssal teal structure, particle-white
  highlights, restrained hologram amber and oxidized copper energy accents,
  asymmetric information fields, and a readable reduced-color fallback;
- a keyboard-driven file browser for selecting any FFmpeg-compatible input;
- complete container, video, audio, subtitle, chapter, attachment, HDR, color,
  frame-rate, and per-audio-track bitrate inspection;
- interactive selection of exactly which audio and subtitle tracks appear in
  the output, with all tracks selected by default;
- estimated Vita output resolution, frame rate, bitrate, encoder order, and
  file size before conversion;
- exact output stream, size, duration, bitrate, track, and cover information
  after conversion;
- editing for encoder, quality, maximum frame rate, AAC bitrate, desktop load
  policy, tone mapping, HDR override, hardware decoding, covers, overwrite
  behavior, FFmpeg paths, and the VAAPI device;
- built-in presets plus named custom presets stored outside the repository;
- a seven-stage live pipeline (`SCAN`, `TEST`, `ENCODE`, `CHECK`, `COVER`,
  `MUX`, `VERIFY`) with separate overall/phase meters, frame, FPS, speed,
  current output size/bitrate, media clock, and ETA;
- a terminal-native particle visualization driven by the real output ratio:
  its point cloud is frozen while idle and animates locally only during
  conversion, alongside the reel-to-reel tape animation; frames and surrounding
  interface elements remain static;
- monotonic progress that never jumps backward when cover extraction or the
  final lossless remux begins;
- live FFmpeg logs, cancellation, and final validation.

Main controls:

| Key | Action |
| --- | --- |
| `O` | Browse for an input video |
| `U` | Edit the output path |
| `I` | Inspect input, output, and available FFmpeg capabilities |
| `R` | Start conversion |
| `C` | Cancel the running conversion |
| `Tab`, `Shift+Tab`, `1`-`5` | Change page |
| Arrow keys | Navigate and change settings |
| `Space` | Toggle the highlighted audio/subtitle track on Streams |
| `A`, `S` | Select all audio tracks or all subtitle tracks on Streams |
| `X` | Clear the highlighted track type on Streams |
| `Enter` | Edit a setting or load a preset |
| `N`, `D` | Save or delete a custom preset on the Presets page |
| `Q` | Quit |

Custom presets are saved as readable JSON in the platform configuration
directory:

- macOS: `~/Library/Application Support/VitaMediaDeck Transcoder/presets.json`;
- Windows: `%APPDATA%\\VitaMediaDeck Transcoder\\presets.json`;
- Linux: `${XDG_CONFIG_HOME:-~/.config}/vitamediadeck-transcoder/presets.json`.

The TUI uses Python's standard `curses` module. It requires no additional
package on macOS or Linux. On Windows, install the compatibility package once:

```powershell
py -m pip install -r requirements-tui.txt
py vitamediadeck_tui.py
```

The command-line converter remains available for scripts and automation.

## Embedded video covers

The default cover priority is:

1. artwork explicitly supplied with `--cover-image`;
2. an existing embedded cover from the input video;
3. a representative frame extracted from the converted SDR video.

Every selected image is normalized to a 480x272 `yuvj420p` JPEG and embedded
using the standard `cover.jpg` filename and `image/jpeg` MIME type. If existing
artwork is present but cannot be decoded, the tool reports a warning and falls
back to frame generation. Generating from the converted stream gives HDR
sources a correctly tone-mapped SDR thumbnail. Existing subtitle-font
attachments remain intact.

Use a custom image instead:

```sh
python3 vitamediadeck_transcoder.py input.mkv --cover-image poster.png
```

The custom image is resized with its aspect ratio preserved and padded when
necessary. Disable cover generation explicitly with:

```sh
python3 vitamediadeck_transcoder.py input.mkv --no-cover
```

## 4K HDR, HDR10, HLG, and Dolby Vision sources

HDR is converted to 8-bit BT.709 SDR before H.264 encoding. The high-quality
path uses software decoding, `zscale` in linear light, the selected FFmpeg
`tonemap` curve, gamut conversion, error-diffusion dithering, and then hardware
encoding when available, with `libx264` as the safe fallback. This split keeps
the CPU-only color pipeline stable without giving up the main speed benefit of
hardware H.264 encoding.

For HDR input, FFmpeg must contain both the `zscale` and `tonemap` filters.
`zscale` requires an FFmpeg build configured with `--enable-libzimg`:

```sh
ffmpeg -hide_banner -filters | grep -E 'zscale|tonemap'
```

On macOS, Homebrew's keg-only `ffmpeg-full` formula includes `zimg`. The
transcoder now detects and uses this paired binary automatically when the
default `ffmpeg`/`ffprobe` names are configured:

```sh
brew install ffmpeg-full
python3 vitamediadeck_transcoder.py input.mkv
```

An explicit tool path still overrides automatic discovery:

```sh
python3 vitamediadeck_transcoder.py input.mkv \
  --ffmpeg "$(brew --prefix ffmpeg-full)/bin/ffmpeg" \
  --ffprobe "$(brew --prefix ffmpeg-full)/bin/ffprobe"
```

If the filters are missing, the script stops instead of silently creating a
clipped, washed-out, or incorrectly tagged file. Dolby Vision conversion still
depends on the installed FFmpeg being able to decode a usable base layer;
profile-specific enhancement layers cannot be guaranteed across every build.

Use `--force-hdr` for untagged HDR sources. `mobius` is the default tone map;
`--tone-map hable` prioritizes highlight detail but is darker, while
`--tone-map reinhard` is brighter and flatter.

## Track preservation

All audio and subtitle tracks are selected by default. On the TUI Streams page,
use the arrow keys and `Space` to toggle individual tracks; `A` and `S` select
all audio or subtitle tracks, while `X` clears the highlighted track type. Each
audio row shows the bitrate reported by ffprobe, including Matroska `BPS` tags.
Output rows show the measured bitrate or the configured AAC target when the
container does not expose a per-stream value.

The CLI accepts repeatable, zero-based track ordinals. The order in the output
follows the order of these options:

```sh
python3 vitamediadeck_transcoder.py input.mkv output.mkv \
  --audio-track 1 --audio-track 0 \
  --subtitle-track 2
```

Use `--no-audio` or `--no-subtitles` to omit a complete stream type. Invalid
ordinals stop before encoding with a precise error. Selected audio tracks
retain language/title metadata while being converted to the Vita-safe AAC
format. Text subtitles stored as `mov_text`, generic text, or WebVTT are
converted to SubRip for Matroska compatibility; ASS/SSA, SubRip, PGS, VobSub,
and other compatible subtitle codecs are copied without re-encoding.

Video and audio are always encoded in isolated passes and then losslessly
remuxed with the selected subtitles and font attachments. This prevents fast
audio encoders from advancing the Matroska clock while a slow 4K HDR video
filter is still starting. Each audio track is resampled with timestamp repair,
padded or trimmed to the primary video duration, and checked after encoding.
The converter refuses to publish a partial output if any audio track ends more
than two seconds before or after the video. This protects long conversions from
producing a movie that continues after its audio has stopped without wasting
time re-encoding a valid video stream.

Output size is driven primarily by duration and total bitrate, not resolution
alone. Both the CLI conversion plan and TUI show the configured bitrate and an
estimated size before encoding; the TUI shows the actual size and storage
change after encoding.

The converted file retains every selected stream. VitaMediaDeck exposes all
retained AAC tracks and the supported text-subtitle codecs listed above; other
subtitle formats remain available to compatible desktop players.

Run the complete option reference with:

```sh
python3 vitamediadeck_transcoder.py --help
```
