"""Runs reference_refine against the synth: each try is written, read back and measured against his reference
passage; the best MEASURED setting is kept through one verified change from where the request started.

Before every write:
Scrunch still on for this request (kill switch and generation), and every control of the change still holds
what Scrunch last wrote (so nothing he touched is overwritten). Every way out leaves the synth and the records
agreeing: the best measured setting kept as one change, or the engineer's own change back as it was, or (when
neither can be written safely) the live values stated and not recorded as a change.
"""
import copy
import math
import re

import action_layer
import reference_refine
import reference_target
import warm_gp_engine

LEVEL = re.compile(r"\b(volume|output|level|gain|master)\b", re.I)   # the measure is level-matched: invisible
LOST_DB = 30.                  # a band a try pushed below the measuring floor counts this far off: never matched
CAPTURE_BUDGET_S = 24.         # at most this much extra listening per request (every capture holds Live)


def enabled():
    """Off unless switched on: in code (tests) or by him on the dashboard (state referenceRefine)."""
    if reference_refine.REFINE_ENABLED:
        return True
    try:
        import engine
        return engine.STATE.get('referenceRefine') is True
    except Exception:
        return False


def _norm(v, k):
    return (float(v) - k['min']) / (k['max'] - k['min'])


def _native(z, k):
    return round(k['min'] + max(0., min(1., float(z))) * (k['max'] - k['min']), 6)


