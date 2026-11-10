"""Arrangement automation as a proposal -> audition -> choice workflow.

STABLE INTERFACE (engineer chat, mix_automation and other callers):
- propose(plan, adapter=None) -> proposal. Read-only: resolves the plan against a fresh Set read and describes
  each lane (target, control, affected bars/beats, curve shape, amount, the plan's reasons). No host write.
- audition(plan, adapter=None, alternative=None, request_id='') -> offer. Records the candidate into the
  Arrangement (with `alternative` lanes it is an A/B offer; A is recorded first), reads it back and verifies it,
  then loops the affected span (one bar either side) with locate-then-play, and leaves it waiting for his choice. Nothing is kept until he chooses, unless the persisted autonomous
  mode is on (off by default), in which case a verified single candidate is kept. `execute` = audition.
- choose(choice, adapter=None, offer=None) -> result. choice: 'original', 'candidate' (single) or 'A'/'B'
  (A/B) to hear a side; 'keep' keeps the candidate in the Set; 'revert' / 'neither' put the original back and
  close the offer. Switching sides first stops Scrunch's own audition (Live must be stopped to undo or record;
  if Live is playing for any other reason the choice is refused), then plays the span again with the new side.
  Keep / Revert / Neither stop the audition.
- offer() -> the waiting offer's HUD panel ({'offerId','kind','playing','buttons'}) or None.
- autonomous() / set_autonomous(on): the persisted auto-keep flag (engine.STATE['automationAutonomous']).
- run(spec, ...) dispatches one engineer action (op propose / audition / choose).
The offer lives in audition_pair's store (key 'automation') with its fields: pairId, kind, playing, toggles.

Plan: {"track":"<track id>","trackName":"<its name>","lanes":[
         {"target":{"mixer":"volume"} | {"mixer":"pan"} | {"mixer":"send","send":<0-based>}
                   | {"device":"<device id>","deviceName":"<its name>","parameter":"<exact name>" | "parameterId":"<id>"},
          "unit":"" (the parameter's own host range) | a Live display unit such as "dB", "Hz", "ms", "%",
          "points":[{"beat":<quarter-note beats from the Arrangement start>,"value":<number>,"reason":"<why>"}, ...]}]}
Curves are linear between 2-256 points; a point's reason explains the move into it. `alternative` is a second
lane list for the same targets. Identity comes from ids checked against a fresh read; ambiguity refuses.

Rules this module enforces:
- Exact revert: every target must carry no automation before the write (automation_state 'none'); otherwise
  the write is refused with "exact revert not guaranteed" (Live exposes no way to restore an existing envelope).
  The original is then fully described by each parameter's value with no automation, and reverting is checked
  against exactly that: Live's undo of Scrunch's recording, then automation 'none' and the identical value.
- One waiting offer at a time: Live's undo reaches only Scrunch's latest recording.
- Every write is read back (value at located beats) and verified against the plan.
- Sides are switched by the recorded-pass state (Live's undo / recording again), never by device bypass
  (bypass is not exact: mix actions measured 0.95 dB at 1 kHz with an EQ bypassed, 26 Sep 2026).
Limits of Live 12.3.5 (evidence/automation-envelopes-2026-09-26): no envelope API, so each write is recorded in
real time (Live plays the span from a lead-in with automation arm on; values set about every 100 ms, so a ramp
can land up to about two ticks early or late); refused while Live is playing or any track is armed; breakpoints
cannot be listed; automation Scrunch did not record cannot be cleared or edited.
"""
import json,math,time,uuid
import engine
import audition_pair
import arrangement_automation as aa

MAX_LANES=8          # one recording pass on one track (bridge limit)
MAX_POINTS=256       # per lane (bridge limit)
MAX_SPAN_BEATS=512.0 # one recording pass (bridge limit)
MAX_PER_MESSAGE=3    # automation actions in one engineer message
LEAD_BEATS=4.0       # recording starts this far before the first point (clipped at the Set start)
POLL=0.1
KEY='automation'
CHOICES={'single':('original','candidate','keep','revert','neither'),'ab':('original','A','B','keep','revert','neither')}
LIMITS=('Recorded in real time (Live 12.3.5 has no envelope API): Live plays the span from a lead-in with automation arm on, '
        'values are set about every 100 ms, read-back is the value at located beats (not breakpoints); refused while Live plays or a track is armed.')

