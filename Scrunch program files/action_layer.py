"""The action layer of the two-layer engineer (build spec step 3).

The conversation layer (the model) only proposes. This layer turns its reply into a structured
proposal (schemas/action-proposal.json), and immediately before writing re-checks the target, the
starting revision and cancellation (request cancel registry and the AAR kill switch). It captures
restore values first, writes only verified targets, reads every control back and returns a verified
result (schemas/verified-result.json). What changed is told only from that result.

"Put it back" is plain code: it refuses, as a conflict, when the producer has moved one of the
controls since the change, instead of overwriting his edit.
"""
import os
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

import sound_moves
from schemas import validate

AAR_STATE = Path(os.environ.get('SCRUNCH_AAR_ROOT') or Path.home() / 'Library' / 'Application Support' / 'Scrunch') / 'aar' / 'state.json'
# The preview build of the panel keeps its state beside it; both are honoured.
AAR_STATES = (AAR_STATE, AAR_STATE.parent.parent / 'aar-visible-prototype' / 'state.json')
CANCELLED = set()                    # request IDs the producer cancelled (kill switch, stop, new message)
SESSION_IDLE = 45 * 60               # seconds of quiet that end a working session (question ceiling)
QUESTIONS_PER_SESSION = 2


def _tol(k):
    return 0.5 if k.get('stepped') else 1e-4


def canonical(value):
    """Host-canonical form of a numeric control value: the bridge writes and reads 6 decimals."""
    return round(float(value), 6)


# ---- kill switch and cancellation ----------------------------------------------------------

def _alive(pid):
    import os
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def aar_state(path=None):
    """(allowed, generation, reason). No overlay running means nothing blocks writes.

    A state file counts while its panel is running (its pid alive), or when the watchdog wrote it (pid 0,
    mid-restart), or when it carries no pid. A panel that has quit leaves no switch behind."""
    if path:
        paths = [Path(path)]
    elif AAR_STATE in AAR_STATES:
        paths = list(AAR_STATES)
    else:                                              # AAR_STATE redirected (tests): honour only it
        paths = [AAR_STATE]
    live = []
    for p in paths:
        try:
            st = json.loads(Path(p).read_text())
        except (OSError, ValueError):
            continue
        pid = st.get('pid')
        if pid in (None, 0) or _alive(pid):
            live.append(st)
    for st in live:
        if not st.get('enabled', True) or st.get('safeMode'):
            return False, st.get('generation'), 'Scrunch is switched off (kill switch or safe mode), so nothing was changed.'
    return True, (live[0].get('generation') if live else None), None


def cancel(request_id):
    CANCELLED.add(str(request_id))


def cancel_clear(request_id, generation, aar_path=None):
    """(clear, reason) right now, for a request that started at AAR `generation`."""
    if str(request_id) in CANCELLED:
        return False, 'The request was cancelled before anything was written.'
    allowed, gen, why = aar_state(aar_path)
    if not allowed:
        return False, why
    if generation is not None and gen is not None and gen != generation:
        return False, 'Scrunch was switched off and on while this request was pending; nothing was written.'
    return True, None


# ---- identity and revision --------------------------------------------------------------------

def identity(c, set_id=None):
    """Exact target: Set, track, device, instance and plug-in. Names or indexes alone are not identity."""
    device = c.device or c._device()
    instance = str(device)
    try:
        import wrapper_backend
        if isinstance(c.a, wrapper_backend.WrappedAdapter) and c.a.hosts(device):
            instance = 'scrunch-host:' + str(c.a.wrapper(device).hello().get('identity') or device)
    except Exception:
        pass
    return {'set_id': str(set_id or 'current-set'), 'track_id': str(c.track), 'device_id': str(device),
            'instance_id': instance, 'plugin_id': str(c.m.get('plugin') or 'unknown')}


def revision(knobs):
    """Opaque revision of every control's value on the target."""
    rows = sorted((str(k['parameter']), canonical(k['value'])) for k in knobs)
    return 'r:' + hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:20]


def _mapping(k):
    return {'kind': 'range', 'source': 'host parameter range' + (' (stepped)' if k.get('stepped') else ''),
            'unit': 'host units', 'minimum': canonical(k['min']), 'maximum': canonical(k['max']), 'values': []}


# ---- proposal ------------------------------------------------------------------------------