def search_tries(passage):
    """Search captures allowed for a passage of this length. The confirming listen before 'matched' comes out of
    the same allowance, so it is reserved first (Astra's audit: 4 x 6 s + a 6 s confirm was 30 s against 24 s)."""
    return min(reference_refine.MAX_TRIES, int(CAPTURE_BUDGET_S // max(passage, 1.)) - 1)


def _moved(knobs, expected):
    """Labels of changed controls that no longer hold what Scrunch last wrote (he touched them)."""
    by = {k['parameter']: k for k in knobs}
    return [by[p]['label'] for p, v in expected.items()
            if p in by and abs(by[p]['value'] - v) > action_layer._tol(by[p])]


def _errors(m, bands):
    """Band errors for one try. With the full comparison (measure()['allBands']): all 11 bands, unrounded, each
    side clamped at the -80 dBFS floor, one fixed set for every try, so a match rests on every band and the
    search is never pushed around by bands appearing or disappearing (Brooklyn Rose Part 5, verified: this
    closes all 7 false claims). Without it (older measurements): the reliable bands, with any band the
    engineer's change was measured in counted LOST_DB off when missing."""
    if 'allBands' in m:
        # Exactly the 11 named bands, each once and finite; anything else can never be called a match.
        import reference_dsp
        lost = LOST_DB / reference_refine.TOLERANCE_DB
        seen = {}
        for b in m['allBands'] or []:
            d = b.get('differenceDb')
            ok = isinstance(d, (int, float)) and not isinstance(d, bool) and math.isfinite(d)
            seen[b.get('band')] = lost if b.get('band') in seen or not ok else d / reference_refine.TOLERANCE_DB
        return {name: seen.get(name, lost) for name, _, _ in reference_dsp.BANDS} | \
            {n: lost for n in seen if n not in {name for name, _, _ in reference_dsp.BANDS}}
    got = reference_refine.residuals(m)
    out = {b: got.get(b, LOST_DB / reference_refine.TOLERANCE_DB) for b in bands}
    out.update(got)
    return out


def _loud(m):
    """How many bands are loud enough to compare (above the floor on at least one side), of how many."""
    every = m.get('allBands') or []
    return sum(1 for b in every if b.get('loud')), len(every)


def refine(c, ref, ref_before, ref_after, proposal, decision, text, rid, generation, evidence_dir, measure=None):
    """After a verified change toward the reference. Returns None when it did nothing, else a dict:
    proposal/result/out (result None: the engineer's first change stands), summary, measure (of what is live),
    said, and records_stale=True when what is live could not be put in a recorded state."""
    measure = measure or reference_target.measure
    passage = float((ref or {}).get('durationSeconds') or 6.)
    budget = search_tries(passage)
    if budget < 1:
        return _first_stands(proposal, {'status': 'not refined', 'tries': 0}, ref_after,
                             'Not refined by measuring: the reference passage is %g s; choose 8 s or less for that.' % passage)
    knobs = {k['parameter']: k for k in c.knobs_view()}
    changed = [ch for ch in proposal['changes'] if ch['control_id'] in knobs]
    refinable = [ch for ch in changed if not knobs[ch['control_id']].get('stepped')
                 and knobs[ch['control_id']]['max'] > knobs[ch['control_id']]['min']
                 and not LEVEL.search('%s %s' % (knobs[ch['control_id']].get('label', ''), knobs[ch['control_id']].get('name', '')))]
    ids = reference_refine.choose_controls(refinable)
    if not ids:
        return None
    by = {ch['control_id']: ch for ch in changed}
    k = {p: knobs[p] for p in ids}
    bands = sorted({b['band'] for b in ref_after['bands']} | {b['band'] for b in ref_before['bands']})
    start = {p: _norm(by[p]['after'], k[p]) for p in ids}
    # The sound before his request is usually several windows away and bends a local model out of shape
    # (Part 5: dropping it improved the prediction in all 19 missed cases); it only sets the probe direction.
    warm = None
    if warm_gp_engine.wanted():            # Brooklyn Rose: the warm GP on a map of this patch (off by default)
        try:
            warm = warm_gp_engine.build(c, ids, k, start, {p: _norm(by[p]['before'], k[p]) for p in ids}, ref_after,
                                        ref_before, budget, stop=lambda: not action_layer.cancel_clear(rid, generation)[0])
        except Exception:
            warm = None                    # no map here: the Refiner searches as before
    if warm is not None:
        r = warm
    else:
        r = reference_refine.Refiner(ids, start, {p: 0. for p in ids}, {p: 1. for p in ids}, max_tries=budget,
                                     direction={p: by[p]['after'] - by[p]['before'] for p in ids})
        r.tell(start, _errors(ref_after, bands), charged=False)
    measured = {tuple(sorted(start.items())): ref_after}
    expected = {ch['control_id']: ch['after'] for ch in changed}       # every control of the change, not only refined
    device = c.device or c._device()
    stopped = None
    import sound_chat
    sound_chat.forget_ahead()                  # controls are about to move
    while stopped is None:
        x = r.ask()
        if x is None:
            break
        clear, why = action_layer.cancel_clear(rid, generation)
        if not clear:
            stopped = why or 'Scrunch was switched off'
            break
        if _moved(c.knobs_view(), expected):
            break                              # handled below: his setting stays
        writes = [{'id': p, 'name': k[p]['name'], 'value': _native(x[p], k[p]), 'expect_value': expected[p]} for p in ids]
        try:
            applied = c.a.set_device_parameters_together(device, writes)
            back = {a.get('id'): a.get('value') for a in applied or []}
            confirmed = all(back.get(w['id']) is not None and abs(back[w['id']] - w['value']) <= action_layer._tol(k[w['id']])
                            for w in writes)
        except Exception as exc:
            back, confirmed = {}, False
            stopped = 'a try could not be confirmed (%s)' % str(exc)[:100]
        if not confirmed:
            live = {kk['parameter']: kk['value'] for kk in c.knobs_view()}
            expected.update({p: live[p] for p in ids if p in live})        # Scrunch's own uncertain write, not his
            stopped = stopped or 'a try did not read back as written'
            break
        expected.update({p: back[p] for p in ids})
        try:
            m = measure(c, ref, evidence_dir)
        except Exception as exc:
            stopped = 'a try could not be measured (%s)' % str(exc)[:100]
            break
        try:
            r.tell(x, m if warm is not None else _errors(m, bands))
        except ValueError as exc:
            stopped = 'a try could not be used (%s)' % str(exc)[:100]
            break
        measured[tuple(sorted(x.items()))] = m
    summary = r.summary()
    if warm is not None:
        summary = dict(summary, map=warm.map)
    check = warm.check if warm is not None else None
    if stopped:
        summary = dict(summary, stopped=stopped)
    his = _moved(c.knobs_view(), expected)
    his_ids = {p for p in expected if _moved(c.knobs_view(), {p: expected[p]})}
    # a try of Scrunch's still live on a refined control he has not taken over
    try_in_place = any(p not in his_ids and abs(expected[p] - by[p]['after']) > action_layer._tol(k[p]) for p in ids)
    if his:
        summary = dict(summary, status='stopped', stopped='you moved %s, so I stopped and left your setting' % ', '.join(his[:3]))
        return _stale(c, changed, summary) if try_in_place else _first_stands(proposal, summary, ref_after, english(summary))
    best = summary.get('setting') or start
    if stopped or all(abs(best[p] - start[p]) < 1e-9 for p in ids):
        # Not keeping a try: the engineer's own change goes back exactly (when a try is still in place).
        if try_in_place and not _put_back(c, changed, expected, rid, generation, device):
            return _stale(c, changed, dict(summary, status='stopped'))
        s = dict(summary, status='stopped') if stopped else dict(summary, kept='first')
        live_measure = ref_after
        if not stopped:
            s, live_measure = _confirm(c, ref, evidence_dir, s, bands, measure, ref_after, check)
        return _first_stands(proposal, s, live_measure, english(s))
    # Keep the best measured setting as ONE verified change from where the request started, so undo, More/Less
    # and move history all see the whole change from his starting point.
    clear, why = action_layer.cancel_clear(rid, generation)
    if not clear:
        return _stale(c, changed, dict(summary, status='stopped', stopped=why or 'Scrunch was switched off'))
    first_move, first_verified = copy.deepcopy(c.move), copy.deepcopy(getattr(c, 'last_verified', None))
    try:
        c._apply(0.)                           # back to his starting point, through the engineer's own change
    except Exception as exc:
        return _stale(c, changed, dict(summary, status='stopped', stopped='could not return to the starting point (%s)'
                                       % str(exc)[:100]))
    c.move = None
    fresh = {kk['parameter']: kk for kk in c.knobs_view()}
    targets = [{'knob': fresh[ch['control_id']], 'to': best[ch['control_id']] if ch['control_id'] in best
                else _norm(ch['after'], fresh[ch['control_id']]),
                'why': 'closest measured to the reference' if ch['control_id'] in best else 'the engineer\'s change'}
               for ch in changed if ch['control_id'] in fresh]
    try:
        proposal2, result2, out2 = action_layer.execute(c, dict(decision, revertFirst=False, targets=targets), text, rid,
                                                        generation)
    except Exception as exc:
        proposal2, result2, out2 = None, {'status': 'failed', 'failure_reason': str(exc)[:100]}, None
    if result2['status'] == 'verified':
        best_measure = measured.get(tuple(sorted(best.items())), ref_after)
        summary, best_measure = _confirm(c, ref, evidence_dir, summary, bands, measure, best_measure, check)
        return {'proposal': proposal2, 'result': result2, 'out': out2, 'summary': summary, 'measure': best_measure,
                'said': english(summary)}
    # The keep failed: the engineer's own change goes back, with the records that describe it.
    now = {kk['parameter']: kk['value'] for kk in c.knobs_view()}
    reason = result2.get('failure_reason') or result2['status']
    if not _put_back(c, changed, now, rid, generation, device, move=first_move):
        return _stale(c, changed, dict(summary, status='stopped', stopped='the closest setting could not be kept (%s)' % reason))
    c.last_verified = first_verified
    s = dict(summary, status='stopped', stopped='the closest setting could not be kept (%s); your first change is back' % reason)
    return _first_stands(proposal, s, ref_after, english(s))


def _put_back(c, changed, expected, rid, generation, device, move=None):
    """The engineer's change exactly as it was, if Scrunch is on and he has touched nothing. True when done."""
    clear, _ = action_layer.cancel_clear(rid, generation)
    if not clear or _moved(c.knobs_view(), expected):
        return False
    now = {kk['parameter']: kk for kk in c.knobs_view()}
    writes = [{'id': ch['control_id'], 'name': now[ch['control_id']]['name'], 'value': ch['after'],
               'expect_value': expected.get(ch['control_id'], now[ch['control_id']]['value'])}   # Scrunch's own last write
              for ch in changed if ch['control_id'] in now]
    try:
        back = c.a.set_device_parameters_together(device, writes)
    except Exception:
        return False
    got = {a.get('id'): a.get('value') for a in back or []}
    if any(got.get(w['id']) is None or abs(got[w['id']] - w['value']) > action_layer._tol(now[w['id']]) for w in writes):
        return False
    if move is not None:
        c.move = move                          # a copy taken before anything moved: amount and trajectory intact
    if c.move:
        c.move['applied'] = back
    return True


def _confirm(c, ref, evidence_dir, summary, bands, measure, fallback, check=None):
    """Before saying 'matched': listen to what is live once more; the claim stands only if it matches again.
    Live playback varies, so the best of several captures can be a lucky one. Returns (summary, measure)."""
    if summary.get('status') != 'matched':
        return summary, fallback
    try:
        m = measure(c, ref, evidence_dir)
    except Exception as exc:
        return dict(summary, status='closest with these controls',
                    confirm='could not listen again to confirm (%s)' % str(exc)[:80]), fallback
    if not (check(m) if check else reference_refine.matched(_errors(m, bands))):
        return dict(summary, status='closest with these controls', confirm='a second listen did not confirm the match'), m
    return dict(summary, loud=list(_loud(m))), m


def _first_stands(proposal, summary, measure, said):
    return {'proposal': proposal, 'result': None, 'out': None, 'summary': summary, 'measure': measure, 'said': said}


def _stale(c, changed, summary):
    """Something other than a recorded change is live and cannot safely be written back: say exactly what is
    live, and drop the records that would describe something else (Undo and More/Less would act on them)."""
    now = {kk['parameter']: kk for kk in c.knobs_view()}
    live = ', '.join('%s %s' % (now[ch['control_id']]['label'], now[ch['control_id']].get('display') or
                                round(now[ch['control_id']]['value'], 4)) for ch in changed if ch['control_id'] in now)
    c.move, c.last_verified = None, None
    summary = dict(summary, status='stopped', live=live)
    said = ('Refining toward the reference stopped: %s. Now live: %s. This is not recorded as a change, so Undo '
            'will not reach it.' % (summary.get('stopped') or 'it could not finish', live))
    return {'proposal': None, 'result': None, 'out': None, 'summary': summary, 'measure': None, 'said': said,
            'records_stale': True}


def english(summary):
    tries = summary.get('tries', 0)
    if summary.get('status') == 'stopped':
        return 'Refining toward the reference stopped: %s.' % summary.get('stopped')
    if summary.get('status') == 'not refined':
        return ''
    loud = summary.get('loud')
    if summary.get('status') == 'matched':
        head = 'Matched the reference within 1 dB' + (
            (' in all %d bands loud enough to compare (the other %d are below -80 dB on both sides)' % (loud[0], loud[1] - loud[0])
             if loud[1] > loud[0] else ' in all %d bands' % loud[1]) if loud and loud[1] else ' in every band it could compare')
    else:
        head = 'This is as close as these controls got to the reference'
        if summary.get('confirm'):
            head += ' (%s)' % summary['confirm']
    how = ' after %d measured tr%s' % (tries, 'y' if tries == 1 else 'ies') if tries else ''
    kept = '; your first change was already the closest, so it stays' if summary.get('kept') == 'first' and tries else ''
    return head + how + kept + '.'
