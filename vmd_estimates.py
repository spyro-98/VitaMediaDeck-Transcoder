"""Source-aware output budgets and explanations using the conversion's own rules."""
from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace
import re

import vitamediadeck_transcoder as core
import vmd_ui


def bitrate(value):
    return f'{value / 1000:,.0f} kb/s' if value is not None else 'unknown bitrate'


def byte_size(value):
    if value is None:
        return 'unavailable'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def channel_count(stream):
    try:
        count = int(stream.get('channels', 0))
        if count > 0:
            return count
    except (ValueError, TypeError):
        pass
    layout = str(stream.get('channel_layout', ''))
    if layout in ('mono', 'stereo'):
        return 1 if layout == 'mono' else 2
    match = re.match(r'^(\d+)\.(\d+)(?:\(|$)', layout)
    return sum(map(int, match.groups())) if match else None


def output_plan(settings, info, metadata, audio, source_size=None, audio_rates=None, sample=None, encoder='', sample_pending=True, source_audio_rates=None,
                processing=None, processing_report=None, capabilities=None):
    """A budget is not a prediction that a quality-controlled encoder hits ABR."""
    fps = core.target_fps(getattr(info, 'fps', Fraction(30)), Fraction(str(settings.max_fps)))
    target, maximum, _ = core.bitrate_plan(960, 544, fps, settings.quality, settings.content_tune)
    duration = getattr(info, 'duration', None)
    duration = duration if duration and duration > 0 else None
    streams = metadata.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), {})
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    tracks = []
    for at, ordinal in enumerate(audio):
        stream = audios[ordinal] if ordinal < len(audios) else {}
        source_rate = core.stream_bit_rate(stream)
        known_rates = getattr(info, 'audio_bit_rates', ())
        if source_rate is None and ordinal < len(known_rates):
            source_rate = known_rates[ordinal]
        source_rate = source_rate or (source_audio_rates or {}).get(ordinal)
        measured = audio_rates is not None and at < len(audio_rates)
        cap = audio_rates[at] if measured else core.audio_bitrate_target(settings.audio_bitrate, source_rate)
        tracks.append(dict(ordinal=ordinal, source_bps=source_rate, channels=channel_count(stream), codec=stream.get('codec_name', 'unknown').upper(),
            layout=stream.get('channel_layout') or '', target_kbps=cap,
            resolved=measured or source_rate is not None,
            estimated_bytes=cap * 1000 * duration / 8 if duration else None))
    audio_kbps = sum(track['target_kbps'] for track in tracks)
    all_audio = []
    for ordinal in range(getattr(info, 'audio_stream_count', len(audios))):
        stream = audios[ordinal] if ordinal < len(audios) else {}
        known = getattr(info, 'audio_bit_rates', ())
        source_rate = core.stream_bit_rate(stream) or (known[ordinal] if ordinal < len(known) else None)
        source_rate = source_rate or (source_audio_rates or {}).get(ordinal)
        all_audio.append(dict(ordinal=ordinal, source_bps=source_rate, channels=channel_count(stream),
                              codec=str(stream.get('codec_name') or 'unknown').upper(), layout=stream.get('channel_layout') or ''))
    total_bps = target + audio_kbps * 1000
    engine = encoder or (settings.encoder if settings.encoder != 'auto' else '')
    source_bps = core.stream_bit_rate(video)
    try:
        picture = core.content_dimensions(info)
    except (AttributeError, TypeError, ZeroDivisionError):
        width, height = getattr(info, 'width', 0), getattr(info, 'height', 0)
        picture = core.content_dimensions(SimpleNamespace(display_width=width, display_height=height)) if width and height else None
    return dict(fps=fps, target_bps=target, max_bps=maximum, audio_kbps=audio_kbps, tracks=tracks,
        total_bps=total_bps, estimated_bytes=total_bps * duration / 8 if duration else None,
        video_bytes=target * duration / 8 if duration else None,
        duration=duration, picture=picture, source_video_bps=source_bps,
        source_codec=video.get('codec_name') or getattr(info, 'video_codec', ''),
        source_size=source_size, hdr_active=bool(settings.force_hdr or getattr(info, 'hdr', False)),
        engine=engine, engine_confirmed=bool(encoder), sample=sample, sample_pending=sample_pending,
        audio_count=getattr(info, 'audio_stream_count', len(audios)), all_audio=all_audio,
        processing=processing or {}, processing_report=processing_report or {}, capabilities=capabilities)


