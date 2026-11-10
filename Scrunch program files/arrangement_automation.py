"""Scrunch-owned arrangement automation: write, read back and take back envelopes.

DAW specifics stay behind the adapter methods automation_target, record_automation,
automation_take_status, stop_automation_take, sample_automation and undo_automation_take
(implemented by daw_remote_script over the ScrunchBridge Remote Script).

What Live 12.3.5 allows (probed live on 26 Sep 2026, evidence/automation-envelopes-2026-09-26):
- There is no track arrangement envelope in its Python API. Clip envelopes exist only for Session
  clips ("Not a session clip or parameter belongs to another track." on Arrangement clips), and a
  Session clip's envelope does not become track automation when duplicated to the Arrangement.
- So automation is written by recording it: Arrangement record with automation arm, the bridge
  setting the parameter inside a gesture on every display tick from Live's own song position.
- Read back = the parameter's value with the playhead located at each beat (stopped transport).
  Breakpoint lists, curve shapes and exact breakpoint times are not readable.
- Take back = Live's undo of the one recording, refused unless every lane still reads exactly as
  recorded; the result must read exactly like the samples taken before the write.
- Automation that Scrunch did not record cannot be deleted through the API.

Times are quarter-note beats from the Arrangement start; values are the parameter's internal
host values within its own min..max (use `internal_values` to convert display units like dB).
"""
import math,time

TICK_SECONDS=0.1   # Live calls a Remote Script's update_display about every 100 ms
MAX_SAMPLES=64
MAX_ERROR_FRACTION_OF_MOVE=0.05  # Engineering reproduction tolerance, not musical approval.

class AutomationError(ValueError):
    """The automation request was refused or could not be verified. Message says whether Live changed."""

def lane(target,name,points,ident=None):
    """One parameter's plan: target {'mixer':'volume'|'pan'} / {'mixer':'send','send':i} / {'device':id,'parameter':id}."""
    out={'target':dict(target),'name':name,'points':[[float(b),float(v)] for b,v in points]}
    if ident is not None:out['id']=ident
    return out

def internal_values(adapter,track,target,displays):
    """Internal values whose Live display reads as each [number, unit], e.g. [[-12,'dB']]."""
    return adapter.automation_target(track,target,displays=[list(d) for d in displays])['internal']

def plan_value(points,beat):
    if beat<=points[0][0]:return points[0][1]
    for (b0,v0),(b1,v1) in zip(points,points[1:]):
        if beat<=b1:return v0+(v1-v0)*(beat-b0)/(b1-b0)
    return points[-1][1]

def check_beats(lanes,extra=()):
    """Never drop requested points. Spend remaining sample budget on segment interiors.

    MAX_SAMPLES is a bridge request limit, not permission to omit a curve's corners.
    Curves with more mandatory points are read in batches by _samples.
    """
    mandatory=set(float(b) for b in extra);interiors=set()
    for l in lanes:
        pts=l['points']
        mandatory.update(float(b) for b,_ in pts)
        for (b0,_),(b1,_) in zip(pts,pts[1:]):
            for f in (.25,.5,.75):interiors.add(round(b0+(b1-b0)*f,6))
        mandatory.add(float(pts[-1][0])+1.0)
    interior=sorted(interiors-mandatory);budget=max(0,MAX_SAMPLES-len(mandatory))
    if len(interior)>budget:
        interior=[interior[int(i*len(interior)/budget)] for i in range(budget)] if budget else []
    return sorted(mandatory|set(interior))

def _tolerance(points,beat,tempo,span):
    """Allowed read-back error at `beat`: two display ticks of the steepest nearby slope, plus 0.2% of range.

    Recording is stamped on Live's clock but only once per tick, so a ramp can land up to a tick early or late.
    """
    window=2*TICK_SECONDS*tempo/60.0
    slopes=[abs((v1-v0)/(b1-b0)) for (b0,v0),(b1,v1) in zip(points,points[1:]) if b1>=beat-window and b0<=beat+window]
    return max(slopes or [0.0])*window+abs(span)*0.002

