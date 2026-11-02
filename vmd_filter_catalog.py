"""Documented filter families and live FFmpeg availability, separate from encoding."""
FAMILIES = (
    ('Noise / grain', ('hqdn3d', 'nlmeans', 'atadenoise', 'bm3d', 'fftdnoiz', 'dctdnoiz', 'vaguedenoiser', 'removegrain',
                       'nlmeans_opencl', 'nlmeans_vulkan', 'bilateral_cuda', 'coreimage')),
    ('Edge clarity', ('unsharp', 'cas', 'convolution', 'unsharp_opencl', 'coreimage')),
    ('Colour bands', ('deband', 'gradfun', 'libplacebo')),
    ('Resize', ('scale', 'zscale', 'scale_vt', 'scale_cuda', 'scale_npp', 'scale_vaapi', 'vpp_qsv', 'scale_vulkan', 'libplacebo')),
    ('HDR to SDR', ('zscale', 'tonemap', 'tonemap_opencl', 'tonemap_vaapi', 'libplacebo')),
    ('Interlaced sources', ('bwdif', 'yadif', 'yadif_videotoolbox', 'bwdif_cuda', 'yadif_cuda', 'bwdif_vulkan', 'deinterlace_vaapi', 'vpp_qsv')),
)
WIRED = {'hqdn3d', 'nlmeans', 'unsharp', 'deband', 'scale', 'zscale', 'tonemap', 'bwdif', 'coreimage', 'scale_vt', 'yadif_videotoolbox'}


def location(name):
    if name == 'coreimage':
        return 'Apple GPU / Core Image'
    if name in ('libplacebo',) or name.endswith('_vulkan'):
        return 'Vulkan GPU; needs runtime/driver'
    if name.endswith('_opencl'):
        return 'OpenCL GPU; needs device context'
    if name.endswith(('_cuda', '_npp')):
        return 'NVIDIA GPU / CUDA'
    if name.endswith('_vaapi'):
        return 'VAAPI hardware / Linux'
    if name.endswith('_qsv'):
        return 'Intel GPU / QSV'
    if name.endswith(('_vt', '_videotoolbox')):
        return 'Apple hardware / VideoToolbox'
    return 'CPU'


def lines(plan, settings, query=''):
    caps = plan.get('capabilities')
    available = caps.filters if caps else set()
    engine = plan.get('engine') or settings.encoder
    rows = [('Filter support', 'heading'), (f'Encoder: {engine}', 'value'),
            ('Filters and encoding are independent.', 'value'),
            ('CPU ↔ GPU transfers are timed in Auto.', 'muted'),
            ('Compiled support is not a runtime test.', 'muted'),
            ('H Back · / Search · PgUp/PgDn', 'change')]
    report = plan.get('processing_report') or {}
    if report:
        rows += [('Measured pipeline', 'heading'), (report.get('policy', ''), 'value')]
        for trial in report.get('trials', []):
            opts = trial['options']
            label = f"{trial['encoder']} / {opts['filter_backend']} / {opts['scaler']}"
            rows.append((label, 'value'))
            rows.append((f"{trial['seconds']:.3f}s for {trial['frames']} frames" if trial.get('seconds') is not None else
                         'Unavailable in runtime test', 'muted' if trial.get('seconds') is not None else 'change'))
            for alternative in trial.get('filter_trials', []):
                option = alternative['options']
                rows.append((f"  {option['filter_backend']} / resize {option['scaler']}", 'value'))
                rows.append((f"  {alternative['seconds']:.3f}s incl. transfers" if alternative.get('seconds') is not None else
                             '  Runtime test failed', 'muted' if alternative.get('seconds') is not None else 'change'))
    known = set()
    for title, names in FAMILIES:
        selected = [name for name in names if not query or query.lower() in (name + ' ' + title + ' ' + location(name)).lower()]
        if not selected:
            continue
        rows.append((title, 'heading'))
        for name in selected:
            known.add(name)
            present = name in available
            rows.append((f"{'✓' if present else '—'} {name} · {location(name)}", 'value' if present else 'muted'))
            rows.append(('App control available' if name in WIRED and present else 'Missing in selected FFmpeg' if caps and not present else
                         'Checking selected FFmpeg' if not caps else 'FFmpeg only; no app control yet', 'muted'))
    others = [name for name in sorted(available - known) if query and query.lower() in (name + ' ' + location(name)).lower()]
    if others:
        rows.append(('Other matching compiled filters', 'heading'))
        rows += [(name + ' · ' + location(name), 'value') for name in others]
    if not query:
        rows += [('Other filters', 'heading'), (f'{len(available)} compiled filters; / searches all.', 'muted'),
                 ('Core Image: spatial cleanup differs from hqdn3d temporal cleanup.', 'muted'),
                 ('Vulkan/OpenCL/CUDA need a working driver, not only an encoder.', 'muted')]
    return rows
