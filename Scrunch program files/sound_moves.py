"""The mechanics of a sound change, once the AI engineer has decided it.

There is no move table here and no word list: which controls to move, which way and how far is
decided by the AI for the patch in front of it (see sound_language). This module only turns that
decision into verified writes that can be steered back to where they started.

A change records the values it started from, so revert and "turn it around" recompute from the
origin instead of drifting. `amount` is 0..1 of the way to the AI's targets.
"""

REACH=1.0         # amount 1.0 lands exactly on the targets the engineer chose
MAX_LEVERS=128


def _norm(knob):
    span=(knob['max']-knob['min']) or 1
    return (knob['value']-knob['min'])/span


def target_plan(targets,name='change'):
    """A change from the engineer's chosen targets: [{'knob': knob, 'to': 0..1 of its range, 'why': str}]."""
    steps=[]
    for t in targets[:MAX_LEVERS]:
        k=t['knob'];span=k['max']-k['min']
        if span<=0:continue
        goal=k['min']+max(0.,min(1.,float(t['to'])))*span
        if k.get('stepped'):goal=round(goal)
        delta=goal-k['value']
        if abs(delta)<1e-6:continue
        steps.append({'parameter':k['parameter'],'name':k['name'],'setting':k['setting'],'label':k['label'],
                      'origin':k['value'],'direction':1 if delta>0 else -1,'travel':abs(delta)/REACH,
                      'why':str(t.get('why') or '')[:160],'display':k.get('display'),'stepped':bool(k.get('stepped'))})
    return {'move':str(name)[:40],'amount':1.,'steps':steps}


def writes(move,amount=None,knobs=None):
    """Knob writes for a change at an amount, always computed from the origin values, never from where it is now."""
    amount=move['amount'] if amount is None else max(0.,min(2.,float(amount)))   # up to twice as far; each control stays in range
    current={k['parameter']:k for k in (knobs or [])}
    out=[]
    for s in move['steps']:
        target=s['origin']+s['direction']*s['travel']*REACH*amount
        if s.get('stepped'):target=round(target)
        knob=current.get(s['parameter'])
        if knob:target=min(knob['max'],max(knob['min'],target))
        w={'id':s['parameter'],'name':s['name'],'value':round(target,6)}
        if knob:w['expect_value']=knob['value']
        out.append(w)
    return out


def explain(move,applied=None):
    """One line per control: what moved, from what to what, and the engineer's reason."""
    by={p.get('id'):p for p in (applied or [])}
    lines=[]
    for s in move['steps']:
        after=by.get(s['parameter'])
        lines.append('%s %s%s: %s'%(s['label'],s['display'] or round(s['origin'],3),
                                    ' → '+str(after.get('display')) if after else '',s['why']))
    return lines
