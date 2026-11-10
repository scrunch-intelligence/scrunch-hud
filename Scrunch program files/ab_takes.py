"""Original / A / B: the engineer offers two different takes on his words, he hears each at the same loudness as the
original and picks one or neither; the pick is kept with everything around it, so Scrunch learns what his words
mean from his ears (John, 24 Sep: 'what do you like more, A or B').

Every take is written through the same verified path as any change (action_layer.execute: identity, prewrite
checks, readback), put back the same way, and level-matched on the track fader against the original sound.
"""
import hashlib
import json
import random
import re
import time
import uuid

import action_layer
import engine
import level_match

PENDING = {}          # device id -> the takes waiting for his pick (one comparison at a time per synth)

SWITCH = re.compile(r"^\s*(?:play |try |hear |let me hear |back to |go to )?(?:take )?(a|b|bee|the original|original)\s*[.!?]?\s*$", re.I)
PICK = re.compile(r"^\s*(?:i )?(?:keep|prefer|like|choose|pick|take|go with|use|want)(?: take)? (a|b)\b.*$|"
                  r"^\s*(a|b) (?:is better|it is|please|wins)\b.*$", re.I)
NEITHER = re.compile(r"^\s*(neither|none|none of them|neither of them|neither of those|not those|back to the original"
                     r"( please)?|keep the original|original is better)\s*[.!?]?\s*$", re.I)
PICKS_FILE = 'ab-picks.jsonl'
OFFERS_FILE = 'ab-offers.jsonl'   # the blind mapping of every offer, written before he hears anything
RNG = random.SystemRandom()


def blind_order():
    """Which of the engineer's two takes is called A: random per offer, so A is not always the model's first idea."""
    return ('first', 'second') if RNG.random() < .5 else ('second', 'first')


