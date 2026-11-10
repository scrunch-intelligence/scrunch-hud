"""Stored before/after: after a search or an engineer change, the sound before it and the last candidate are both
kept as full control vectors (every exposed control of the synth, read from the host) plus the track fader each was
heard at. Revert toggles between them, 'candidate' brings the exact last result back.

A toggle is host writes with compare-and-set and readback only: no model call, no render, no listen, no Set file.
The fader goes back to the level each side was heard at (the candidate's level-matched trim, the original's fader),
so the switch stays level-matched the way the A/B takes are.
"""
import json
import time
import uuid

import engine

FILE = 'audition-pairs.json'
TOL = 1e-4
class Moved(ValueError):
    """He moved a control the toggle would write: nothing is written over his edit."""


PAIRS = {}                 # device id -> pair (mirrored in data/audition-pairs.json across server restarts)
_loaded = [False]


def _path():
    return engine.DATA / FILE


def _load():
    if not _loaded[0]:
        _loaded[0] = True
        try:
            PAIRS.update(json.loads(_path().read_text()))
        except Exception:
            pass


def _save():
    try:
        _path().write_text(json.dumps(PAIRS))
    except Exception:
        pass


def _key(c):
    return str(getattr(c, 'device', None))


def snapshot(c):
    """Every exposed control (fresh host read) + the track fader. Returns {'values', 'names', 'fader', 'at'}."""
    knobs = c.knobs_view()
    try:
        fader = c.a.track(c.track, mixer=True).get('gainDb')
    except Exception:
        fader = None
    return {'values': {str(k['parameter']): float(k['value']) for k in knobs},
            'names': {str(k['parameter']): k['name'] for k in knobs},
            'labels': {str(k['parameter']): k.get('label') or k['name'] for k in knobs},
            'displays': {str(k['parameter']): k.get('display') for k in knobs},
            'stepped': [str(k['parameter']) for k in knobs if k.get('stepped')],
            'fader': fader, 'at': time.time()}


def commit(c, before, kind, words='', playing='candidate'):
    """Store `before` and what is live now (fresh readback) as the pair for this synth."""
    if not before:
        return None
    _load()
    after = snapshot(c)
    moved = [p for p, v in after['values'].items() if abs(v - before['values'].get(p, v)) > TOL]
    if not moved and before.get('fader') == after.get('fader'):
        return None
    pair = {'pairId': uuid.uuid4().hex, 'kind': kind, 'words': str(words)[:200], 'track': getattr(c, 'track', None),
            'original': before, 'candidate': after, 'playing': playing, 'moved': moved, 'toggles': 0}
    PAIRS[_key(c)] = pair
    _save()
    return pair


def get(c):
    _load()
    return PAIRS.get(_key(c))


def forget(c):
    _load()
    if PAIRS.pop(_key(c), None) is not None:
        _save()


# ---- keyed pairs of other kinds (arrangement automation): same store, same 'playing' sides and HUD buttons ----
# A single candidate offers Original / Candidate / Keep; an A/B offer Original / A / B / Neither / Keep
# (core/HudLayout.hpp; Hold and Play are the HUD's own transport buttons).
BUTTONS = {'single': ['Original', 'Candidate', 'Keep'], 'ab': ['Original', 'A', 'B', 'Neither', 'Keep']}


def put(key, pair):
    _load()
    PAIRS[key] = pair
    _save()
    return pair


def get_key(key):
    _load()
    return PAIRS.get(key)


def offer_panel(pair):
    """HUD offer for a keyed pair still waiting for his choice (same shape as panel())."""
    if not pair or pair.get('decided'):
        return None
    return {'offerId': pair['kind'] + ':' + pair['pairId'], 'kind': pair['kind'], 'playing': pair['playing'],
            'buttons': list(BUTTONS['ab' if 'B' in pair else 'single'])}


def panel(c):
    p = get(c) if c is not None else None
    if not p:
        return None
    return {'offerId': 'audition:' + p['pairId'], 'kind': 'audition', 'playing': p['playing'],
            'buttons': ['Original', 'Candidate']}