# ---- persisted state ----

def autonomous():
    return engine.STATE.get('automationAutonomous') is True

def set_autonomous(on):
    engine.STATE['automationAutonomous']=bool(on);engine.save();return autonomous()

def _pair():
    return audition_pair.get_key(KEY)

def _waiting():
    p=_pair()
    return p if p and not p.get('decided') else None

def offer():
    return audition_pair.offer_panel(_waiting())

# ---- validation (structure only, no host reads) ----

def _text(v,what,limit=200):
    if not isinstance(v,str) or not v.strip() or len(v)>limit:raise ValueError(what+' must be a non-empty string.')
    return v

def _num(v,what):
    if type(v) not in (int,float) or not math.isfinite(v):raise ValueError(what+' must be a finite number.')
    return float(v)

def _target_key(t):
    if not isinstance(t,dict):raise ValueError('Each lane needs a target object.')
    if 'mixer' in t:
        if set(t)=={'mixer'} and t['mixer'] in ('volume','pan'):return ('mixer',t['mixer'])
        if set(t)=={'mixer','send'} and t['mixer']=='send' and type(t['send']) is int and 0<=t['send']<64:return ('send',t['send'])
        raise ValueError('Mixer targets are {"mixer":"volume"}, {"mixer":"pan"} or {"mixer":"send","send":<0-based index>}.')
    if set(t) in ({'device','deviceName','parameter'},{'device','deviceName','parameterId'}):
        for k in t:_text(t[k],'Target '+k)
        return ('device',t['device'],t.get('parameter'),t.get('parameterId'))
    raise ValueError('A device target needs device, deviceName and parameter (or parameterId).')

def _lanes(lanes):
    if not isinstance(lanes,list) or not 1<=len(lanes)<=MAX_LANES:raise ValueError('Give 1 to %d lanes.'%MAX_LANES)
    keys=[];first,last=math.inf,-math.inf
    for lane in lanes:
        if not isinstance(lane,dict) or set(lane)!={'target','unit','points'}:raise ValueError('Each lane has target, unit and points.')
        key=_target_key(lane['target'])
        if key in keys:raise ValueError('A target appears in two lanes.')
        keys.append(key)
        if not isinstance(lane['unit'],str) or len(lane['unit'])>16:raise ValueError('Unit is "" (host range) or a display unit.')
        pts=lane['points']
        if not isinstance(pts,list) or not 2<=len(pts)<=MAX_POINTS:raise ValueError('Give 2 to %d points per lane.'%MAX_POINTS)
        prev=-1.0
        for p in pts:
            if not isinstance(p,dict) or set(p)!={'beat','value','reason'}:raise ValueError('Each point has beat, value and reason.')
            beat=_num(p['beat'],'Point beat');_num(p['value'],'Point value');_text(p['reason'],'Each point\'s reason',300)
            if beat<0 or beat<=prev:raise ValueError('Point beats must be non-negative and strictly increasing.')
            prev=beat
        first,last=min(first,pts[0]['beat']),max(last,pts[-1]['beat'])
    if last-first>MAX_SPAN_BEATS:raise ValueError('One recording pass covers at most %d beats.'%MAX_SPAN_BEATS)
    return keys