def _samples(adapter,track,lanes,beats):
    rows={}
    for start in range(0,len(beats),MAX_SAMPLES):
        r=adapter.sample_automation(track,[l['target'] for l in lanes],beats[start:start+MAX_SAMPLES])
        if not r.get('playheadRestored'):raise AutomationError('Sampling could not put the playhead back; inspect Live.')
        for row in r['targets']:
            if row['id'] not in rows:rows[row['id']]=dict(row,samples=[])
            rows[row['id']]['samples'].extend(row['samples'])
    return list(rows.values())

def _same(a,b):
    """Exact sameness of two sample sets (value within float noise, same automation state)."""
    if not a or not b:return False
    for ra,rb in zip(a,b):
        if ra['id']!=rb['id']:return False
        if len(ra['samples'])!=len(rb['samples']):return False
        for sa,sb in zip(ra['samples'],rb['samples']):
            if sa['beat']!=sb['beat'] or sa['automation']!=sb['automation']:return False
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (sa['value'],sb['value'])):return False
            if abs(sa['value']-sb['value'])>1e-6:return False
    return len(a)==len(b)

def samples_complete(rows,ids,beats):
    if len(rows)!=len(ids) or sorted(r.get('id','') for r in rows)!=sorted(ids):return False
    for row in rows:
        samples=row.get('samples',[])
        if sorted(s.get('beat',-1) for s in samples)!=sorted(beats):return False
        for s in samples:
            v=s.get('value');position=s.get('playhead',s['beat'])
            if not isinstance(v,(int,float)) or not math.isfinite(v):return False
            if not isinstance(position,(int,float)) or not math.isfinite(position) or abs(position-s['beat'])>1e-4:return False
    return True

def verify_samples(lanes,before,post,beats,tempo):
    """Account for every requested sample and bound error by the requested movement.

    The former slope allowance alone could exceed the entire move at a sharp corner.
    Retain that legacy result for diagnosis, but it cannot certify reproduction.
    A 0.001% host-range floor accommodates floating point noise on constant lanes.
    """
    checks=[];worst_range=0.0;worst_move=0.0
    complete=samples_complete(post,[l['id'] for l in lanes],beats)
    for l,b in zip(lanes,before):
        span=abs(b['max']-b['min']);move=max(v for _,v in l['points'])-min(v for _,v in l['points'])
        cap=max(span*0.00001,move*MAX_ERROR_FRACTION_OF_MOVE)
        rows=[r for r in post if r.get('id')==l['id']]
        samples=rows[0].get('samples',[]) if len(rows)==1 else []
        complete=complete and sorted(s.get('beat',-1) for s in samples)==sorted(beats)
        for beat in beats:
            matches=[s for s in samples if s.get('beat')==beat]
            s=matches[0] if len(matches)==1 else {}
            want=plan_value(l['points'],beat);legacy=_tolerance(l['points'],beat,tempo,span)
            value=s.get('value');finite=isinstance(value,(int,float)) and math.isfinite(value)
            err=abs(value-want) if finite else None
            automated=s.get('automation')=='playing'
            tol=min(legacy,cap)
            if err is not None:
                worst_range=max(worst_range,err/span if span else err)
                if move:worst_move=max(worst_move,err/move)
            checks.append({'id':l['id'],'name':l['name'],'beat':beat,'planned':want,'read':value,
                           'display':s.get('display'),'automation':s.get('automation'),'error':err,
                           'tolerance':tol,'legacyTolerance':legacy,
                           'legacyOk':err is not None and err<=legacy and automated,
                           'ok':err is not None and err<=tol and automated})
    complete=complete and len(before)==len(lanes)
    return {'checks':checks,'samplesComplete':complete,'maxErrorFractionOfRange':worst_range,
            'maxErrorFractionOfRequestedMove':worst_move,
            'legacyTolerancePassed':complete and bool(checks) and all(c['legacyOk'] for c in checks),
            'reproductionPassed':complete and bool(checks) and all(c['ok'] for c in checks),
            'verificationMethod':'All requested points; error capped at 5% of requested movement, with 0.001% host-range numerical floor. Located values only, not exact breakpoint times or musical acceptance.'}

