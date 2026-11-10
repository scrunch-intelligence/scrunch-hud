"""Research-only raw report adapter; never consumes floored/rounded band differences."""
import math

BANDS = (
    ('sub', 20, 60), ('bass', 60, 200), ('low mids', 200, 500),
    ('mids', 500, 2000), ('upper mids', 2000, 3000), ('presence', 3000, 4000),
    ('presence / harshness region', 4000, 6000), ('brightness', 6000, 8000),
    ('brilliance', 8000, 12000), ('air', 12000, 16000), ('upper air', 16000, 20000),
)


def finite_number(value):
    if isinstance(value, (bool, str)) or value is None:
        raise ValueError('Finite numeric measurement required')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Finite numeric measurement required')
    return value


def descriptors(raw):
    """Convert one raw DSP side to target-independent band dB minus integrated LUFS.

    The caller must supply the raw report, not the display's allBands/delta rows.
    Schema validation cannot establish whether a producer rounded its numbers;
    source/DSP identity and provenance must be verified separately.
    """
    loudness = finite_number(raw['integratedLUFS'])
    rows = raw['bands']
    if not isinstance(rows, (list, tuple)) or len(rows) != len(BANDS):
        raise ValueError('Exactly eleven named bands required')
    by_name = {}
    for row in rows:
        name = row['name']
        if name in by_name:
            raise ValueError('Duplicate band name')
        by_name[name] = row
    if set(by_name) != {b[0] for b in BANDS}:
        raise ValueError('Unexpected or missing band name')
    values = []
    for name, low, high in BANDS:
        band = by_name[name]
        if finite_number(band['lowHz']) != low or finite_number(band['highHz']) != high:
            raise ValueError('Band edges differ from tested DSP')
        values.append(finite_number(band['energyDbFS']) - loudness)
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Descriptor overflow')
    return values


def report_descriptors(report, *, expected_version, expected_duration_seconds):
    if report['version'] != expected_version:
        raise ValueError('DSP version mismatch')
    if finite_number(report['sampleRate']) != 48000:
        raise ValueError('Only tested 48 kHz DSP is supported')
    duration = finite_number(report['durationSeconds'])
    expected = finite_number(expected_duration_seconds)
    if expected <= 0 or abs(duration - expected) > 1 / 48000:
        raise ValueError('Unexpected capture duration')
    # Product raw contains loudness only; spectral bands live in matched. Undo
    # its recorded scalar gain before applying raw LUFS normalization.
    result = {}
    for side in ('live', 'reference'):
        gain = finite_number(report['gainDb'][side])
        bands = [dict(b, energyDbFS=finite_number(b['energyDbFS']) - gain)
                 for b in report['matched'][side]['bands']]
        result[side] = descriptors({'integratedLUFS': report['raw'][side]['integratedLUFS'],
                                    'bands': bands})
    return result
