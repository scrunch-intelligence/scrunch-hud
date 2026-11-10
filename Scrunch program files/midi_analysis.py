"""Measurements of one MIDI part, for the private reference library and for scoring takes.

Everything here is arithmetic on notes: where onsets fall on the grid, which intervals follow
which, how often bars repeat, which pitch classes sound together. None of it is musical
judgement, and nothing is keyed to words - a role label never changes what is measured.
Chord and key figures are the best-fitting templates with their fit, reported as candidates,
not as the truth about the harmony.

Notes are `{p,t,d,v}` in quarter-note beats. `beats_per_bar` comes from the file's meter.
"""
import math
from collections import Counter

VERSION = 1
GRID = 0.25          # a sixteenth, in beats
TOL = 1 / 96         # onsets closer than this share a position
PC = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
# Krumhansl-Kessler key profiles.
MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
TRIADS = {'major': (0, 4, 7), 'minor': (0, 3, 7), 'diminished': (0, 3, 6), 'suspended': (0, 5, 7)}


def _r(x, n=4):
    return round(float(x), n)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _corr(a, b):
    ma, mb = _mean(a), _mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


def _cos(a, b):
    den = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / den if den else 0.0


def onsets(notes):
    """Distinct onset times, merging onsets closer than TOL."""
    out = []
    for t in sorted(n['t'] for n in notes):
        if not out or t - out[-1] > TOL:
            out.append(t)
    return out


def skyline(notes):
    """The top voice: the highest note at each onset. Used for interval and contour measures."""
    line = []
    for n in sorted(notes, key=lambda n: (n['t'], -n['p'])):
        if line and abs(n['t'] - line[-1]['t']) <= TOL:
            continue
        line.append(n)
    return line


def level(step, steps):
    """Metric level of a sixteenth position in a bar: 0 bar, 1 half bar, 2 beat, 3 eighth, 4 sixteenth."""
    if step == 0:
        return 0
    if steps % 2 == 0 and step == steps // 2:
        return 1
    if step % 4 == 0:
        return 2
    return 3 if step % 2 == 0 else 4


def _step(t, bar):
    return int(round((t % bar) / GRID)) % max(1, int(round(bar / GRID)))


def rhythm(notes, bar):
    steps = max(1, int(round(bar / GRID)))
    ons = onsets(notes)
    grid = [0] * steps
    for t in ons:
        grid[_step(t, bar)] += 1
    on_grid = sum(abs(t / GRID - round(t / GRID)) * GRID <= TOL for t in ons)
    iois = [b - a for a, b in zip(ons, ons[1:])]
    line = skyline(notes)
    legato = [n['d'] / (m['t'] - n['t']) for n, m in zip(line, line[1:]) if m['t'] - n['t'] > TOL]
    return {'onsetCount': len(ons), 'positionHistogram': grid,
            'onGridShare': _r(on_grid / len(ons)) if ons else 0.0,
            'ioiHistogram': {str(_r(k, 3)): v for k, v in sorted(Counter(round(i / GRID) * GRID for i in iois).items())},
            'durationHistogram': {str(_r(k, 3)): v for k, v in sorted(Counter(max(1, round(n['d'] / GRID)) * GRID for n in notes).items())},
            'legatoRatio': _r(_mean(legato))}


def intervals(line):
    ps = [n['p'] for n in line]
    iv = [b - a for a, b in zip(ps, ps[1:])]
    if not iv:
        return {'count': 0, 'histogram': {}, 'repeatShare': 0.0, 'stepShare': 0.0, 'skipShare': 0.0,
                'leapShare': 0.0, 'meanAbs': 0.0, 'topTrigrams': []}
    a = [abs(i) for i in iv]
    tri = Counter(tuple(iv[i:i + 3]) for i in range(len(iv) - 2))
    return {'count': len(iv), 'histogram': {str(k): v for k, v in sorted(Counter(max(-24, min(24, i)) for i in iv).items())},
            'repeatShare': _r(a.count(0) / len(a)), 'stepShare': _r(sum(1 <= x <= 2 for x in a) / len(a)),
            'skipShare': _r(sum(3 <= x <= 4 for x in a) / len(a)), 'leapShare': _r(sum(x >= 5 for x in a) / len(a)),
            'meanAbs': _r(_mean(a)), 'topTrigrams': [[list(k), v] for k, v in tri.most_common(10)]}


