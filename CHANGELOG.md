# Changelog

## 1.1.0-beta.1

First beta of the terminal AV editor. Published on `beta`, branching from `dev`.

### Interface and preview

- VitaMediaDeck-inspired blue theme with nine numbered sections, visible focus,
  wrapped parameter cards, responsive preset lists and source/output comparisons.
- Always-visible converted AV preview, synchronized audio and subtitle activity,
  filmstrip navigation across 100 positions, and manual playback.
- Original/processed split comparison with keyboard and mouse controls;
  retained divider and source position after parameter changes.
- Every preview edit rebuilds the complete sample from the original source.
  No preview media, comparison frames or automatic processing decisions are
  reused from earlier renders. Superseded workers cannot publish stale results.
- Audio controls show source/output channels, per-track bitrate and arithmetic
  per-channel averages. Included/excluded tracks remain selectable while loading.

### Conversion and presets

- Six Vita HW/SW playback presets, with Max perceptual, Medium and Light levels,
  plus content-specific presets and descriptions of current-to-preset changes.
- Optional denoising, sharpening, debanding, resizing and HDR tone mapping,
  with runtime filter support and Apple GPU paths where available.
- Automatic pipeline measurements include decode, filtering, transfers and
  encoding; High/Maximum automatic resizing retains Lanczos.
- Higher initial quality/resource settings, source-capped AAC targets and
  ordered numeric controls without endpoint wraparound. Enhancement filters
  remain disabled by default.
- Frame-based conversion progress starts at zero for every video, with a
  duration-weighted queue total and separate finalization phases.
- Each converted video has its own folder containing the output, recovery
  metadata and report. TUI/batch logs live alongside their conversion data.
- Output conflicts are checked before processing. Existing results are
  replaced only after the replacement passes validation.
- Exit confirmation and resumable file/batch jobs retain source identities,
  paths, selected streams and per-media settings. Older saved jobs remain readable.
- Both command-line entry points expose the same `--version` value.

### Validation

Host regressions cover fresh renders, cancellation, split pixels on all four
terminal backends, AV timing, aspect ratio, stream selection, storage safety,
batch recovery and progress. They do not certify physical Vita performance,
every terminal's graphics/audio behavior, or a Windows executable build.

## 1.0.0

Initial tagged host transcoder release, with the terminal interface and Windows
executable build workflow. Subsequent development added resumable directory
conversion and per-media batch settings before the AV editor beta.
