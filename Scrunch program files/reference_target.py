"""Reference as target (build spec 4.7 / step 6, amendment 7).

The producer picks a reference and the passage where the part he means is exposed ("the lead at 1:12,
6 seconds"). Scrunch captures the synth being worked on while Live plays, level-matches it to that
passage with the same pipeline as the Reference page (reference_dsp.compare: EBU R128, identical
bands), and gives the engineer the measured band differences as the target. After a change it
measures again, so "closer to the reference" is a measurement, not the model's opinion.

What is measured and what is inferred stay apart: a mastered reference carries its arrangement,
processing and masking, so the differences are a direction for one synth, not an exact target, and
the comparison is only called comparable when the producer chose the passage for that purpose.
"""
import json
import re
import time
import uuid
from pathlib import Path

import numpy as np

import audio_analysis  # noqa: F401  (the project's analysis dependencies: soundfile, scipy)

CAVEAT = ('Measured on the passage he chose, level-matched. A finished record includes its arrangement, mix '
          'and mastering: use these differences as a direction for this one synth, not an exact target.')


def set_target(state, body):
    """Store the producer's chosen reference passage. body: {path | referenceId, cueSeconds, durationSeconds, part}."""
    import soundfile as sf
    path = body.get('path')
    name = body.get('name')
    if not path and body.get('referenceId'):
        import reference_ab
        _, _, root = reference_ab.project()
        row = next((r for r in reference_ab.manifest(root)['references'] if r['id'] == body['referenceId']), None)
        if row is None:
            raise ValueError('That reference is not loaded in this project.')
        path, name = row['path'], row['name']
    p = Path(str(path or ''))
    if not p.is_file():
        raise ValueError('Choose a reference audio file first.')
    info = sf.info(str(p))
    cue, duration = float(body.get('cueSeconds', 0)), float(body.get('durationSeconds', 6))
    if not 3 <= duration <= 30 or cue < 0 or cue + duration > info.duration:
        raise ValueError('Choose a passage of 3 to 30 seconds inside the reference.')
    state['referenceTarget'] = {'id': uuid.uuid4().hex, 'path': str(p), 'name': str(name or p.stem)[:200],
                                'cueSeconds': cue, 'durationSeconds': duration,
                                'part': str(body.get('part') or 'the part he means')[:80],
                                'comparable': True,       # he chose this passage as the target for this part
                                'setAt': time.time()}
    return state['referenceTarget']


def _read_cue(target):
    import soundfile as sf
    info = sf.info(target['path'])
    first, count = round(target['cueSeconds'] * info.samplerate), round(target['durationSeconds'] * info.samplerate)
    y, sr = sf.read(target['path'], start=first, frames=count, always_2d=True, dtype='float64')
    return y, sr


def measure(c, target, evidence_dir=None):
    """Level-matched comparison of the synth as it sounds now against the reference passage.

    Returns {'id', 'bands': [{band, differenceDb}], 'distanceDb', ...}; raises ValueError when Live is
    stopped or nothing can be captured (nothing is guessed in its place)."""
    y, sr = c.capture_raw(target['durationSeconds'])
    return compare_audio(y, sr, target, evidence_dir)


def compare_audio(y, sr, target, evidence_dir=None):
    """measure() on audio already captured (y: frames x channels, sr)."""
    import reference_dsp
    y = np.asarray(y, dtype=float)
    need = round(target['durationSeconds'] * sr)
    if len(y) < need:
        raise ValueError('Captured %.1f s of the synth; the reference passage is %.1f s.' % (len(y) / sr, target['durationSeconds']))
    ref, rsr = _read_cue(target)
    # compare() needs equal durations: both sides cut to the passage length.
    report, _, _ = reference_dsp.compare(y[:need], sr, ref, rsr)
    bands = [{'band': b['band'], 'differenceDb': round(b['energyDb'], 2)} for b in report['delta']['bands']
             if b.get('comparisonReliable') and b.get('energyDb') is not None]
    # Every band, unrounded, each side clamped at the -80 dBFS reporting floor: one comparison that a
    # "matched" claim can rest on (a band loud on one side only still counts).
    floor = -80.
    every = []
    for live, ref_ in zip(report['matched']['live']['bands'], report['matched']['reference']['bands']):
        a = live['energyDbFS'] if live['energyDbFS'] is not None else floor
        b = ref_['energyDbFS'] if ref_['energyDbFS'] is not None else floor
        every.append({'band': live['name'], 'differenceDb': max(a, floor) - max(b, floor), 'loud': max(a, b) >= floor})
    try:            # the raw metric the warm GP was tested on (band minus loudness per side, unrounded)
        import warm_gp
        raw = warm_gp.report_descriptors(report, expected_version=reference_dsp.VERSION,
                                         expected_duration_seconds=report['durationSeconds'])
    except Exception:
        raw = None
    out = {'id': 'reference-measure:' + uuid.uuid4().hex, 'referenceId': target['id'], 'reference': target['name'],
           'passage': {'cueSeconds': target['cueSeconds'], 'durationSeconds': target['durationSeconds'], 'part': target['part']},
           'levelMatched': True, 'comparable': bool(target.get('comparable')),
           'bands': bands, 'allBands': every, 'descriptors': raw,
           'distanceDb': round(sum(abs(b['differenceDb']) for b in bands) / max(1, len(bands)), 3),
           'stereoCorrelationDifference': report['delta'].get('correlation'),
           'crestFactorDifferenceDb': report['delta'].get('crestFactorDb'), 'at': time.time()}
    if evidence_dir:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        (Path(evidence_dir) / (out['id'].split(':')[1] + '.json')).write_text(json.dumps({'measure': out, 'report': {
            k: report[k] for k in ('version', 'durationSeconds', 'gainDb', 'delta', 'alignment')}}, default=float))
    return out