def validate(spec):
    """Structural check of one automation action (op propose / audition / choose); raises ValueError."""
    if not isinstance(spec,dict):raise ValueError('Automation action must be a JSON object.')
    op=spec.get('op')
    if op=='choose':
        if set(spec)!={'op','offer','choice'}:raise ValueError('Automation choose takes offer and choice.')
        p=_waiting()
        if not p or spec['offer']!=KEY+':'+p['pairId']:raise ValueError('That automation offer is not waiting for a choice.')
        kind='ab' if 'B' in p else 'single'
        if spec['choice'] not in CHOICES[kind]:raise ValueError('Choose one of '+', '.join(CHOICES[kind])+'.')
        if spec['choice']==p['playing']:raise ValueError('The '+p['playing']+' is already in the Set.')
        return spec
    if op not in ('propose','audition'):raise ValueError('Automation op must be propose, audition or choose.')
    if not {'op','track','trackName','lanes'}<=set(spec)<={'op','track','trackName','lanes','alternative'}:
        raise ValueError('Automation '+op+' needs track, trackName and lanes (and optionally alternative).')
    _text(spec['track'],'Track id');_text(spec['trackName'],'Track name')
    keys=_lanes(spec['lanes'])
    if 'alternative' in spec and _lanes(spec['alternative'])!=keys:raise ValueError('The alternative must automate the same targets in the same order.')
    return spec

def check_plan(specs):
    """Whole-message rules: at most MAX_PER_MESSAGE automation actions, at most one audition, and no choice in the
    same message as an audition (he cannot have chosen what he has not heard)."""
    if len(specs)>MAX_PER_MESSAGE:raise ValueError('At most %d automation actions per message: each write is a real-time recording.'%MAX_PER_MESSAGE)
    ops=[s.get('op') if isinstance(s,dict) else None for s in specs]
    if ops.count('audition')>1:raise ValueError('One automation audition per message: each one waits for his choice.')
    if 'audition' in ops and 'choose' in ops:raise ValueError('A choice cannot be made in the same message as the audition it chooses from.')
    for s in specs:validate(s)

# ---- resolution against a fresh Set read (read-only) ----

def _resolve_target(adapter,track,t):
    if 'mixer' in t:return dict(t)
    d=adapter.device(t['device'],parameters=True)
    if d.get('trackId')!=track:raise ValueError('Device %s is not on track %s. Nothing was recorded.'%(t['deviceName'],track))
    if d.get('name')!=t['deviceName']:raise ValueError('Device %s is now named %r, not %r. Nothing was recorded.'%(t['device'],d.get('name'),t['deviceName']))
    params=d.get('parameters') or []
    if 'parameterId' in t:found=[p for p in params if p.get('id')==t['parameterId']]
    else:found=[p for p in params if t['parameter'] in (p.get('name'),p.get('originalName'))]
    wanted=t.get('parameter') or t.get('parameterId')
    if not found:
        raise ValueError('%s has no parameter %r (it has %s). Nothing was recorded.'%(t['deviceName'],wanted,', '.join(repr(p.get('name')) for p in params[:12])))
    if len(found)>1:
        raise ValueError('%s has %d parameters matching %r (%s); name it by parameterId. Nothing was recorded.'%(t['deviceName'],len(found),wanted,', '.join(p['id'] for p in found)))
    return {'device':d['id'],'parameter':found[0]['id']}

def refuse_if_busy(adapter,allow_playing=False):
    """Recording needs Live stopped and nothing armed (an armed track would record too)."""
    if not allow_playing and adapter.transport().get('playing'):
        raise ValueError('Live is playing. Automation is recorded in real time, so Live must be stopped first. Nothing was recorded.')
    armed=[t.get('name') for t in adapter.read_live_set('tracks').get('tracks',[]) if t.get('isArmed')]
    if armed:raise ValueError('Armed tracks would record too (%s); disarm them first. Nothing was recorded.'%', '.join(armed[:4]))

def _beats_per_bar(signature):
    try:
        n,d=(int(x) for x in str(signature).split('/'))
        return n*4.0/d if n>0 and d>0 else None
    except ValueError:return None