def recording_limits(tempo=None):
    return {'verificationVersion':'2026-10-03.2','method':'Live display-tick recording','nominalTickSeconds':TICK_SECONDS,
            'nominalTickBeats':TICK_SECONDS*float(tempo)/60. if tempo is not None else None,
            'exactBreakpointTiming':False,'maxErrorFractionOfRequestedMove':MAX_ERROR_FRACTION_OF_MOVE,
            'guidance':'A change shorter than a display tick may miss its boundary. Choose a musically appropriate earlier transition and hold the endpoint through the boundary; do not silently move supplied points. Read back all requested points. Verified values do not prove exact breakpoint times.'}

def reads_as(adapter,track,lanes,beats,samples):
    """True only if every lane still reads exactly like `samples` (same beats, values and automation state)."""
    return _same(_samples(adapter,track,lanes,beats),samples)

def read(adapter,track,targets,beats):
    """Envelope read back: each target's value, display and automation state with the playhead at each beat."""
    return _samples(adapter,track,[{'target':t} for t in targets],list(beats))

def write(adapter,track,lanes,*,lead=4.0,poll=0.1,sleep=time.sleep,clock=time.monotonic):
    """Record `lanes` on `track`, then verify the read-back against the plan.

    Identity: every target is read immediately before writing and must still carry the expected name
    (and id when given). The record returned is what `undo` needs.
    """
    if not lanes:raise AutomationError('Nothing to write.')
    tempo=float(adapter.connect()['tempo'])
    lanes=[dict(l,target=dict(l['target']),points=[list(p) for p in l['points']]) for l in lanes]
    before=[]
    for l in lanes:
        t=adapter.automation_target(track,l['target'])
        if t.get('name')!=l['name'] or ('id' in l and t.get('id')!=l['id']):
            raise AutomationError('Target changed: %r is now %r (%s). Nothing was changed.'%(l['name'],t.get('name'),t.get('id')))
        l['id']=t['id'];before.append(t)
    beats=check_beats(lanes)
    pre=_samples(adapter,track,lanes,beats)
    if not samples_complete(pre,[l['id'] for l in lanes],beats):
        raise AutomationError('The original automation samples are incomplete or misplaced. Nothing was written.')
    started=adapter.record_automation(track,[{'target':l['target'],'name':l['name'],'id':l['id'],'points':l['points']} for l in lanes],lead=lead)
    deadline=clock()+float(started['expectedSeconds'])*2+15
    while True:
        status=adapter.automation_take_status()
        if not status.get('running'):break
        if clock()>deadline:
            adapter.stop_automation_take()
            raise AutomationError('Recording overran; it was stopped. Part of the automation may be written: inspect Live.')
        sleep(poll)
    if status.get('take')!=started['take']:raise AutomationError('Live reports a different recording; inspect Live.')
    post=_samples(adapter,track,lanes,beats)
    quality=verify_samples(lanes,before,post,beats,tempo)
    record={'take':started['take'],'trackId':track,'tempo':tempo,'lanes':lanes,'before':before,'beats':beats,'pre':pre,
            'post':post,'status':status,**quality,
            'recorded':status.get('state')=='finished','verified':status.get('state')=='finished' and not status.get('error') and quality['reproductionPassed'],
            'method':'Arrangement recording on Live\'s clock; read back by locating the playhead'}
    return record

def undo(adapter,record):
    """Take a recording back out with Live's undo; verified only if every sample reads exactly as before the write."""
    now=_samples(adapter,record['trackId'],record['lanes'],record['beats'])
    if not _same(now,record['post']):
        raise AutomationError('The automation has changed since Scrunch recorded it, so Live\'s undo could remove someone else\'s edit. Nothing was undone.')
    result=adapter.undo_automation_take(record['take'])
    after=_samples(adapter,record['trackId'],record['lanes'],record['beats'])
    restored=_same(after,record['pre'])
    return {'take':record['take'],'liveUndo':result.get('liveUndo'),'after':after,'restored':restored,
            'tracksUnchanged':result.get('tracksBefore')==result.get('tracksAfter')}