def for_engineer(m):
    """What the engineer sees: synth minus reference per band after level matching (negative = less than the reference)."""
    return {'status': 'measured', 'reference': m['reference'], 'part': m['passage']['part'],
            'differencesDb': {b['band']: b['differenceDb'] for b in m['bands']}, 'averageDistanceDb': m['distanceDb'],
            'crestFactorDifferenceDb': m['crestFactorDifferenceDb'], 'note': CAVEAT}


def not_measured(target, why):
    if not target:
        return {'status': 'none', 'note': 'No reference passage is chosen. Ask him which record and where the part is '
                                         '(or to set it on the Reference page); do not guess what it sounds like.'}
    return {'status': 'not measured', 'reference': target['name'], 'why': why,
            'note': 'Do not describe the reference from memory; say what is needed to measure it.'}


def proposal_reference(target, m):
    """The proposal's reference_target block (schemas/common.json#reference)."""
    if not target:
        return None
    measured = m is not None
    return {'reference_id': target['id'], 'required': True,
            'comparison_status': 'measured' if measured and target.get('comparable') else 'not_measured',
            'comparable_material': bool(target.get('comparable')) if measured else None,
            'loudness_matched': True if measured else None,
            'evidence_ids': [m['id']] if measured else []}


def verdict(before, after):
    """Closer or further from the reference, per band and overall: a measurement both ways."""
    b = {x['band']: x['differenceDb'] for x in before['bands']}
    rows = [{'band': x['band'], 'beforeDb': b[x['band']], 'afterDb': x['differenceDb'],
             'closerDb': round(abs(b[x['band']]) - abs(x['differenceDb']), 2)} for x in after['bands'] if x['band'] in b]
    # Overall change over the bands measured both times. A band measured before but too quiet to measure
    # after (below the -80 dBFS floor) almost always means the change took it far from the reference: it is
    # named, and the result is never called closer while one is lost.
    a = {x['band'] for x in after['bands']}
    lost = [band for band in b if band not in a]
    gained = [band for band in a if band not in b]
    change = round(sum(r['closerDb'] for r in rows) / len(rows), 3) if rows else 0.
    if not rows:
        result = 'not compared'
    elif lost:
        result = 'further'
    else:
        result = 'closer' if change > .1 else 'further' if change < -.1 else 'no clear change'
    return {'closerOverallDb': change, 'result': result, 'bands': rows, 'sharedBands': len(rows),
            'lostBands': lost, 'gainedBands': gained, 'evidence_ids': [before['id'], after['id']]}


def english(v):
    moved = sorted(v['bands'], key=lambda r: -abs(r['closerDb']))[:3]
    parts = ', '.join('%s %s by %.1f dB' % (r['band'], 'closer' if r['closerDb'] > 0 else 'further', abs(r['closerDb']))
                      for r in moved if abs(r['closerDb']) >= .1)
    head = {'closer': 'Measured against the reference: closer', 'further': 'Measured against the reference: further away',
            'no clear change': 'Measured against the reference: no clear change',
            'not compared': 'Not compared with the reference: no band could be measured both times'}[v['result']]
    lost = v.get('lostBands') or []
    tail = (' %s fell below the measuring floor (much further from the reference).' % ', '.join(lost)) if lost else ''
    return head + (' (%s).' % parts if parts else '.') + tail