def _resolve_lanes(adapter,track,lanes):
    out=[]
    for lane in lanes:
        target=_resolve_target(adapter,track,lane['target'])
        info=adapter.automation_target(track,target)
        if info.get('enabled') is False:raise ValueError('%r is disabled in Live. Nothing was recorded.'%info.get('name'))
        if info.get('automation')!='none':
            raise ValueError('Exact revert not guaranteed: %r already carries automation (%s), and Live exposes no way to put an existing envelope back exactly. Nothing was recorded.'
                             %(info.get('name'),info.get('automation')))
        values=[float(p['value']) for p in lane['points']]
        if lane['unit']:
            internal=[float(v) for v in aa.internal_values(adapter,track,target,[[v,lane['unit']] for v in values])]
        else:
            lo,hi=float(info['min']),float(info['max']);bad=[v for v in values if not lo<=v<=hi]
            if bad:raise ValueError('%r takes host values %g..%g; %g is outside it. Nothing was recorded.'%(info['name'],lo,hi,bad[0]))
            internal=values
        if info.get('quantized') and any(v!=int(v) for v in internal):raise ValueError('%r is stepped; give whole steps. Nothing was recorded.'%info['name'])
        label=info['name']+('' if 'mixer' in target else ' ('+lane['target']['deviceName']+')')
        out.append({'target':target,'name':info['name'],'id':info['id'],'label':label,'unit':lane['unit'],'range':[info['min'],info['max']],
                    'points':[{'beat':float(p['beat']),'value':float(p['value']),'internal':v,'reason':p['reason']} for p,v in zip(lane['points'],internal)]})
    return out

def prepare(adapter,spec,allow_playing=False):
    """Validate and resolve one action against a fresh read. Read-only; raises before anything is written."""
    validate(spec)
    if spec['op']=='choose':
        if not allow_playing and not _waiting().get('auditioning') and adapter.transport().get('playing'):
            raise ValueError('Live is playing (not Scrunch\'s audition); stop it before switching automation. Nothing was changed.')
        return dict(spec)
    if spec['op']=='audition' and _waiting():
        raise ValueError('An automation offer on %s is still waiting for a choice (Keep, Revert or Neither); Live\'s undo only reaches the latest recording. Nothing was recorded.'%_waiting()['trackName'])
    mix=audition_pair.get_key('mix')          # mix_engineer's offer: its revert may need Live's undo of its own take
    if spec['op']=='audition' and mix and not mix.get('decided'):
        raise ValueError('A mix candidate from the engineer notes is still waiting for a choice (Keep, Revert or Neither); Live\'s undo only reaches the latest recording. Nothing was recorded.')
    track=spec['track']
    rows=[t for t in adapter.session()['tracks'] if t.get('id')==track]
    if len(rows)!=1:raise ValueError('Track %s (%s) is not in the open Set. Nothing was recorded.'%(spec['trackName'],track))
    if rows[0].get('name')!=spec['trackName']:raise ValueError('Track %s is now named %r, not %r. Nothing was recorded.'%(track,rows[0].get('name'),spec['trackName']))
    out={'op':spec['op'],'track':track,'trackName':spec['trackName'],'beatsPerBar':_beats_per_bar(adapter.connect().get('timeSignature')),
         'A':_resolve_lanes(adapter,track,spec['lanes'])}
    if 'alternative' in spec:out['B']=_resolve_lanes(adapter,track,spec['alternative'])
    if spec['op']=='audition':
        refuse_if_busy(adapter,allow_playing)
    return out

# ---- description ----

def _fmt(v):return ('%.3f'%v).rstrip('0').rstrip('.')

def _bar(beat,bpb):return 'bar '+_fmt(beat/bpb+1) if bpb else 'beat '+_fmt(beat)

def _describe(lane,bpb):
    pts=lane['points'];vals=[p['value'] for p in pts];unit=(' '+lane['unit']) if lane['unit'] else ' (host value)'
    moves=['rises' if b>a else 'falls' if b<a else 'holds' for a,b in zip(vals,vals[1:])]
    shape=[m for i,m in enumerate(moves) if i==0 or m!=moves[i-1]]
    return {'target':lane['label'],'control':lane['name'],'from':_bar(pts[0]['beat'],bpb),'to':_bar(pts[-1]['beat'],bpb),
            'beats':[pts[0]['beat'],pts[-1]['beat']],'shape':'%d-point linear curve: %s'%(len(pts),', then '.join(shape)),
            'amount':'%s to %s%s (lowest %s, highest %s)'%(_fmt(vals[0]),_fmt(vals[-1]),unit,_fmt(min(vals)),_fmt(max(vals))),
            'reasons':[{'beat':p['beat'],'reason':p['reason']} for p in pts]}

