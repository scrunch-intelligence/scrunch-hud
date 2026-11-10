"""Local directional evidence. These descriptors are not a musical quality score."""
import audio_analysis  # Reuse the mixing engine's local dependency environment.
import hashlib
import json

import librosa
import numpy as np
from scipy.signal import welch
from reference_dsp import BANDS


POLICY = ('Measurements describe changes, not quality. Do not rank candidates, select a winner, '
          'or use these values as a reward. The producer chooses more / less / keep / something-else.')


def _envelope(y, sr, notes):
    # Measure only non-overlapping MIDI events, combining simultaneous chord notes.
    groups = {}
    for n in notes:
        on, off = int(n['on']), int(n['off'])
        if not 0 <= on < off <= len(y):
            raise ValueError('MIDI note window is outside the rendered audio.')
        groups[on] = max(groups.get(on, off), off)
    starts = sorted(groups)
    attacks, decays = [], []
    hop = max(1, round(sr * .002))
    for i, on in enumerate(starts):
        off = groups[on]
        if any(a < on < b for a, b in groups.items()):
            continue
        end = min(off, starts[i + 1] if i + 1 < len(starts) else off)
        segment = y[on:end]
        count = len(segment) // hop
        if count < 4:
            continue
        env = np.sqrt(np.mean(segment[:count * hop].reshape(count, hop, -1) ** 2, axis=(1, 2)))
        peak = float(env.max())
        if peak < 1e-7:
            continue
        p = int(np.argmax(env))
        lo = np.flatnonzero(env[:p + 1] >= peak * .1)
        hi = np.flatnonzero(env[:p + 1] >= peak * .9)
        if len(lo) and len(hi):
            attacks.append(float((hi[0] - lo[0]) * hop / sr * 1000))
        tail = np.flatnonzero(env[p + 1:] <= peak * .1)
        if len(tail):
            decays.append(float((tail[0] + 1) * hop / sr * 1000))
    return {'attack10to90Ms': float(np.median(attacks)) if attacks else None,
            'decayPeakTo10Ms': float(np.median(decays)) if decays else None,
            'attackEvents': len(attacks), 'decayEvents': len(decays),
            'method': '2 ms channel-energy RMS, MIDI-windowed; overlap excluded; decay censored at next note/note-off',
            'limitation': 'Rendered envelope proxy, not a synth ADSR readback; effects and sustained tails can confound it.'}


def inspect(audio, phrase):
    y = np.asarray(audio, dtype=float)
    if y.ndim == 1:
        y = y[:, None]
    sr = phrase['sample_rate']
    if (not isinstance(sr, int) or sr < 8000 or y.ndim != 2 or y.shape[1] not in (1, 2)
            or not 0 < len(y) <= sr * 120 or not np.isfinite(y).all()):
        raise ValueError('Expected finite mono/stereo audio, up to 120 seconds, at a valid sample rate.')
    fingerprint = hashlib.sha256(json.dumps({k: phrase.get(k) for k in
        ('sample_rate', 'musical_frames', 'notes', 'tempo', 'ppq_origin')}, sort_keys=True).encode()).hexdigest()
    peak = float(np.max(np.abs(y)))
    result = {'version': 1, 'policy': POLICY, 'phraseFingerprint': fingerprint, 'frames': len(y),
              'sampleRate': sr, 'silence': peak < 1e-7, 'samplePeakDbFS': float(20 * np.log10(peak)) if peak else None,
              'clippedSampleFraction': float(np.mean(np.abs(y) >= 1)), 'metrics': {}, 'bands': [],
              'limitations': ['Brightness is a centroid proxy, not taste.',
                             '2-6 kHz energy share is not a perceptual harshness verdict.',
                             'Spectral flatness is not harmonic density; harmonic density is not estimated.']}
    if result['silence']:
        return result
    f, power = welch(y, sr, nperseg=min(8192, len(y)), axis=0)
    power = power.mean(axis=1)  # Never collapse antiphase stereo into a silent mono signal.
    total = float(power[(f >= 20) & (f < min(20000, sr / 2))].sum())
    if total <= 0:
        return result
    for name, lo, hi in BANDS:
        share = float(power[(f >= lo) & (f < hi)].sum() / total) if hi <= sr / 2 else None
        result['bands'].append({'name': name, 'lowHz': lo, 'highHz': hi, 'energyShare': share})
    magnitude = np.sqrt(power)[:, None]
    centroid = librosa.feature.spectral_centroid(S=magnitude, freq=f)[0, 0]
    flatness = librosa.feature.spectral_flatness(S=magnitude, power=2)[0, 0]
    harsh = float(power[(f >= 2000) & (f < 6000)].sum() / total) if sr / 2 >= 6000 else None
    result['metrics'] = {'brightnessCentroidHz': float(centroid), 'spectralFlatness': float(flatness),
                         'presence2to6kShare': harsh}
    result['envelope'] = _envelope(y, sr, phrase.get('notes', []))
    result['metrics'].update({k: result['envelope'][k] for k in ('attack10to90Ms', 'decayPeakTo10Ms')})
    return result


def directional_feedback(before, after, requests=()):
    """requests: explicit metric/direction/tolerance, not automatic interpretation of taste."""
    if (before['phraseFingerprint'], before['frames']) != (after['phraseFingerprint'], after['frames']):
        raise ValueError('Directional feedback requires the same MIDI, timing and render duration.')
    deltas = {}
    for k in before['metrics'].keys() | after['metrics'].keys():
        a, b = before['metrics'].get(k), after['metrics'].get(k)
        deltas[k] = None if a is None or b is None else b - a
    checks = []
    for r in requests:
        key, direction = r['metric'], r['direction']
        tolerance = float(r['tolerance'])
        if key not in deltas or direction not in ('increase', 'decrease') or not np.isfinite(tolerance) or tolerance < 0:
            raise ValueError('Unknown directional request or invalid tolerance.')
        delta = deltas[key]
        status = 'unavailable'
        if delta is not None:
            signed = delta if direction == 'increase' else -delta
            status = 'within-tolerance' if abs(delta) <= tolerance else ('requested-direction' if signed > 0 else 'opposite-direction')
        checks.append({**r, 'delta': delta, 'observation': status})
    return {'policy': POLICY, 'deltas': deltas, 'requestedDirections': checks,
            'qualityDecision': 'producer-only', 'nextChoices': ['more', 'less', 'keep', 'something-else']}