def budget_lines(plan):
    if plan['estimated_bytes'] is None:
        return ['File-size estimate unavailable: source duration is unknown.']
    lines = [f"File budget estimate: {byte_size(plan['estimated_bytes'])} incl. selected audio."]
    original = plan['source_size']
    if original:
        delta = (plan['estimated_bytes'] / original - 1) * 100
        lines.append(f"Estimated {'increase' if delta > 0 else 'reduction'}: {abs(delta):.1f}% vs original file.")
    lines.append('Budget excludes artwork/subtitle/container overhead; final variable bitrate depends on the scenes.')
    return lines


def sample_lines(plan):
    sample = plan.get('sample')
    if not sample:
        return ['Measured video: -- kb/s', 'Sample file estimate: --']
    lines = [f"Measured video: {bitrate(sample.get('video_bps'))} ({sample['duration']:.1f}s sample)"]
    if plan['duration'] and sample.get('file_bytes'):
        projection = sample['file_bytes'] / sample['duration'] * plan['duration']
        lines.append(f'Sample file estimate: {byte_size(projection)} (scene only)')
    else:
        lines.append('Sample file estimate: --')
    return lines


def audio_summary(plan, ordinal=None):
    """Twelve invariant rows: source → output, track totals and channel means."""
    sources = plan.get('all_audio', [])
    if ordinal is None:
        ordinal = plan['tracks'][0]['ordinal'] if plan['tracks'] else 0
    ordinal = max(0, min(ordinal, max(0, len(sources) - 1)))
    source = sources[ordinal] if sources else {}
    track = next((item for item in plan['tracks'] if item['ordinal'] == ordinal), None)
    bps, channels = source.get('source_bps'), source.get('channels')
    cap = track['target_kbps'] if track else None
    source_rate = f'{bps / 1000:.0f}' if bps else '--'
    target_rate = str(cap) if cap is not None else '--'
    mean_in = f'{bps / 1000 / channels:.1f}' if bps and channels else '--'
    mean_out = f'{cap / 2:.1f}' if cap is not None else '--'
    layout = source.get('layout', '').split('(')[0]
    source_channels = str(channels) if channels else '--'
    if layout:
        source_channels += f' ({layout})'
    if len(source_channels) > 9:
        source_channels = f'{channels} ch'
    delta = f' ({100 * (cap * 1000 / bps - 1):+.0f}%)' if cap is not None and bps else ''
    measured = (plan.get('sample') or {}).get('audio_bps', [])
    at = next((i for i, item in enumerate(plan['tracks']) if item['ordinal'] == ordinal), None)
    measured_value = f'{measured[at] / 1000:.0f}' if at is not None and at < len(measured) else '--'
    outcome = ('Lossy + surround removed' if channels and channels > 2 else 'Lossy re-encode') if track else 'Excluded; source unchanged'
    def pair(label, before, after):
        return f'{label:<10}{before:>9} → {after:>10}'
    return [
        pair(f'A{ordinal + 1}' if sources else 'No audio', 'Source', 'Output'),
        pair('Codec', source.get('codec', '--')[:9], 'AAC-LC' if track else '--'),
        pair('Channels', source_channels, '2 stereo' if track else '--'),
        pair('Track kb/s', source_rate, target_rate + delta),
        pair('kb/s/ch*', mean_in, mean_out),
        f"Max A{ordinal + 1}: {core.audio_bitrate_limit(bps)} kb/s ({'source' if bps and bps // 1000 < core.AAC_OUTPUT_MAX_KBPS else 'AAC'})",
        f'Loss: {outcome}',
        'Audible quality: unmeasured',
        f"Included {len(plan['tracks'])}/{plan['audio_count']}; total {plan['audio_kbps']} kb/s",
        f'Measured A{ordinal + 1}: {measured_value} kb/s',
        f"Size A{ordinal + 1} (est.): {byte_size(track['estimated_bytes']) if track else '--'}",
        '* Mean only; bits are shared.',
    ]