def _proposal(prep):
    bpb=prep['beatsPerBar']
    out={'track':prep['trackName'],'candidates':{k:[_describe(l,bpb) for l in prep[k]] for k in ('A','B') if k in prep},'limits':LIMITS}
    lines=[]
    for k,rows in out['candidates'].items():
        for r in rows:
            lines.append('%s%s on %s, %s to %s: %s, %s. Why: %s.'%(('Candidate %s: '%k) if 'B' in prep else 'Proposal: ',r['target'],prep['trackName'],r['from'],r['to'],
                         r['shape'],r['amount'],'; '.join('%s: %s'%(_bar(q['beat'],bpb),q['reason']) for q in r['reasons'])))
    out['text']='\n'.join(lines)
    return out

def propose(plan,adapter=None):
    """Read-only proposal of a curve plan (see module docstring)."""
    adapter=adapter or engine.host()
    return _proposal(prepare(adapter,dict(plan,op='propose')))

# ---- recording, exact original, choice ----

def _record(adapter,track,lanes):
    plan=[aa.lane(l['target'],l['name'],[(p['beat'],p['internal']) for p in l['points']],ident=l['id']) for l in lanes]
    started=time.monotonic()
    rec=aa.write(adapter,track,plan,lead=LEAD_BEATS,poll=POLL)
    rec['seconds']=round(time.monotonic()-started,1)
    return rec

def _original(adapter,p):
    """Differences from the stored original (no automation, identical value). Empty = exactly the original."""
    off=[]
    for t in p['original']:
        now=adapter.automation_target(p['track'],t['target'])
        if now.get('automation')!='none' or now.get('value')!=t['value'] or now.get('id')!=t['id']:
            off.append('%s reads %s (%s), was %s (none)'%(t['name'],now.get('display'),now.get('automation'),t['display']))
    return off

def _report(p,side,rec):
    lanes=p[side]['lanes'];bpb=p['beatsPerBar']
    first=min(l['points'][0]['beat'] for l in lanes);end=max(l['points'][-1]['beat'] for l in lanes);start=max(0.0,first-LEAD_BEATS)
    lines=['Recorded %s in one real-time pass: Live played %s to %s (lead-in from %s) with automation arm on, %s s including read-back; loop, punch, start marker and playhead put back, playback stopped.'
           %('candidate '+side if 'B' in p else 'the candidate',_bar(start,bpb),_bar(end,bpb),_bar(start,bpb),_fmt(rec.get('seconds',0)))]
    for l,row in zip(lanes,rec['post']):
        at={s['beat']:s for s in row['samples']}
        checks=[c for c in rec['checks'] if c['id']==l['id']];bad=[c for c in checks if not c['ok']]
        span=abs(l['range'][1]-l['range'][0])
        worst=max((c['error']/span for c in checks if c['error'] is not None and span),default=0.0)
        shown=l['points'] if len(l['points'])<=6 else l['points'][:3]+l['points'][-3:]
        pts='; '.join('%s planned %s, Live reads %s'%(_bar(q['beat'],bpb),_fmt(q['value'])+(' '+l['unit'] if l['unit'] else ''),
                     (at[q['beat']]['display'] or _fmt(at[q['beat']]['value'])) if q['beat'] in at else 'not sampled') for q in shown)
        lines.append('%s: %s. Checked at %d located beats, largest error %.1f%% of range: %s.'
                     %(l['label'],pts,len(checks),worst*100,'verified' if not bad else '%d outside tolerance, NOT verified'%len(bad)))
    if not rec['recorded']:lines.append('Live did not finish the recording (%s); inspect Live.'%rec['status'].get('state'))
    return lines