def propose(decision, words, request_id, knobs, target, start_revision, set_revision=None):
    """The model's reply as a structured proposal. Nothing is written here."""
    plan = sound_moves.target_plan(decision.get('targets') or [], name=str(words)[:40] or 'change')
    requested = {w['id']: w['value'] for w in sound_moves.writes(plan, 1., knobs)}
    by = {k['parameter']: k for k in knobs}
    changes = []
    for s in plan['steps']:
        k = by[s['parameter']]
        before, after = canonical(k['value']), canonical(requested[s['parameter']])
        if before == after:
            continue
        changes.append({'control_id': str(s['parameter']), 'field_kind': 'parameter', 'before': before, 'after': after,
                        'direction': 'increase' if after > before else 'decrease', 'magnitude': abs(after - before),
                        'mapping': _mapping(k)})
    for ch in changes:                                     # the validator checks |after-before| exactly
        ch['magnitude'] = abs(ch['after'] - ch['before'])
    action = 'change' if changes else 'no_action'
    reason = None if changes else (decision.get('say') or 'No control on this patch would change.')
    expect = decision.get('expect')
    p = {'schema_version': 1, 'request_id': str(request_id), 'target_identity': target,
         'start_revision': start_revision, 'cancel_requested': str(request_id) in CANCELLED,
         'user_words': str(words)[:2000] or '-', 'interpreted_intent': (decision.get('intent') or decision.get('say') or str(words))[:500] or '-',
         'action': action, 'reference_target': decision.get('referenceTarget'), 'changes': changes,
         'expected_measurable_effect': ('%s %s' % expect) if expect else None,
         'uncertainty': float(decision.get('uncertainty', .3)), 'question_required': False, 'question': None,
         'explanation_draft': str(decision.get('say') or '')[:500] or '-',
         'undo_plan': 'Restore the captured values if every changed control still reads what this change wrote.',
         'no_action_reason': reason}
    return validate('action-proposal', p), plan


def no_write_result(proposal, status, reason, checks=None, started=None):
    r = {'schema_version': 1, 'request_id': proposal['request_id'], 'target_identity': proposal['target_identity'],
         'start_revision': proposal['start_revision'], 'cancel_requested': status == 'cancelled',
         'result_id': str(uuid.uuid4()), 'end_revision': proposal['start_revision'], 'status': status,
         'prewrite_checks': checks or {'target_matches': True, 'revision_matches': True, 'cancel_clear': status != 'cancelled'},
         'partial_write': False, 'writes': [], 'restore_data': None, 'measurements': [], 'inferences': [],
         'failure_reason': reason, 'latency_ms': round((time.perf_counter() - (started or time.perf_counter())) * 1000),
         'cost_usd': 0}
    return validate('verified-result', r)


# ---- execution -------------------------------------------------------------------------------

def execute(c, decision, words, request_id, generation, set_id=None, aar_path=None):
    """Propose, re-check, write, read back. Returns (proposal, result, out) where out is the coach's
    own report of the change (for the UI), or {} when nothing was written."""
    started = time.perf_counter()
    knobs = c.knobs_view()
    target = identity(c, set_id)
    start = revision(knobs)
    proposal, plan = propose(decision, words, request_id, knobs, target, start)
    if proposal['action'] == 'no_action':
        return proposal, no_write_result(proposal, 'no_action', proposal['no_action_reason'], started=started), {}

    # Immediately before the write: same target, nothing moved since the proposal, not cancelled.
    now_knobs = c.knobs_view()
    checks = {'target_matches': identity(c, set_id) == target, 'revision_matches': revision(now_knobs) == start}
    clear, why = cancel_clear(request_id, generation, aar_path)
    checks['cancel_clear'] = clear
    if not clear:
        return proposal, no_write_result(proposal, 'cancelled', why, checks, started), {}
    if not checks['target_matches']:
        return proposal, no_write_result(proposal, 'conflict', 'The instrument changed while I was deciding; nothing was written.', checks, started), {}
    if not checks['revision_matches']:
        moved = [k['label'] for k, n in zip(knobs, now_knobs) if abs(k['value'] - n['value']) > _tol(k)]
        return proposal, no_write_result(proposal, 'conflict', 'Controls moved while I was deciding (%s); nothing was written.'
                                         % ', '.join(moved[:4] or ['the patch']), checks, started), {}

    restore = {'captured_before_write': True, 'state_revision': start,
               'values': {ch['control_id']: ch['before'] for ch in proposal['changes']}}
    c.last_restore = restore
    error = None
    out = {}
    try:
        out = c.change(dict(decision, revertFirst=False), words, knobs=now_knobs)
    except Exception as exc:                              # the write itself refused or was not verified
        error = str(exc)[:300]
    after = {str(k['parameter']): k for k in c.knobs_view()}
    writes = []
    for ch in proposal['changes']:
        k = after.get(ch['control_id'])
        readback = canonical(k['value']) if k else None
        ok = error is None and readback is not None and readback == ch['after']
        writes.append({'control_id': ch['control_id'], 'before': ch['before'], 'requested': ch['after'],
                       'readback': readback, 'verified': ok,
                       'error': None if ok else (error or 'read back %s, not %s' % (readback, ch['after']))})
    touched = [w for w in writes if w['readback'] is not None and w['readback'] != w['before']]
    if all(w['verified'] for w in writes):
        status, reason = 'verified', None
    elif touched:
        status, reason = 'partial', error or 'Some controls did not read back what was written.'
    else:
        status, reason, writes = 'failed', error or 'Nothing took the new values.', []
    result = {'schema_version': 1, 'request_id': proposal['request_id'], 'target_identity': target,
              'start_revision': start, 'cancel_requested': False, 'result_id': str(uuid.uuid4()),
              'end_revision': revision(list(after.values())), 'status': status, 'prewrite_checks': checks,
              'partial_write': status == 'partial', 'writes': writes,
              'restore_data': restore if writes else None, 'measurements': [], 'inferences': [],
              'failure_reason': reason, 'latency_ms': round((time.perf_counter() - started) * 1000), 'cost_usd': 0}
    result = validate('verified-result', result)
    if status == 'verified':
        c.last_verified = {'proposal': proposal, 'result': result}
    return proposal, result, out


