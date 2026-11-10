"""Which controls in a synth's safe search scope this sound actually uses, measured on the pinned phrase (27 Sep 2026).

The Host's scope lists every timbre control its synth has (for Diva, every oscillator/filter model's and the loaded
effects'). A control owned by an inactive module, or muted by the patch, changes nothing; searching it wastes the
budget. Each control is moved once, well away from its value, and the phrase is rendered offline in an isolated
instance of the same Host code; the change is compared with the difference between two renders of the untouched sound.
A control is active when its change clearly exceeds that repeat difference. Nothing is written to Live.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

FFT, HOP, BANDS = 2048, 1024, 40
MIN_DB = 0.5        # below this mean band difference nobody hears a change, whatever the repeat noise
NOISE_TIMES = 3.0   # above three times the repeat difference


def _bands(audio, rate):
    x = np.asarray(audio, dtype=np.float64)
    x = x.mean(axis=1) if x.ndim == 2 else x
    frames = 1 + max(0, (len(x) - FFT) // HOP)
    idx = np.arange(FFT)[None, :] + HOP * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(FFT), axis=1)) ** 2
    freqs = np.fft.rfftfreq(FFT, 1 / rate)
    edges = np.geomspace(40, min(16000, rate / 2), BANDS + 1)
    out = np.stack([spec[:, (freqs >= lo) & (freqs < hi)].sum(axis=1) for lo, hi in zip(edges[:-1], edges[1:])], axis=1)
    return 10 * np.log10(out + 1e-12)


def distance(a, b, rate):
    """Mean absolute band-level difference (dB) over the frames where either sound is within 60 dB of its peak."""
    A, B = _bands(a, rate), _bands(b, rate)
    loud = np.maximum(A.max(axis=1), B.max(axis=1))
    keep = loud > loud.max() - 60
    return float(np.abs(A[keep] - B[keep]).mean())


def probe_value(control):
    """A value well away from the original: the farthest option for a stepped control, else 0.35 of the range away."""
    o = control['original']
    if control.get('options'):
        return float(max(control['options'], key=lambda v: abs(v - o)))
    return float(o - 0.35 if o >= 0.5 else o + 0.35)


def measure(driver, workers=6):
    """driver: an offline driver over the sound's own state (adaptive_render.OfflineDriver). Returns rows per control."""
    controls = driver.scope['controls']; original = {c['id']: c['original'] for c in controls}
    rate = driver.phrase['sample_rate']
    a0, _, _ = driver.render(dict(original), 'activity-original', 0)
    a1, _, _ = driver.render(dict(original), 'activity-original', 1)
    floor = distance(a0, a1, rate)

    def one(i):
        c = controls[i]; v = probe_value(c)
        audio, _, rec = driver.render(dict(original, **{c['id']: v}), 'activity', i)
        return i, v, distance(a0, audio, rate), rec['values'].get(c['id'])

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, v, d, back in pool.map(one, range(len(controls))):
            c = controls[i]
            rows.append({'id': c['id'], 'name': c['name'], 'family': c['family'], 'original': c['original'], 'probe': v,
                         'readback': back, 'differenceDb': round(d, 3),
                         'active': bool(d > max(MIN_DB, NOISE_TIMES * floor))})
    return {'repeatDifferenceDb': round(floor, 3), 'threshold': round(max(MIN_DB, NOISE_TIMES * floor), 3), 'controls': rows}


def restrict(scope, activity):
    """The scope with only the controls measured active (same identities and originals)."""
    keep = {r['id'] for r in activity['controls'] if r['active']}
    return dict(scope, controls=[c for c in scope['controls'] if c['id'] in keep])


if __name__ == '__main__':
    import sys
    from adaptive_render import OfflineDriver
    state, phrase_json, out = sys.argv[1:4]
    phrase = json.loads(Path(phrase_json).read_text()); phrase['tail_frames'] = 0; phrase['frames'] = phrase['musical_frames']
    d = OfflineDriver(Path(out), state, phrase, scope=None, case='activity')
    try:
        result = measure(d)
    finally: d.cleanup()
    Path(out, 'activity.json').write_text(json.dumps(result, indent=1))
    act = [r for r in result['controls'] if r['active']]
    print('scope', len(result['controls']), 'active', len(act), 'repeat dB', result['repeatDifferenceDb'])
    for r in sorted(result['controls'], key=lambda r: -r['differenceDb']):
        print('%-4s %-18s %-12s %6.2f %s' % (r['id'], r['name'], r['family'], r['differenceDb'], 'ACTIVE' if r['active'] else ''))