def _span(p):
    lanes=[l for k in ('candidate','A','B') if k in p for l in p[k]['lanes']]
    return max(0.0,min(l['points'][0]['beat'] for l in lanes)-LEAD_BEATS),max(l['points'][-1]['beat'] for l in lanes)+LEAD_BEATS

def _play(adapter,p):
    """Audition: loop the affected span with whichever side is in the Set (locate, then play)."""
    start,end=_span(p)
    r=adapter.play_span(start,end)
    p['auditioning']=bool(r.get('playing'))
    side={'original':'original','candidate':'candidate'}.get(p['playing'],'candidate '+p['playing'])
    return ('Playing %s to %s in a loop with the %s in the Set; your own loop comes back when playback stops.'%(_bar(start,p['beatsPerBar']),_bar(end,p['beatsPerBar']),side)
            if p['auditioning'] else 'Playback did not start (%s); nothing else changed.'%r)

def _stop_audition(adapter,p):
    """Stop Scrunch's own audition before switching; refuse if Live is playing for another reason."""
    if not adapter.transport().get('playing'):
        p['auditioning']=False;return
    if not p.get('auditioning'):raise ValueError('Live is playing (not Scrunch\'s audition); stop it first. Nothing was changed.')
    if adapter.stop_playback().get('playing'):raise ValueError('Live did not stop; nothing was switched.')
    p['auditioning']=False

def _to_original(adapter,p):
    """Take the candidate in the Set back out with Live's undo; verified against the exact original."""
    out=aa.undo(adapter,p['record'])
    p['record']=None
    off=_original(adapter,p)
    ok=bool(out['restored']) and not off
    return ok,('Original back: Live\'s undo (%s) took out Scrunch\'s recording on %s; every control has no automation and its exact previous value.'%(out.get('liveUndo'),p['trackName'])
               if ok else 'Live\'s undo ran (%s) but %s is not exactly the original: %s. Inspect Live.'%(out.get('liveUndo'),p['trackName'],'; '.join(off) or 'read-back differs'))

def _to_side(adapter,p,side):
    """Record `side` from the exact original (refused if anything moved since, so his edit is never overwritten)."""
    off=_original(adapter,p)
    if off:raise ValueError('%s is no longer exactly the original (%s), so recording could overwrite your edit. Nothing was recorded.'%(p['trackName'],'; '.join(off)))
    refuse_if_busy(adapter)
    rec=_record(adapter,p['track'],p[side]['lanes'])
    p['record']=rec;p[side]['verified']=rec['verified'];p[side]['samples']=rec['post']
    return rec['verified'],'\n'.join(_report(p,side,rec))

def audition(plan,adapter=None,alternative=None,request_id='',allow_playing=False,prepared=None):
    """Record a candidate (or A of an A/B offer) for audition; it waits for his choice. See module docstring."""
    adapter=adapter or engine.host()
    if prepared is None:
        spec=dict(plan,op='audition')
        if alternative is not None:spec['alternative']=alternative
        prepared=prepare(adapter,spec,allow_playing)
    proposal=_proposal(prepared)
    first='A' if 'B' in prepared else 'candidate'
    original=[]
    for l in prepared['A']:
        info=adapter.automation_target(prepared['track'],l['target'])
        original.append({'target':l['target'],'id':l['id'],'name':l['name'],'value':info['value'],'display':info.get('display')})
    p={'pairId':uuid.uuid4().hex,'kind':KEY,'requestId':request_id,'createdAt':time.time(),'track':prepared['track'],'trackName':prepared['trackName'],
       'beatsPerBar':prepared['beatsPerBar'],'original':original,'playing':'original','record':None,'toggles':0,'decided':None,'proposal':proposal}
    if 'B' in prepared:p['A']={'lanes':prepared['A']};p['B']={'lanes':prepared['B']}
    else:p['candidate']={'lanes':prepared['A']}
    with engine.PLAY_LOCK:
        audition_pair.put(KEY,p)          # stored before the write: a failure mid-recording still leaves the original on record
        ok,text=_to_side(adapter,p,first)
        p['playing']=first;audition_pair.put(KEY,p)
    lines=[proposal['text'],text]
    auto=ok and 'B' not in p and autonomous()
    if auto:
        p['decided']='kept (autonomous mode)';audition_pair.put(KEY,p)
        lines.append('Autonomous mode is on, so this verified candidate was kept. Nothing else was changed.')
    else:
        with engine.PLAY_LOCK:
            lines.append(_play(adapter,p));audition_pair.put(KEY,p)
        lines.append('The %s is in the Set for audition; nothing is kept until you choose %s.'%(first if first=='candidate' else 'candidate A',
                     'Original, Candidate, Keep or Revert' if 'B' not in p else 'Original, A, B, Keep or Neither'))
    return {'offerId':KEY+':'+p['pairId'],'side':first,'playing':bool(p.get('auditioning')),'verified':ok,'failed':not ok,'autoKept':auto,
            'proposal':proposal,'panel':offer(),'text':'\n'.join(lines)}