def contour(line, length, bar):
    ps = [n['p'] for n in line]
    signs = [(b > a) - (b < a) for a, b in zip(ps, ps[1:])]
    moving = [s for s in signs if s]
    changes = sum(1 for x, y in zip(moving, moving[1:]) if x != y)
    bars = max(1, int(math.ceil(length / bar - 1e-9)))
    per_bar = []
    for b in range(bars):
        inside = [n['p'] for n in line if b * bar - TOL <= n['t'] < (b + 1) * bar - TOL]
        per_bar.append(_r(_mean(inside), 2) if inside else None)
    peak = max(line, key=lambda n: n['p'])['t'] / length if line and length else 0.0
    n = len(signs) or 1
    return {'upShare': _r(signs.count(1) / n), 'downShare': _r(signs.count(-1) / n), 'flatShare': _r(signs.count(0) / n),
            'directionChangesPerMove': _r(changes / len(moving)) if moving else 0.0,
            'peakPosition': _r(peak), 'barMeanPitch': per_bar}


def density(notes, length, bar):
    bars = max(1, int(math.ceil(length / bar - 1e-9)))
    per_bar = [0] * bars
    for n in notes:
        per_bar[min(bars - 1, int((n['t'] + TOL) // bar))] += 1
    poly = [sum(1 for m in notes if m['t'] - TOL <= t < m['t'] + m['d'] - TOL) for t in onsets(notes)]
    return {'notesPerBeat': _r(len(notes) / length) if length else 0.0, 'notesPerBar': per_bar,
            'meanPolyphony': _r(_mean(poly)), 'maxPolyphony': max(poly) if poly else 0}


def note_range(notes):
    ps = sorted(n['p'] for n in notes)
    mid = len(ps) // 2
    median = ps[mid] if len(ps) % 2 else (ps[mid - 1] + ps[mid]) / 2
    return {'lowest': ps[0], 'highest': ps[-1], 'span': ps[-1] - ps[0], 'mean': _r(_mean(ps), 2),
            'median': median, 'std': _r(_std(ps), 2)}


def phrasing(line, rest=1.0):
    """Phrases split where the top voice rests for at least `rest` beats."""
    if not line:
        return {'phraseCount': 0, 'phraseBeats': [], 'notesPerPhrase': [], 'meanRestBeats': 0.0}
    phrases = [[line[0]]]; rests = []
    for prev, n in zip(line, line[1:]):
        gap = n['t'] - (prev['t'] + prev['d'])
        if gap >= rest - TOL:
            rests.append(gap); phrases.append([n])
        else:
            phrases[-1].append(n)
    return {'phraseCount': len(phrases),
            'phraseBeats': [_r(p[-1]['t'] + p[-1]['d'] - p[0]['t'], 3) for p in phrases],
            'notesPerPhrase': [len(p) for p in phrases], 'meanRestBeats': _r(_mean(rests), 3)}


def _bars(notes, length, bar):
    count = max(1, int(math.ceil(length / bar - 1e-9)))
    out = [[] for _ in range(count)]
    for n in notes:
        out[min(count - 1, int((n['t'] + TOL) // bar))].append(n)
    return out


def _sig(notes, bar, transpose=False):
    base = min((n['p'] for n in notes), default=0) if transpose else 0
    return frozenset((round((n['t'] % bar) * 48), n['p'] - base) for n in notes)


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 1.0


def repetition(notes, line, length, bar):
    bars = [b for b in _bars(notes, length, bar) if b]
    exact = [_sig(b, bar) for b in bars]
    moved = [_sig(b, bar, True) for b in bars]
    ps = [n['p'] for n in line]
    iv = [b - a for a, b in zip(ps, ps[1:])]
    grams = Counter(tuple(iv[i:i + 4]) for i in range(len(iv) - 3))
    total = sum(grams.values())
    return {'nonEmptyBars': len(bars),
            'exactBarRepeatShare': _r(1 - len(set(exact)) / len(exact)) if exact else 0.0,
            'transposedBarRepeatShare': _r(1 - len(set(moved)) / len(moved)) if moved else 0.0,
            'intervalFourgramRepeatShare': _r(sum(v for v in grams.values() if v > 1) / total) if total else 0.0}


def syncopation(notes, bar, length=None):
    """Onsets off the beat, and a Longuet-Higgins/Lee-style weight per onset.

    An onset on a weaker position scores when the next stronger position passes without a new
    onset (the note is held or rests through it). The score is the level difference. The part is
    treated as a loop: after the last onset comes the first one again, one part-length later.
    """
    steps = max(1, int(round(bar / GRID)))
    ons = onsets(notes)
    if not ons:
        return {'offBeatShare': 0.0, 'sixteenthOffbeatShare': 0.0, 'weightedPerOnset': 0.0}
    off_beat = sum(abs(t - round(t)) > TOL for t in ons)
    sixteenth = sum(level(_step(t, bar), steps) == 4 for t in ons)
    score = 0
    for i, t in enumerate(ons):
        k = int(round(t / GRID)); lv = level(k % steps, steps)
        nxt = ons[i + 1] if i + 1 < len(ons) else (ons[0] + length if length else None)
        j = k + 1
        while level(j % steps, steps) >= lv and j - k <= steps:
            j += 1
        if j - k <= steps and (nxt is None or nxt > j * GRID + TOL):
            score += lv - level(j % steps, steps)
    return {'offBeatShare': _r(off_beat / len(ons)), 'sixteenthOffbeatShare': _r(sixteenth / len(ons)),
            'weightedPerOnset': _r(score / len(ons))}


def velocity(notes, bar):
    vs = [n['v'] for n in notes]
    beat = [n['v'] for n in notes if abs(n['t'] - round(n['t'])) <= TOL]
    off = [n['v'] for n in notes if abs(n['t'] - round(n['t'])) > TOL]
    steps = max(1, int(round(bar / GRID)))
    by_pos = {}
    for n in notes:
        by_pos.setdefault(_step(n['t'], bar), []).append(n['v'])
    return {'mean': _r(_mean(vs), 2), 'std': _r(_std(vs), 2), 'lowest': min(vs), 'highest': max(vs),
            'onBeatMinusOffBeat': _r(_mean(beat) - _mean(off), 2) if beat and off else 0.0,
            'pitchCorrelation': _r(_corr([n['p'] for n in notes], vs)) if len(set(vs)) > 1 else 0.0,
            'meanByPosition': [_r(_mean(by_pos[k]), 1) if k in by_pos else None for k in range(steps)]}


def chroma(notes, start=None, end=None):
    """Duration-weighted pitch-class profile, optionally only between start and end beats."""
    c = [0.0] * 12
    for n in notes:
        a, b = n['t'], n['t'] + n['d']
        if start is not None:
            a, b = max(a, start), min(b, end)
        if b > a:
            c[n['p'] % 12] += b - a
    total = sum(c)
    return [x / total for x in c] if total else c


def key_candidates(c, top=3):
    rows = []
    for tonic in range(12):
        for mode, prof in (('major', MAJOR), ('minor', MINOR)):
            rows.append((_corr(c, prof[-tonic:] + prof[:-tonic]), PC[tonic], mode))
    rows.sort(reverse=True)
    return [{'tonic': t, 'mode': m, 'fit': _r(r)} for r, t, m in rows[:top]]


def chord_candidate(c):
    best = None
    for root in range(12):
        for quality, shape in TRIADS.items():
            tpl = [0.0] * 12
            for s in shape:
                tpl[(root + s) % 12] = 1.0
            fit = _cos(c, tpl)
            if best is None or fit > best[0] + 1e-9:
                best = (fit, PC[root], quality)
    return {'root': best[1], 'quality': best[2], 'fit': _r(best[0])} if best and best[0] > 0 else None


def harmony(notes, length, bar):
    c = chroma(notes)
    bars = max(1, int(math.ceil(length / bar - 1e-9)))
    per = [chroma(notes, b * bar, (b + 1) * bar) for b in range(bars)]
    chords = [chord_candidate(x) if any(x) else None for x in per]
    moves = [1 - _cos(a, b) for a, b in zip(per, per[1:]) if any(a) and any(b)]
    named = [(x['root'], x['quality']) if x else None for x in chords]
    changes = sum(1 for a, b in zip(named, named[1:]) if a and b and a != b)
    lows = [min(n['p'] for n in bb) if bb else None for bb in _bars(notes, length, bar)]
    root_motion = [((b - a + 5) % 12) - 5 for a, b in zip(lows, lows[1:]) if a is not None and b is not None]
    return {'pitchClassProfile': [_r(x) for x in c], 'keyCandidates': key_candidates(c),
            'barChordCandidates': chords, 'chordChangeShare': _r(changes / max(1, len(named) - 1)),
            'meanChromaMovement': _r(_mean(moves)), 'bassRootMotion': root_motion,
            'pitchClassesUsed': sum(1 for x in c if x > 0)}


def structure(notes, length, bar, same=0.75):
    """Bar letters (A, B, A...) by onset-and-pitch overlap, the shortest repeat period, and half-to-half similarity."""
    bars = _bars(notes, length, bar)
    sigs = [_sig(b, bar) for b in bars]
    letters = []; seen = []
    for s in sigs:
        if not s:
            letters.append('-'); continue
        match = next((L for L, ref in seen if _jaccard(s, ref) >= same), None)
        if match is None:
            match = chr(ord('A') + len(seen)) if len(seen) < 26 else '*'
            seen.append((match, s))
        letters.append(match)
    n = len(letters); period = None
    for p in range(1, n // 2 + 1):
        pairs = [(letters[i], letters[i + p]) for i in range(n - p)]
        if pairs and sum(a == b for a, b in pairs) / len(pairs) >= 0.9:
            period = p; break
    half = n // 2
    halves = None
    if half:
        first = frozenset((round((m['t'] % (half * bar)) * 48), m['p']) for m in notes if m['t'] < half * bar - TOL)
        second = frozenset((round((m['t'] % (half * bar)) * 48), m['p']) for m in notes if half * bar - TOL <= m['t'] < 2 * half * bar - TOL)
        halves = _r(_jaccard(first, second))
    return {'bars': n, 'barLetters': ''.join(letters), 'distinctBars': len(seen),
            'repeatPeriodBars': period, 'halfSimilarity': halves}


def analyse(notes, length, beats_per_bar=4.0, drums=False):
    """All measures for one part. Pitched measures are skipped for drum parts, with the reason."""
    if not notes:
        raise ValueError('An empty part has nothing to analyse.')
    length = max(float(length), max(n['t'] + n['d'] for n in notes))
    bar = float(beats_per_bar)
    line = skyline(notes)
    out = {'version': VERSION, 'noteCount': len(notes), 'lengthBeats': _r(length, 3), 'beatsPerBar': bar,
           'rhythm': rhythm(notes, bar), 'density': density(notes, length, bar),
           'syncopation': syncopation(notes, bar, length), 'velocity': velocity(notes, bar),
           'repetition': repetition(notes, line, length, bar), 'structure': structure(notes, length, bar),
           'phrasing': phrasing(line)}
    if drums:
        voices = {}
        for n in notes:
            voices.setdefault(n['p'], [0] * max(1, int(round(bar / GRID))))[_step(n['t'], bar)] += 1
        out['drumVoices'] = {str(k): v for k, v in sorted(voices.items())}
        out['pitched'] = 'skipped: drum part; pitch is instrument choice, not melody or harmony'
    else:
        out.update({'noteRange': note_range(notes), 'intervals': intervals(line),
                    'contour': contour(line, length, bar), 'harmony': harmony(notes, length, bar)})
    return out