def _digest(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _ids(c, measured, result=None):
    """What identifies what he heard: the synth state read back, the verified write, and the audio measured."""
    try:
        state = {k['parameter']: k['value'] for k in c.knobs_view()}
    except Exception:
        state = None
    m = measured or {}
    return {'stateId': _digest(state) if state is not None else None,
            'resultId': (result or {}).get('result_id'),
            'audioId': m.get('audioId') or m.get('captureId'),
            'measurementId': _digest(m) if m else None}


def _key(c):
    return str(getattr(c, 'device', None) or '')


def pending(c):
    return PENDING.get(_key(c))


def _decision(side, t):
    return {'action': 'change', 'say': t['say'], 'revertFirst': False, 'intent': t['intent'], 'uncertainty': .5,
            'audition': True, 'expect': None, 'targets': t['targets']}


def _fresh_targets(c, targets):
    """The take's controls as they are now (their positions move when another take is playing)."""
    now = {k['parameter']: k for k in c.knobs_view()}
    return [dict(t, knob=now.get(t['knob']['parameter'], t['knob'])) for t in targets]


def _restores(c, p, side):
    """Controls the take playing now moved that `side` does not touch: written back to the original with it."""
    cur = p.get('playing')
    if cur not in ('A', 'B') or cur == side:
        return []
    mine = {x['knob']['parameter'] for x in p['takes'][side]['targets']}
    now = {k['parameter']: k for k in c.knobs_view()}
    out = []
    for x in p['takes'][cur]['targets']:
        par = x['knob']['parameter']
        k = now.get(par)
        if par in mine or k is None or par not in p['original']:
            continue
        span = (k['max'] - k['min']) or 1
        out.append({'knob': k, 'to': (p['original'][par] - k['min']) / span, 'why': 'back to the original (not part of this take)'})
    return out


def _rebase(c, p, side):
    """The change now playing, measured from the ORIGINAL (not from the take it replaced), so putting it back,
    'neither' or the original button return exactly to the original, and the fader to the original's level."""
    if not c.move:
        return
    now = {k['parameter']: k for k in c.knobs_view()}
    params = [x['knob']['parameter'] for x in p['takes'][side]['targets']]
    params += [s['parameter'] for s in c.move['plan']['steps'] if s['parameter'] not in params]
    steps, old = [], {s['parameter']: s for s in c.move['plan']['steps']}
    why = {x['knob']['parameter']: x.get('why') for x in p['takes'][side]['targets']}
    for par in params:
        k = now.get(par)
        if k is None or par not in p['original']:
            continue
        o = p['original'][par]
        d = k['value'] - o
        s = dict(old.get(par) or {'parameter': par, 'name': k['name'], 'setting': k['setting'], 'label': k['label'],
                                  'stepped': bool(k.get('stepped'))})
        s.update(origin=o, direction=1 if d >= 0 else -1, travel=abs(d), why=str(why.get(par) or s.get('why') or '')[:160])
        steps.append(s)
    c.move['plan']['steps'] = steps
    c.move['amount'] = 1.
    snap = c.move.get('snapshot') or {}
    for k in now.values():
        if k['parameter'] in p['original'] and k.get('setting') in snap:
            snap[k['setting']] = {'value': p['original'][k['parameter']], 'display': snap[k['setting']].get('display')}


def _apply(c, p, side, rid, generation):
    """Write `side` in one pass from whatever is playing now: its own targets plus, for controls the other take
    moved and this one does not, their original values. Never passes through the original on the way."""
    t = p['takes'][side]
    targets = _fresh_targets(c, t['targets']) + _restores(c, p, side)
    c.move = None                              # the take playing now is replaced, not put back first
    proposal, result, out = action_layer.execute(c, _decision(side, dict(t, targets=targets)),
                                                 p['text'], '%s:%s:%d' % (rid, side, int(time.time() * 1000)), generation)
    if result['status'] != 'verified':
        raise RuntimeError('take %s did not read back (%s)' % (side, result.get('failure_reason') or result['status']))
    p.setdefault('results', {})[side] = result
    _rebase(c, p, side)
    if c.move:
        c.move['levelBase'] = p['base']
        if p['base'] and p['base'].get('fader') is not None:
            c.move['levelTrim'] = {'track': c.track, 'from': p['base']['fader']}   # putting it back = original fader
    p['move'] = c.move                         # survives the synth being found again between his replies
    return out


def _level(c, p, side):
    heard = p['levels'].get(side)
    if heard:
        return level_match.match(c, None, heard, p['text'])
    base = p.get('base') or {}
    if base.get('fader') is not None and not level_match.intended(p['text']):
        c.a.update_track(c.track, gainDb=base['fader'])        # not measured: at least not the other take's trim
    return None


def _back_to_original(c, p=None):
    if not c.move and p and p.get('move') and p.get('playing') in ('A', 'B'):
        c.move = p['move']
    if c.move:
        c.steer('revert')                      # also puts the fader back (level_match.undo)
    if p is not None:
        p['move'] = None


def offer(c, decision, text, rid, generation, before, listen):
    """Make take B, listen, put it back; make take A, listen, leave A playing level-matched.
    `before` is the measured original; `listen(c)` returns a fresh measurement of what is playing now."""
    c.move = None                              # what he is hearing now is the original of this comparison
    base = {'lufs': before.get('loudnessLUFS'), 'fader': c.a.track(c.track, mixer=True).get('gainDb')} \
        if before and before.get('loudnessLUFS') is not None else None
    truth = {'first': {'targets': decision['targets'], 'say': decision.get('say') or '', 'intent': decision.get('intent') or ''},
             'second': {'targets': decision['targetsB'], 'say': decision.get('sayB') or '', 'intent': decision.get('intent') or ''}}
    order = blind_order()
    p = {'offerId': uuid.uuid4().hex[:12], 'blind': {'A': order[0], 'B': order[1]}, 'ids': {},
         'text': text, 'rid': rid, 'at': time.time(), 'plugin': c.m.get('plugin'),
         'preset': (c.m.get('preset') or {}).get('name'), 'base': base, 'levels': {}, 'playing': 'original',
         'original': {k['parameter']: k['value'] for k in c.knobs_view()}, 'before': before,
         'takes': {'A': truth[order[0]], 'B': truth[order[1]]}}
    p['ids']['original'] = _ids(c, before)
    PENDING[_key(c)] = p
    with open(engine.DATA / OFFERS_FILE, 'a') as f:
        f.write(json.dumps({'at': p['at'], 'offerId': p['offerId'], 'requestId': rid, 'request': text,
                            'plugin': p['plugin'], 'preset': p['preset'], 'blind': p['blind'],
                            'takes': {k: {'say': v['say'], 'targets': [{'parameter': x['knob']['parameter'], 'to': x['to']}
                                                                        for x in v['targets']]} for k, v in truth.items()}},
                           default=str) + '\n')
    changes = {}
    for side in ('B', 'A'):
        changes[side] = _apply(c, p, side, rid, generation)
        p['levels'][side] = listen(c)
        p['ids'][side] = _ids(c, p['levels'][side], (p.get('results') or {}).get(side))
        p['playing'] = side                    # A is written straight over B (no pass through the original)
    done = _level(c, p, 'A')
    _keep_audio(p)
    # Blind: nothing that tells the takes apart (no descriptions, no controls) until he has picked.
    return {'offerId': p['offerId'], 'takes': {s: {'loudnessLUFS': (p['levels'].get(s) or {}).get('loudnessLUFS')}
                                               for s in ('A', 'B')},
            'levelMatch': done, 'playing': 'A'}, said(p)


AUDIO_DIR = 'ab-audio'


def _save_wav(path, mono, rate):
    import wave
    import numpy as np
    pcm = (np.clip(np.asarray(mono, dtype=np.float32), -1, 1) * 32767).astype('<i2')
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


def _keep_audio(p):
    """The audio he hears (original, A, B) kept with the offer under data/ab-audio/<offerId>/. Never raises."""
    try:
        import sound_audio
        captures = {'original': (p.get('before') or {}).get('captureId'),
                    'A': (p['levels'].get('A') or {}).get('captureId'), 'B': (p['levels'].get('B') or {}).get('captureId')}
        ids = {}
        for side, cid in captures.items():
            got = sound_audio.kept_audio(cid) if cid else None
            ids[side] = cid if got is not None else None
            if got is not None: _save_wav(engine.DATA / AUDIO_DIR / p['offerId'] / ('%s.wav' % cid), *got)
        p['audioIds'] = ids
    except Exception as exc:
        p['audioIds'] = p.get('audioIds') or {}
        p['audioError'] = str(exc)[:200]


def said(p):
    return ('Two takes, A and B, each at the same loudness as the original. Now playing A. Say "B", "original" or '
            '"A" to compare, then "keep A", "keep B" or "neither". I will say what each one was after you pick.')


def reveal(p):
    a, b = p['takes']['A']['say'], p['takes']['B']['say']
    return 'A was: %s\nB was: %s' % (a or 'take A', b or 'take B')


def panel(c):
    """What the panel over Live shows while a comparison waits: neutral labels only."""
    p = pending(c)
    if not p:
        return None
    return {'offerId': p['offerId'], 'playing': p['playing'], 'buttons': ['Original', 'A', 'B', 'Neither', 'Keep']}


def switch(c, side, rid, generation):
    """Play the original, A or B (each at the original's loudness)."""
    p = pending(c)
    side = 'original' if side.lower().endswith('original') else side.upper()
    if p['playing'] == side:
        return {'playing': side}, 'Already playing %s.' % ('the original' if side == 'original' else side)
    if side == 'original':
        _back_to_original(c, p)
        p['playing'] = 'original'
    else:
        if not c.move and p.get('move') and p['playing'] in ('A', 'B'):
            c.move = p['move']
        _apply(c, p, side, rid, generation)    # straight from the take playing now to this one, one write
        p['playing'] = side
        _level(c, p, side)
    return {'playing': side}, 'Playing %s.' % ('the original' if side == 'original' else side)


def choose(c, side, words, rid, generation, source='user'):
    """His pick: keep A or B (as one change he can still put back), or neither (the original comes back)."""
    p = pending(c)
    if side == 'neither':
        _back_to_original(c, p)
        reply = 'Back to the original. Neither take is kept; I will note that neither was what you meant.'
    else:
        if p['playing'] != side:
            switch(c, side, rid, generation)
        if not c.move and p.get('move'):
            c.move = p['move']
        c.keep_move(words)
        reply = 'Kept %s.' % side
    reply += '\n' + reveal(p)
    record(c, p, side, words, source)
    PENDING.pop(_key(c), None)
    return {'picked': side}, reply


def drop(c, why='a new request'):
    """He moved on without picking: whatever is playing stays, and the comparison is recorded as unanswered."""
    p = PENDING.pop(_key(c), None)
    if p:
        record(c, p, None, why, 'user')


def record(c, p, side, words, source):
    truth = p.get('blind') or {'A': 'first', 'B': 'second'}
    row = {'at': time.time(), 'offerId': p.get('offerId'), 'blind': truth,
           'pickedTake': truth.get(side) if side in ('A', 'B') else side, 'ids': p.get('ids') or {},
           'source': source, 'request': p['text'], 'requestId': p['rid'], 'plugin': p['plugin'],
           'preset': p['preset'], 'picked': side, 'words': str(words)[:300], 'original': p['original'],
           'audioIds': p.get('audioIds') or {},
           'before': {k: (p['before'] or {}).get(k) for k in ('loudnessLUFS', 'brightnessHz', 'highShare', 'widthShare')},
           'takes': {s: {'say': t['say'], 'intent': t['intent'],
                         'targets': [{'parameter': x['knob']['parameter'], 'label': x['knob'].get('label'), 'to': x['to'],
                                      'why': x.get('why')} for x in t['targets']],
                         'heard': {k: (p['levels'].get(s) or {}).get(k) for k in ('loudnessLUFS', 'brightnessHz', 'highShare',
                                                                                   'widthShare')}}
                     for s, t in p['takes'].items()}}
    with open(engine.DATA / PICKS_FILE, 'a') as f:
        f.write(json.dumps(row, default=str) + '\n')


def reply_to(c, text):
    """His words while a comparison is waiting: ('switch', side) / ('pick', side) / ('pick', 'neither') / None."""
    if not pending(c):
        return None
    if NEITHER.match(text):
        return 'pick', 'neither'
    m = PICK.match(text)
    if m:
        return 'pick', (m.group(1) or m.group(2)).upper()
    m = SWITCH.match(text)
    if m:
        return 'switch', 'b' if m.group(1).lower() == 'bee' else m.group(1)
    return None


PANEL = ('original', 'A', 'B', 'neither', 'keep')


def panel_answer(c, action):
    """A panel button as the same answer his words give: Keep = the take playing now (the original = neither)."""
    if action not in PANEL:
        raise ValueError('Unknown A/B button.')
    if action == 'keep':
        playing = pending(c)['playing']
        return 'pick', 'neither' if playing == 'original' else playing
    if action == 'neither':
        return 'pick', 'neither'
    return 'switch', action
