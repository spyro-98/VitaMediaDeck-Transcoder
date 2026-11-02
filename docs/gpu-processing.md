# GPU processing research and current integration

Research checked against the FFmpeg documentation and the installed FFmpeg
9.0.2 on 31 October 2026. Names compiled into FFmpeg are capability evidence,
not proof of a usable driver/device. H in the app displays the currently
selected FFmpeg's inventory and actual calibration results.

## Useful filter families

| Purpose | CPU filters | Hardware alternatives | Current application controls |
| --- | --- | --- | --- |
| Noise / grain | hqdn3d, nlmeans, atadenoise, bm3d, fftdnoiz, dctdnoiz, vaguedenoiser, removegrain | Core Image CINoiseReduction; nlmeans_opencl; nlmeans_vulkan; bilateral_cuda | hqdn3d, CPU NLMeans, Apple Core Image noise reduction |
| Edge clarity | unsharp, cas, convolution | Core Image CISharpenLuminance; unsharp_opencl | unsharp / Apple sharpen luminance |
| Colour banding | deband, gradfun | libplacebo on Vulkan | CPU deband |
| Resizing | scale, zscale | scale_vt, scale_cuda, scale_npp, scale_vaapi, vpp_qsv, scale_vulkan, libplacebo | CPU Lanczos/Bicubic/Spline; Apple hardware scaling |
| HDR → SDR | zscale + tonemap | tonemap_opencl, tonemap_vaapi, libplacebo | CPU Mobius/Hable/Reinhard, including after Apple resizing |
| Deinterlacing | bwdif, yadif | yadif_videotoolbox (Metal), bwdif/yadif_cuda, bwdif_vulkan, deinterlace_vaapi, vpp_qsv | CPU bwdif / Apple Metal YADIF, one output frame per source frame |

The catalogue lists research alternatives separately from implemented controls.
It does not silently substitute an unsupported shader or install a driver.
Non-enhancement filters such as cuts, overlays, stabilization and generated
frames are not automatic transcoding operations. `/` searches all compiled
filters if a particular operation needs inspection.

## What is usable on the checked Mac

- **Core Image:** compiled and executed successfully with noise reduction and
  luminance sharpening. Their algorithms differ from temporal hqdn3d/unsharp;
  inspect grain, motion and halos rather than treating GPU as identical pixels.
- **VideoToolbox scaling:** executed with both CPU-decoded uploaded frames and
  native VideoToolbox frames. Hardware output uses `videotoolbox_vld`.
- **Metal deinterlacing:** the YADIF VideoToolbox path executed in the mixed
  pipeline and preserves frame cadence; deinterlacing does not double FPS.
- **Vulkan/libplacebo/nlmeans_vulkan:** filters compiled, but device creation
  failed because the Vulkan loader was unavailable. They are research options,
  not enabled application paths. Libplacebo can resize, deband and tone-map on
  a working Vulkan stack; that is not a reason to replace a verified HDR path
  before checking runtime support and output colour.
- **OpenCL/CUDA/VAAPI/QSV:** availability depends on the chosen build, OS and
  driver. An NVENC/AMF encoder does not by itself supply OpenCL or Vulkan
  filtering. The installed Mac build does not provide those app paths.

## Compatible mixtures

| Decode | Filters | Encode | Status |
| --- | --- | --- | --- |
| CPU | CPU | x264 | Supported |
| CPU | CPU | VideoToolbox/NVENC/AMF/VAAPI | Supported when that encoder is valid |
| CPU | Apple resize + Core Image | x264 | Executed on the checked Mac |
| VideoToolbox | Apple resize / Metal deinterlace + Core Image | VideoToolbox | Executed on the checked Mac |
| CPU | Apple resize → CPU HDR tone map → Core Image | x264 | HDR fixture converted and validated on the checked Mac |
| CPU/compatible hardware | Vulkan/OpenCL/CUDA/VAAPI filters | CPU or compatible hardware encoder | Technically possible with transfers/interop; not enabled without runtime and output validation |

Core Image and x264 do not need to run on the same processor. Frames cross
CPU/GPU memory as required. Different physical GPU families may require extra
copies; zero-copy interoperability must be proved for the actual devices.

## Automatic policy and proof boundaries

Auto compares real complete FFmpeg video pipelines on a short source sample.
It includes decoder, filter, upload/download, synchronization and encoder time.
A fixed encoder is respected; Auto filters/resizing still optimize its path.
Near ties prefer a hardware encoder, and filter substitutions require a 15%
timing benefit. This is a measured heuristic, not a guarantee for a whole movie
or a statement that different processing algorithms preserve identical detail.

For High/Maximum, automatic resizing stays on Lanczos. A faster Apple resize
must not silently soften texture while increasing the quality level: the
installed scale_vt exposes dimensions and colour tags, but no Lanczos kernel.
This restriction does not disable hardware encoding/decoding or GPU effects.
Balanced/Compact can still benchmark Apple resizing, and an explicit Apple
choice remains valid. Every request recalibrates from the current source and
settings; previous timing decisions are never loaded or saved.

The application keeps operations in one FFmpeg pipeline. It relies on FFmpeg's
threading and hardware queues to overlap independent work across frames;
dependent operations on the same frame remain ordered. There is no extra
process per filter and no intermediate frame sequence. Measuring actual
GPU/CPU queue overlap would require a separate profiler trace.

Host tests validate converted frames, timing/stream contracts, source integrity,
fresh preview/export calibration and CPU/GPU combinations. They do not prove Vita
playback acceptance or a user's terminal graphics appearance.

## Primary references

- [FFmpeg Core Image](https://ffmpeg.org/ffmpeg-filters.html#coreimage): GPU filter chains and CPU/host transfers.
- [FFmpeg VideoToolbox filters](https://ffmpeg.org/ffmpeg-filters.html#VideoToolbox-Video-Filters): scaling and hardware frame contexts.
- [FFmpeg Vulkan filters](https://ffmpeg.org/ffmpeg-filters.html#Vulkan-Video-Filters): Vulkan device-dependent operations.
- [FFmpeg libplacebo](https://ffmpeg.org/ffmpeg-filters.html#libplacebo): scaling, tone mapping, debanding and transfer-cost caveats.
- [FFmpeg OpenCL filters](https://ffmpeg.org/ffmpeg-filters.html#OpenCL-Video-Filters): device initialization and hwupload/hwdownload.
- [FFmpeg CUDA filters](https://ffmpeg.org/ffmpeg-filters.html#CUDA-Video-Filters): CUDA prerequisites and operations.
- [FFmpeg Core Image implementation](https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/vf_coreimage.m): output-error reporting is checked even if a process returns success.