execute=audition

def choose(choice,adapter=None,offer_id=None):
    """His choice on the waiting offer (see module docstring)."""
    adapter=adapter or engine.host()
    p=_waiting()
    if not p:raise ValueError('No automation offer is waiting for a choice.')
    validate({'op':'choose','offer':offer_id or KEY+':'+p['pairId'],'choice':choice})
    if choice=='keep' and p['playing']=='original':raise ValueError('The original is in the Set; pick a candidate to keep, or Neither.')
    with engine.PLAY_LOCK:
        _stop_audition(adapter,p);audition_pair.put(KEY,p)
        if choice=='keep':
            p['decided']='kept';audition_pair.put(KEY,p)
            return {'side':p['playing'],'playing':False,'decided':'kept','verified':True,'failed':False,
                    'text':'Kept %s on %s; playback stopped. It stays in the Set as recorded; Scrunch cannot take it back once Live has newer undo steps.'%(
                        'the candidate' if p['playing']=='candidate' else 'candidate '+p['playing'],p['trackName'])}
        texts=[];ok=True
        if p['playing']!='original':
            ok,text=_to_original(adapter,p);texts.append(text)
            p['playing']='original';p['toggles']+=1;audition_pair.put(KEY,p)
        if choice in ('revert','neither'):
            p['decided']=choice;audition_pair.put(KEY,p)
            texts.append('Offer closed: the original stays; playback stopped.')
        else:
            if choice!='original':
                ok,text=_to_side(adapter,p,choice);texts.append(text)
                p['playing']=choice;p['toggles']+=1
            texts.append(_play(adapter,p));audition_pair.put(KEY,p)
    return {'side':p['playing'],'playing':bool(p.get('auditioning')) and not p['decided'],'decided':p['decided'],'verified':ok,'failed':not ok,'text':'\n'.join(texts)}

def run(spec,adapter=None,request_id='',allow_playing=False,prepared=None):
    """One engineer automation action, already validated with the whole plan and prepared from a fresh read."""
    adapter=adapter or engine.host()
    if spec['op']=='choose':return choose(spec['choice'],adapter,spec['offer'])
    prepared=prepared or prepare(adapter,spec,allow_playing)
    if spec['op']=='propose':
        prop=_proposal(prepared)
        return {'failed':False,'proposal':prop,'text':prop['text']+'\nProposal only: nothing was written. '+LIMITS}
    return audition(None,adapter,request_id=request_id,prepared=prepared)

def context():
    """What the planner may see: the waiting offer (id, sides, which one is in the Set) and the autonomous flag."""
    p=_waiting()
    return {'autonomousMode':autonomous(),'waitingOffer':None if not p else {
        'offerId':KEY+':'+p['pairId'],'track':p['trackName'],'playing':p['playing'],
        'choices':list(CHOICES['ab' if 'B' in p else 'single']),'proposal':p['proposal']['candidates']}}
