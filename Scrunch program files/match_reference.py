"""Match Reference: John's explicit ON/OFF mode (25 Sep 2026).

OFF (the default): sound requests take the usual engineer route and reference matching never runs on its own;
the engineer model has no search op to pick.
ON: every sound request from chat, voice or the HUD runs Brooklyn Rose's full measured reference search. This is
routing by the mode's state, never by his words. It needs a loaded reference target and the selected synth hosted
in Scrunch Host; if either is missing the request is refused, saying which.

Stored in the server state (state.json, key 'matchReference'), so it survives a restart. POST toggles carry a
requestId: a repeated id returns the first answer and changes nothing.
"""
import time

KEY = 'matchReference'
KEEP_IDS = 50


def _rec(st):
    rec = st.get(KEY)
    if not isinstance(rec, dict):
        rec = {'on': False, 'changedAt': None, 'requests': {}}
        st[KEY] = rec
    rec.setdefault('requests', {})
    return rec


def is_on(st):
    rec = st.get(KEY)
    return isinstance(rec, dict) and rec.get('on') is True


def reference_name(st):
    ref = st.get('referenceTarget')
    if not isinstance(ref, dict):
        return None
    return ref.get('name') or ref.get('path') or ref.get('referenceId')


def pinned(st):
    """The hosted synth's track id while the mode is on, else None."""
    target = (st.get(KEY) or {}).get('target') if is_on(st) else None
    return target.get('trackId') if isinstance(target, dict) else None


def view(st):
    target = (st.get(KEY) or {}).get('target') if is_on(st) else None
    return {'on': is_on(st), 'reference': reference_name(st), 'changedAt': (st.get(KEY) or {}).get('changedAt'),
            'target': (target or {}).get('trackName') if isinstance(target, dict) else None}


def missing(st, c):
    """None when the search can run; else a plain sentence naming what is missing."""
    import warm_gp_engine
    gaps = []
    if not isinstance(st.get('referenceTarget'), dict):
        gaps.append('no reference is loaded (set one on the Reference page)')
    if c is None or warm_gp_engine.hosted_state(c) is None:
        plugin = ((getattr(c, 'm', None) or {}).get('plugin') or 'the selected synth') if c is not None else 'the selected synth'
        track = (getattr(c, 'm', None) or {}).get('trackName') if c is not None else None
        gaps.append('%s%s is not hosted in Scrunch Host' % (plugin, (' on "%s"' % track) if track else ''))
    if not gaps:
        return None
    return 'Match Reference cannot run: ' + ' and '.join(gaps) + '. Nothing changed.'


def toggle(st, body, coach_factory):
    """POST body {'on': bool, 'requestId': str}. Turning ON is refused (mode stays OFF) when not ready."""
    rid = str(body.get('requestId') or '')
    if not 1 <= len(rid) <= 100:
        raise ValueError('A requestId is required.')
    if not isinstance(body.get('on'), bool):
        raise ValueError("Send 'on': true or false.")
    rec = _rec(st)
    if rid in rec['requests']:
        return dict(rec['requests'][rid], duplicate=True)
    refused = None
    ms = {}
    started = time.perf_counter()
    if body['on']:
        c = None
        try:
            c = coach_factory()
            ms['coach'] = round((time.perf_counter() - started) * 1000)
            tick = time.perf_counter()
            c.ensure_device()
            ms['ensureDevice'] = round((time.perf_counter() - tick) * 1000)
        except Exception as exc:
            refused = 'Match Reference cannot run: the selected synth was not found (%s). Nothing changed.' % str(exc)[:160]
        if refused is None:
            tick = time.perf_counter()
            refused = missing(st, c)
            ms['readyCheck'] = round((time.perf_counter() - tick) * 1000)
    if refused is None:
        rec['on'] = body['on']
        # Two separate pointers: the reference is the audio passage; the target is the hosted synth selected
        # when the mode went ON. Words never move the target while the mode is on.
        rec['target'] = {'trackId': getattr(c, 'track', None), 'deviceId': getattr(c, 'device', None),
                         'trackName': (getattr(c, 'm', None) or {}).get('trackName')} if body['on'] else None
        rec['changedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    ms['total'] = round((time.perf_counter() - started) * 1000)
    answer = dict(view(st), refused=refused, ms=ms)
    rec['requests'][rid] = answer
    while len(rec['requests']) > KEEP_IDS:
        rec['requests'].pop(next(iter(rec['requests'])))
    return answer


def for_engineer(st):
    """Neutral context for the model while the mode is on (no interpretation of any word)."""
    return {'on': True, 'reference': reference_name(st),
            'note': 'Match Reference is on: Scrunch runs its measured search toward the reference for this message. '
                    'Your say is shown as the explanation; controls you name (knobs or searchControls) are offered to '
                    'the search as candidates it may include.'}
