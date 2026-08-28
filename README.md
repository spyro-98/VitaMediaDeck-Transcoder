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
- every audio track converted independently to AAC-LC, stereo, 48 kHz;
- all subtitle streams retained, along with chapters, language/title metadata,
  and Matroska subtitle-font attachments.

Matroska is intentional: MP4 cannot retain every mainstream subtitle type,
including PGS, VobSub, and ASS font attachments. VitaMediaDeck's hardware and
software packages both demux H.264/AAC from Matroska.

## Requirements

- Python 3.9 or newer;
- `ffmpeg` and `ffprobe` available on `PATH`;
- the appropriate current graphics driver for hardware encoding.

The automatic encoder order is:

- macOS: `h264_videotoolbox`;
- Windows/Linux with NVIDIA: `h264_nvenc`;
- Windows/Linux with AMD AMF: `h264_amf`;
- Linux with AMD/Intel VAAPI: `h264_vaapi` through
  `/dev/dri/renderD128`;
- all platforms: `libx264` fallback.

The script performs a one-second encoder preflight. It first requests automatic
hardware decoding where that can feed the required CPU filters, retries with
software decoding if necessary, and finally tries the next encoder. Hardware
encoding remains the primary acceleration path. FFmpeg itself notes that
hardware decoding can become slower when frames must be copied back for normal
filters, so `--no-hw-decode` is available when a driver behaves poorly.

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

Use `--quality balanced` or `--quality compact` to reduce the file size. The
default `high` profile targets about 2.4 Mb/s at 24 fps, 2.8 Mb/s at 30 fps,
and 5.6 Mb/s at 60 fps for a full 960x544 frame, with bounded VBR peaks.

## 4K HDR, HDR10, HLG, and Dolby Vision sources

HDR is converted to 8-bit BT.709 SDR before H.264 encoding. The high-quality
path uses `zscale` in linear light, the selected FFmpeg `tonemap` curve, gamut
conversion, error-diffusion dithering, and then hardware encoding.

For HDR input, FFmpeg must contain both the `zscale` and `tonemap` filters.
`zscale` requires an FFmpeg build configured with `--enable-libzimg`:

```sh
ffmpeg -hide_banner -filters | grep -E 'zscale|tonemap'
```

On macOS, Homebrew's keg-only `ffmpeg-full` formula includes `zimg`:

```sh
brew install ffmpeg-full
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

All audio tracks are kept in their original order and retain language/title
metadata while being converted to the Vita-safe AAC format. Text subtitles
stored as `mov_text`, generic text, or WebVTT are converted to SubRip for
Matroska compatibility; ASS/SSA, SubRip, PGS, VobSub, and other compatible
subtitle codecs are copied without re-encoding.

The converted file retains these tracks even if the current VitaMediaDeck UI
does not yet expose selection or rendering for every one of them.

Run the complete option reference with:

```sh
python3 vitamediadeck_transcoder.py --help
```