def _last_verified(c, target, store=None):
    """The engineer's last verified change on this exact target: this session's, else move history."""
    mine = getattr(c, 'last_verified', None)
    if isinstance(mine, dict) and mine['proposal']['target_identity'] == target and mine['result']['status'] == 'verified':
        return mine['proposal']['changes']
    try:
        import taste_memory
        moves = [m for m in (store or taste_memory.MemoryStore()).moves(target)
                 if m['verified_result']['status'] == 'verified']
    except Exception:
        moves = []
    return moves[-1]['changes'] if moves else None


def further(c, amount, words, request_id, generation, set_id=None, store=None, aar_path=None):
    """"Again" / "push it further": the last verified change repeated from where the controls are now,
    `amount` times its size (1 = the same step again, .5 half, negative back the other way). Each control
    stops at its own range; a control he has moved since is a conflict, not a guess."""
    knobs = c.knobs_view()
    target = identity(c, set_id)
    base = _last_verified(c, target, store)
    stub = {'schema_version': 1, 'request_id': str(request_id), 'target_identity': target,
            'start_revision': revision(knobs)}
    if not base:
        return None, no_write_result(stub, 'no_action', "There is no earlier change of mine on this sound to continue."), {}, []
    by = {str(k['parameter']): k for k in knobs}
    moved = [by[ch['control_id']]['label'] for ch in base
             if ch['control_id'] in by and abs(by[ch['control_id']]['value'] - ch['after']) > _tol(by[ch['control_id']])]
    if moved:
        return None, no_write_result(stub, 'conflict', 'You have moved %s since my last change, so I did not push it '
                                     'further from a guess.' % ', '.join(moved[:4])), {}, []
    targets, limits = [], []
    for ch in base:
        k = by.get(ch['control_id'])
        if k is None or k['max'] <= k['min'] or ch['direction'] == 'set':
            continue
        sign = 1 if ch['direction'] == 'increase' else -1
        goal = k['value'] + sign * ch['magnitude'] * float(amount)
        if not k['min'] <= goal <= k['max']:
            limits.append(k['label'])
            goal = min(k['max'], max(k['min'], goal))
        targets.append({'knob': k, 'to': (goal - k['min']) / (k['max'] - k['min']), 'why': 'the same move, %sx' % amount})
    decision = {'action': 'change', 'say': '', 'revertFirst': False, 'intent': 'continue the last change x%s' % amount,
                'uncertainty': 0., 'audition': False, 'targets': targets, 'expect': None}
    proposal, result, out = execute(c, decision, words, request_id, generation, set_id, aar_path)
    if result['status'] == 'verified' and isinstance(getattr(c, 'last_verified', None), dict):
        # 'More' and 'less' stay relative to what he asked for: a step back is not a new direction, or a second
        # 'less' would undo the first (24 Sep: 'less' twice went 3.75 -> 5.62 -> 4.69, back toward 'more').
        origin = {ch['control_id']: ch['direction'] for ch in base}
        for ch in c.last_verified['proposal']['changes']:
            if origin.get(ch['control_id']) in ('increase', 'decrease'):
                ch['direction'] = origin[ch['control_id']]
    return proposal, result, out, limits