def parameter_effects(settings, info, plan, audio_cursor=None):
    """Compact field summaries; each field has a constant number of rows."""
    target = f"{plan['target_bps'] / 1000:.0f}"
    maximum = f"{plan['max_bps'] / 1000:.0f}"
    source_rate = f"{plan['source_video_bps'] / 1000:.0f}" if plan['source_video_bps'] else '--'
    sample = plan.get('sample') or {}
    measured = f"{sample['video_bps'] / 1000:.0f}" if sample.get('video_bps') else '--'
    engine = plan['engine'] or 'auto (pending)'
    rate_control = (f"x264 CRF: {core.quality_crf(settings.quality, settings.content_tune)}" if engine == 'x264' else
                    f"NVENC CQ: {core.quality_crf(settings.quality, settings.content_tune)}" if engine == 'nvenc' else
                    'Rate control: hardware bitrate' if engine in ('videotoolbox', 'amf', 'vaapi') else 'Rate control: awaiting encoder')
    tune_control = f'x264 tune: {core.X264_CONTENT_TUNES[settings.content_tune]}' if engine == 'x264' else 'Content changes bitrate budget'
    resolved = plan.get('processing', {})
    backend = resolved.get('filter_backend', getattr(settings, 'filter_backend', 'auto'))
    apple = backend == 'apple-gpu'
    caps = plan.get('capabilities')
    ci_status = ('Core Image: checking support' if caps is None else
                 'Core Image: compiled; see H' if 'coreimage' in caps.filters else 'Unavailable: Core Image missing')
    scale_status = ('Apple resize: checking support' if caps is None else
                    'Apple resize: compiled; see H' if 'scale_vt' in caps.filters else 'Unavailable: Apple resize missing')
    gpu_effects = []
    if apple and settings.denoise in ('light', 'medium', 'strong'):
        gpu_effects.append('noise')
    if apple and settings.sharpen != 'off':
        gpu_effects.append('sharpness')
    effect_place = ('GPU: ' + ' + '.join(gpu_effects) if gpu_effects else
                    'CPU / off for current effects' if backend != 'auto' else 'Pending timed selection')
    picture = plan['picture']
    dims = f'{picture[0]}×{picture[1]}' if picture else '--'
    source_dims = f"{getattr(info, 'width', '?')}×{getattr(info, 'height', '?')}"
    source_fps = float(getattr(info, 'fps', 0) or 0)
    fps = float(plan['fps'])
    motion = 'Unchanged' if abs(source_fps - fps) < .0001 else f'{100 * (1 - fps / source_fps):.0f}% fewer frames' if source_fps > 0 else 'Source FPS unknown'
    reduced = picture and getattr(info, 'width', 0) * getattr(info, 'height', 0) > picture[0] * picture[1]
    quality = [
        f'Video kb/s: {source_rate} → {target} target',
        f'Video limit: {maximum} kb/s',
        f'Measured video: {measured} kb/s',
        f"File size (est.): {byte_size(plan['estimated_bytes'])}",
        {'maximum': 'More bits; no detail recovery', 'high': 'Fine detail; larger files',
         'balanced': 'Balance size and fine detail', 'compact': 'Smaller files; more detail loss'}[settings.quality],
        f'Encoder: {engine or "pending"}',
        'Lossy re-encode; no quality score.',
        rate_control,
    ]
    hdr = [
        f"Tone map: {settings.tone_map if plan['hdr_active'] else 'inactive (SDR source)'}",
        'Output: BT.709 SDR, 8-bit',
        {'mobius': 'Neutral colour / midtones', 'hable': 'Film-like highlight roll-off', 'reinhard': 'Brighter; flatter contrast'}[settings.tone_map],
        'Processing: CPU HDR → SDR' if plan['hdr_active'] else 'SDR source: no visible change',
    ]
    threads = core.resource_thread_limit(settings.system_load)
    cover = 'None' if settings.cover_mode == 'none' else '480×272 JPEG'
    return dict(
        encoder=[f"Codec: {str(plan['source_codec']).upper() or '--'} → H.264 High",
                 {'auto': 'Auto: timed CPU / GPU pipelines', 'videotoolbox': 'VideoToolbox: Apple hardware',
                  'nvenc': 'NVENC: NVIDIA hardware', 'amf': 'AMF: AMD hardware', 'vaapi': 'VAAPI: Linux GPU hardware',
                  'x264': 'x264: CPU / software'}[settings.encoder],
                 'Use: detail per bit; slower' if settings.encoder == 'x264' else 'Use: fastest validated mixture' if settings.encoder == 'auto' else 'Use: fast compatible hardware',
                 f'Picture: {source_dims} → {dims}', 'Canvas: 960×544; proportions kept',
                 f"Change: {'less spatial detail' if reduced else 'lossy re-encode'}", f'Engine: {engine}'],
        quality=quality,
        content_tune=[{'movie': 'Use: live action; natural texture', 'film-grain': 'Use: film scans; keep grain',
                      'anime': 'Use: clean animation / line art', 'anime-grain': 'Use: grainy analogue animation',
                      'vita-sw': 'Use: Vita SW playback; x264, ≤30 fps. No B-frames; one reference.'}[settings.content_tune],
                      tune_control, f'Video target: {target} kb/s'],
        max_fps=[f'Cap: {vmd_ui.fps_label(settings.max_fps)}', f'FPS: {source_fps:.3f} → {fps:.3f}',
                 'Source ≤ cap: FPS kept' if motion == 'Unchanged' else f'Motion: {motion}', f'Video target: {target} kb/s'],
        audio_bitrate=audio_summary(plan, audio_cursor),
        tone_map=hdr, force_hdr=list(hdr),
        system_load=[f"FFmpeg threads: {threads if threads else 'unlimited'}", 'Effect: processing load / speed', 'Quality / size: unchanged target'],
        hw_decode=[f"Decode: {'CPU (HDR)' if plan['hdr_active'] else 'hardware / CPU fallback' if settings.hw_decode else 'CPU'}",
                   'Quality / size: unchanged target'],
        cover_mode=[f'Cover: {cover}', 'Embedded in output file'],
        cover_image=[f"Image: {'custom cover' if settings.cover_mode == 'custom' else 'inactive'}", f'Cover output: {cover}'],
        overwrite=[f"Existing output: {'replace allowed' if settings.overwrite else 'replacement refused'}", 'Quality / size: unaffected'],
        vaapi_device=[f"Device: {'used by VAAPI' if plan['engine'] == 'vaapi' else 'inactive unless VAAPI'}", 'Quality / size: unchanged target'],
        ffmpeg=['Encoding / filters executable', 'Full path: Enter to edit'],
        ffprobe=['Media analysis executable', 'Full path: Enter to edit'],
        encode_speed=[f"Compression effort: {getattr(settings, 'encode_speed', 'slow')}",
                      {'fast': 'Fast: quicker, less efficient', 'medium': 'Medium: balanced CPU time',
                       'slow': 'Slow: better bits / CPU time', 'veryslow': 'Very slow: small extra gains'}[getattr(settings, 'encode_speed', 'slow')],
                      'Active for the x264 encoder' if plan['engine'] == 'x264' else
                      'Auto: applies if x264 is chosen' if settings.encoder == 'auto' else 'Inactive: hardware has own speed'],
        denoise=[{'off': 'Off: keeps noise and film grain', 'light': 'Light: clean mild source noise',
                  'medium': 'Medium: noisy / low-bitrate video', 'strong': 'Strong: heavy noise; softer image',
                  'nlmeans': 'NLMeans: detailed spatial cleanup'}[getattr(settings, 'denoise', 'off')],
                 'NLMeans: slow CPU algorithm' if getattr(settings, 'denoise', 'off') == 'nlmeans' else
                 'Apple GPU: spatial noise filter' if apple else 'CPU filter; encoder independent',
                 'Grain film: prefer off / light'],
        sharpen=[{'off': 'Off: keeps original edge contrast', 'light': 'Light: subtle edge clarity',
                  'medium': 'Medium: stronger edge contrast'}[getattr(settings, 'sharpen', 'off')],
                 'Apple GPU: sharpen luminance' if apple else 'CPU unsharp; use on soft sources', 'Too strong: halos / more noise'],
        deband=[{'off': 'Off: keeps source gradients', 'light': 'Light: smooth faint colour bands',
                 'medium': 'Medium: stronger band smoothing'}[getattr(settings, 'deband', 'off')],
                'CPU: skies / animation gradients', 'Too strong: removes fine texture'],
        scaler=[{'auto': 'Auto: High/Max keep Lanczos detail', 'apple': 'Apple GPU: may soften fine detail', 'lanczos': 'Lanczos: crisp downsizing', 'bicubic': 'Bicubic: softer / fewer halos',
                 'spline': 'Spline: smooth, detailed resize'}[getattr(settings, 'scaler', 'lanczos')],
                'Running: ' + resolved.get('scaler', 'pending Auto selection' if settings.scaler == 'auto' else settings.scaler),
                scale_status if settings.scaler in ('auto', 'apple') else 'CPU scaler; GPU encode allowed'],
        filter_backend=[{'auto': 'Auto: measure CPU / GPU mixtures', 'apple-gpu': 'Apple GPU: Core Image filters',
                         'cpu': 'CPU: standard FFmpeg filters'}[settings.filter_backend],
                        effect_place,
                        ci_status if settings.filter_backend != 'cpu' else 'GPU encode may run in parallel'],
    )