def toggle(c, side=None):
    """Play `side` ('original' / 'candidate'; None = the other one). One write of every control that differs,
    each carrying the value the side playing now left there; then a full readback against the stored vector."""
    p = get(c)
    if not p:
        raise ValueError('No before/after is stored for this synth yet.')
    side = side or ('original' if p['playing'] == 'candidate' else 'candidate')
    if side not in ('original', 'candidate'):
        raise ValueError('Unknown side.')
    started = time.perf_counter()
    if side == p['playing']:
        return {'playing': side, 'writes': 0, 'already': True, 'exact': True, 'ms': 0}
    want, was = p[side]['values'], p[p['playing']]['values']
    names = p[side]['names']
    stepped = set(p[side].get('stepped') or [])
    diff = [q for q in want if q in was and abs(want[q] - was[q]) > TOL]
    live = {str(k['parameter']): float(k['value']) for k in c.knobs_view()}
    moved = [p[side]['labels'].get(q, q) for q in diff if q in live and abs(live[q] - was[q]) > (.5 if q in stepped else TOL)]
    if moved:
        raise Moved('You have moved %s since my change, so I did not put it back over your edit.' % ', '.join(moved[:4]))
    writes = [{'id': _id(q), 'name': names[q], 'value': round(want[q], 6), 'expect_value': was[q]} for q in diff]
    if writes:
        c.a.set_device_parameters_together(c.device, writes)      # compare-and-set; refuses over his own edit
    now = {str(k['parameter']): float(k['value']) for k in c.knobs_view()}
    off = [q for q in want if q in now and abs(now[q] - want[q]) > (.5 if q in stepped else TOL)]
    fader = p[side].get('fader')
    if fader is not None and fader != p[p['playing']].get('fader'):
        c.a.update_track(c.track, gainDb=fader)
    # what the other chat paths think is live follows the side playing
    if side == 'original':
        p['move'], p['searchUndo'] = _plain(getattr(c, 'move', None)), getattr(c, 'search_undo', None)
        c.move, c.search_undo = None, None
    else:
        if p.get('move'):
            c.move = p['move']
        if p.get('searchUndo'):
            c.search_undo = p['searchUndo']
    p['playing'] = side
    p['toggles'] = p.get('toggles', 0) + 1
    _save()
    shown = lambda s_, q: (p[s_].get('displays') or {}).get(q) or round(p[s_]['values'][q], 4)
    rows = [{'label': p[side]['labels'].get(q, q), 'parameter': q, 'was': shown('original' if side == 'candidate' else 'candidate', q),
             'now': shown(side, q)} for q in diff]
    return {'playing': side, 'writes': len(writes), 'controls': len(want), 'exact': not off, 'knobs': rows,
            'reverted': side == 'original',
            'mismatched': [p[side]['labels'].get(q, q) for q in off][:6], 'fader': fader,
            'ms': round((time.perf_counter() - started) * 1000), 'modelCalls': 0, 'renders': 0}


def _id(q):
    try:
        return int(q)
    except ValueError:
        return q


def _plain(move):
    try:
        return json.loads(json.dumps(move)) if move else None
    except Exception:
        return None


def english(done):
    if done.get('already'):
        return 'Already playing the %s.' % ('original' if done['playing'] == 'original' else 'last candidate')
    rows = done.get('knobs') or []
    if done['playing'] == 'original':
        head = 'Put back: ' + '; '.join('%s back to %s' % (k['label'], k['now']) for k in rows[:8])
    else:
        head = 'Last candidate back: ' + '; '.join('%s: %s to %s' % (k['label'], k['was'], k['now']) for k in rows[:8])
    more = ' (and %d more)' % (len(rows) - 8) if len(rows) > 8 else ''
    return '%s%s.\n%d control%s written, all %d read back %s.' % (
        head, more, done['writes'], '' if done['writes'] == 1 else 's', done['controls'],
        'exactly' if done['exact'] else 'with differences on ' + ', '.join(done['mismatched']))