def revert_conflicts(c):
    """Controls the producer moved after the engineer's last change. Putting back over them would
    overwrite his edit, so revert refuses while this list is not empty."""
    move = getattr(c, 'move', None)
    if not move or not move.get('applied'):
        return []
    now = {k['parameter']: k for k in c.knobs_view()}
    out = []
    for s, p in zip(move['plan']['steps'], move['applied']):
        k = now.get(s['parameter'])
        if k is not None and abs(k['value'] - p['value']) > _tol(k):
            out.append(k['label'])
    return out


# ---- what is said about it -------------------------------------------------------------------

def _named(text, knobs):
    """Controls of this synth that a sentence mentions by label or setting."""
    t = ' ' + re.sub(r'\s+', ' ', str(text).lower()) + ' '
    found = set()
    for k in knobs:
        for name in {str(k.get('label') or ''), str(k.get('setting') or ''), str(k.get('name') or '')}:
            n = name.strip().lower()
            if len(n) >= 3 and re.search(r'(?<![\w])' + re.escape(n) + r'(?![\w])', t):
                found.add(str(k['parameter']))
    return found


def explain(result, out, say, knobs, audition=False):
    """The reply. What changed comes only from the verified result; the model's words are kept only
    when they name no control that did not change (so it cannot describe a change it did not make)."""
    changed = {w['control_id'] for w in result['writes'] if w['readback'] != w['before']}
    lines = []
    if result['status'] in ('no_action', 'conflict', 'cancelled', 'failed'):
        return result['failure_reason'] or 'Nothing was changed.'
    if say and _named(say, knobs) <= changed:
        lines.append(say.strip())
    rows = [r for r in (out.get('knobs') or [])]
    by_label = {k['label']: str(k['parameter']) for k in knobs}
    # By control identity: several controls can share a label (Diva has two "Decay"s).
    shown = [r for r in rows if str(r.get('parameter', by_label.get(r['label']))) in changed]
    if shown:
        lines.append('; '.join('%s: %s to %s' % (r['label'], r['was'], r['now']) for r in shown) + '.')
    if result['status'] == 'partial':
        lines.append('Only partly done: %s' % result['failure_reason'])
    if audition:
        lines.insert(0, 'Audition — keep it, reject it, or neither?')
    return '\n'.join(lines) or 'Done.'


# ---- the question ceiling (spec 4.4) ---------------------------------------------------------

def questions_left(state, now=None):
    now = now or time.time()
    s = state.get('engineerSession') or {}
    if now - s.get('at', 0) > SESSION_IDLE:
        s = {'at': now, 'questions': 0}
    s['at'] = now
    state['engineerSession'] = s
    return max(0, QUESTIONS_PER_SESSION - s.get('questions', 0))


def asked(state):
    s = state.setdefault('engineerSession', {'at': time.time(), 'questions': 0})
    s['questions'] = s.get('questions', 0) + 1


# ---- move history (spec 4.6) -----------------------------------------------------------------

def record_move(proposal, result, evidence_id, store=None):
    """Keep a verified (or partial) change, ordered and with magnitudes, for "again / further"."""
    if result['status'] not in ('verified', 'partial') or not evidence_id:
        return None
    import taste_memory
    store = store or taste_memory.MemoryStore()
    seq = max([m['sequence'] for m in store.moves()] or [0]) + 1
    written = {w['control_id'] for w in result['writes']}
    item = {'schema_version': 1, 'request_id': proposal['request_id'], 'target_identity': proposal['target_identity'],
            'start_revision': proposal['start_revision'], 'cancel_requested': result['cancel_requested'],
            'id': 'move:' + proposal['request_id'], 'sequence': seq, 'evidence_ids': [evidence_id],
            'changes': [ch for ch in proposal['changes'] if ch['control_id'] in written],
            'partial_write': result['partial_write'], 'restore_data': result['restore_data'],
            'verified_result': result, 'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    store.record_move(item)
    return item['id']
