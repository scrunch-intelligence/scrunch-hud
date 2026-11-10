"""Mix actions: real, verified mix changes on a track, measured before and after, with exact undo and A/B.

Scrunch-owned and DAW-neutral. Actions are in musical units; the host adapter (`daw_mix.LiveMix` for
Live 12) turns them into the DAW's own values and devices. This module never names a Live device or
parameter.

Actions (all need 'track', the host's track id):
  {'kind': 'gain', 'db': -9.0} or {'kind': 'gain', 'byDb': -2.0}
  {'kind': 'pan', 'pan': -0.3}                                    # -1 hard left .. +1 hard right
  {'kind': 'eq', 'shape': 'bell'|'low-shelf'|'high-shelf'|'high-pass'|'low-pass'|'notch',
                 'hz': 1000, 'db': -3.0, 'q': 1.4, 'slope': 12|48}  # db for bell/shelves; slope for pass filters
  {'kind': 'compressor', 'thresholdDb', 'ratio', 'attackMs', 'releaseMs' (or 'auto'), 'makeupDb', 'mixPercent',
                 'sidechain': {'track': <source track id>, 'tap': 'Pre FX'|'Post FX'|'Post Mixer'}}
  {'kind': 'glue', same numbers as compressor, no sidechain}      # typically a group or the master
  {'kind': 'utility', 'widthPercent': 120, 'bassMonoHz': 120 | None}

Every value is checked against the host's real range (the host refuses, with its range, anything
outside it), written, and read back from the host's own display. `apply` returns a record holding
the complete before and after state; `undo` restores the before state exactly - removing any
device it inserted - and proves it by comparing a fresh read with the before snapshot. `ab` switches
between the original (A) and the change (B) with the same compare-and-set rule: nothing is written
over an edit made since.

Measuring is separate from acting, and needs audio: `measure` on a capture, `compare` of two, and
`report` of the change that matters for the action. `propose` turns a Mix Doctor finding into an
action by solving for the setting from measured audio, and predicts its effect by applying a
standard filter/dynamics model of the move to that audio (labelled as a model; the Live result is
measured afterwards, never assumed).
"""
import copy,json,math,os,re,time,uuid
from pathlib import Path
import numpy as np

VERSION='mix-actions-1'
ROOT=Path(__file__).resolve().parent
JOURNAL=ROOT/'data'/'mix-actions'
OWNED='Scrunch Mix'
KINDS=('gain','pan','eq','compressor','glue','utility')
SHAPES=('bell','low-shelf','high-shelf','high-pass','low-pass','notch')
DEVICE_KINDS=('eq','compressor','glue','utility')
TAPS=('Pre FX','Post FX','Post Mixer')

# ---------------------------------------------------------------- validation
def _num(x,what):
    if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x):raise ValueError('%s must be a finite number.'%what)
    return float(x)

def validate(action):
    """A normalised copy of `action`; raises ValueError on anything malformed. Ranges are the host's job."""
    if not isinstance(action,dict):raise ValueError('An action is an object.')
    a=copy.deepcopy(action);k=a.get('kind')
    if k not in KINDS:raise ValueError('Action kind must be one of %s.'%', '.join(KINDS))
    if not isinstance(a.get('track'),str) or not a['track']:raise ValueError('Name the target track by its id.')
    allowed={'kind','track','reuse','label'}
    if k=='gain':
        if ('db' in a)==('byDb' in a):raise ValueError('Gain takes exactly one of db or byDb.')
        for f in ('db','byDb'):
            if f in a:a[f]=_num(a[f],f)
        allowed|={'db','byDb'}
    elif k=='pan':
        a['pan']=_num(a.get('pan'),'pan')
        if not -1<=a['pan']<=1:raise ValueError('Pan is -1 (hard left) .. +1 (hard right).')
        allowed|={'pan'}
    elif k=='eq':
        if a.get('shape') not in SHAPES:raise ValueError('EQ shape must be one of %s.'%', '.join(SHAPES))
        a['hz']=_num(a.get('hz'),'hz')
        if a['shape'] in ('bell','low-shelf','high-shelf'):a['db']=_num(a.get('db'),'db')
        elif 'db' in a:raise ValueError('A %s has no gain.'%a['shape'])
        if 'q' in a:a['q']=_num(a['q'],'q')
        if a['shape'] in ('high-pass','low-pass'):
            a['slope']=int(a.get('slope',48))
            if a['slope'] not in (12,48):raise ValueError('Pass-filter slope is 12 or 48 dB/octave.')
        elif 'slope' in a:raise ValueError('Only pass filters take a slope.')
        allowed|={'shape','hz','db','q','slope'}
    elif k in ('compressor','glue'):
        fields=('thresholdDb','ratio','attackMs','releaseMs','makeupDb','mixPercent')
        for f in fields:
            if f in a and not (f=='releaseMs' and a[f]=='auto'):a[f]=_num(a[f],f)
        if not any(f in a for f in fields):raise ValueError('Give at least one %s setting.'%k)
        if 'autoMakeup' in a:
            if k!='compressor' or not isinstance(a['autoMakeup'],bool):raise ValueError('autoMakeup is a true/false Compressor setting.')
        allowed|=set(fields)|{'autoMakeup'}
        if 'sidechain' in a:
            s=a['sidechain']
            if k!='compressor':raise ValueError('Only the compressor action takes a sidechain.')
            if not isinstance(s,dict) or not isinstance(s.get('track'),str) or s['track']==a['track']:raise ValueError('Sidechain needs a different source track id.')
            s.setdefault('tap','Post FX')
            if s['tap'] not in TAPS:raise ValueError('Sidechain tap is one of %s.'%', '.join(TAPS))
            allowed|={'sidechain'}
    elif k=='utility':
        if 'gainDb' in a:a['gainDb']=_num(a['gainDb'],'gainDb')
        if 'widthPercent' in a:a['widthPercent']=_num(a['widthPercent'],'widthPercent')
        if 'bassMonoHz' in a and a['bassMonoHz'] is not None:a['bassMonoHz']=_num(a['bassMonoHz'],'bassMonoHz')
        if not any(f in a for f in ('gainDb','widthPercent','bassMonoHz')):raise ValueError('Give gainDb, widthPercent and/or bassMonoHz.')
        allowed|={'gainDb','widthPercent','bassMonoHz'}
    extra=set(a)-allowed
    if extra:raise ValueError('Unknown action fields: %s.'%', '.join(sorted(extra)))
    return a

def settings(a):
    return {k:v for k,v in a.items() if k not in ('kind','track','reuse','label','sidechain')}

# ---------------------------------------------------------------- snapshots
def _controls(device):return {c['id']:c for c in device.get('controls',())}

def _following_envelope(a,b):
    # A running envelope changes its instantaneous value without a user edit.
    # An overridden/deleted envelope changes state and must still be reported.
    return a.get('automation')==b.get('automation')==1

def diff(a,b):
    """Every difference between two neutral snapshots of one track, raw values compared exactly. Solo and mute are
    left out: they are how John listens (he solos a track to audit a change), Scrunch never writes them, and a solo
    must not stop a switch or a revert (live, 27 Sep: soloing track 39 refused the Candidate)."""
    out=[]
    for key in ('gain','pan'):
        if a[key].get('automation')!=b[key].get('automation'):
            out.append({'what':key+' automation','before':a[key].get('automation'),'after':b[key].get('automation')})
        if a[key]['raw']!=b[key]['raw'] and not _following_envelope(a[key],b[key]):
            out.append({'what':key,'before':a[key]['raw'],'after':b[key]['raw']})
    for key in ('name',):
        if a.get(key)!=b.get(key):out.append({'what':key,'before':a.get(key),'after':b.get(key)})
    ida=[d['id'] for d in a['devices']];idb=[d['id'] for d in b['devices']]
    if ida!=idb:out.append({'what':'devices','before':[(d['id'],d['name']) for d in a['devices']],'after':[(d['id'],d['name']) for d in b['devices']]})
    bb={d['id']:d for d in b['devices']}
    for d in a['devices']:
        e=bb.get(d['id'])
        if e is None:continue
        if d.get('name')!=e.get('name'):out.append({'what':'device name','device':d['id'],'before':d.get('name'),'after':e.get('name')})
        ca,cb=_controls(d),_controls(e)
        if list(ca)!=list(cb):out.append({'what':'controls','device':d['id']})
        for cid,c in ca.items():
            if cid in cb and c['raw']!=cb[cid]['raw']:
                out.append({'what':'control','device':d['id'],'deviceName':d.get('name'),'control':c['name'],'id':cid,
                            'before':c['raw'],'after':cb[cid]['raw'],'beforeDisplay':c.get('display'),'afterDisplay':cb[cid].get('display')})
        sa,sb=d.get('sidechain'),e.get('sidechain')
        if any((sa or {}).get(k)!=(sb or {}).get(k) for k in ('type','channel','typeTrack')):
            out.append({'what':'sidechain','device':d['id'],'before':sa and {k:sa.get(k) for k in ('type','channel','typeTrack')},
                        'after':sb and {k:sb.get(k) for k in ('type','channel','typeTrack')}})
    return out

def _device(state,ident):
    for d in state['devices']:
        if d['id']==ident:return d
    return None

def _is_switch(c):
    """A control whose change must come last on undo: an on/off switch (values under it are ignored while off)."""
    opts=[str(o).lower() for o in (c.get('options') or ())]
    return sorted(opts)==['off','on']

# ---------------------------------------------------------------- journal
def _save(rec,journal):
    if journal is None:return
    d=Path(journal);d.mkdir(parents=True,exist_ok=True)
    p=d/(rec['id']+'.json');tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(rec,indent=1,default=str));os.replace(tmp,p)

def load(ident,journal=JOURNAL):
    return json.loads((Path(journal)/(ident+'.json')).read_text())

def _matches(display_number,want):
    if isinstance(want,str):return None
    if display_number is None:return False
    if math.isinf(want) or math.isinf(display_number):return want==display_number
    return abs(display_number-want)<=max(1e-6,.006*abs(want))

# ---------------------------------------------------------------- apply / undo / A-B
def apply(action,host,journal=JOURNAL,use='mix-action'):
    """Make one mix change on the host, read it back, and return the record (before/after state).
    `use` names the workflow ('mix-action', or 'mix-automation' for a device an automation curve needs)."""
    import chain_position
    a=validate(action);t=a['track'];k=a['kind']
    before=host.state(t)
    # An offset must leave the existing volume envelope playing. Apply it after
    # the existing audio effects using a fresh Utility, never disable automation.
    original_action=None
    if k=='gain' and before['gain'].get('automation') in (1,2):
        original_action=dict(a)
        delta=a['byDb'] if 'byDb' in a else a['db']-before['gain']['db']
        a={'kind':'utility','track':t,'gainDb':delta,'reuse':False};k='utility'
    rec={'id':uuid.uuid4().hex,'version':VERSION,'action':a,'track':t,'trackName':before['name'],'at':time.time(),'use':use,
         'before':before,'created':[],'device':None,'stages':[],'status':'applying'}
    if original_action:
        rec['requestedAction']=original_action
        rec['automationHandling']='Volume envelope retained; Utility applies the offset. Absolute dB is relative to the fader at the recorded before-state.'
    _save(rec,journal)
    try:
        want={}
        if k=='gain':
            db=a['db'] if 'db' in a else before['gain']['db']+a['byDb']
            r=host.gain_raw(t,db);host.set_mixer(t,gain=r['value'],expect={'gain':before['gain']['raw']})
            want['gain']=r['number'];rec['resolved']=r
        elif k=='pan':
            r=host.pan_raw(t,a['pan']);host.set_mixer(t,pan=r['value'],expect={'pan':before['pan']['raw']})
            want['pan']=r['number'];rec['resolved']=r
        else:
            existing=[d for d in before['devices'] if d['kind']==k and str(d.get('name') or '').startswith(OWNED)]
            fresh=not (existing and a.get('reuse',True))
            if fresh:
                devices=[{'deviceName':d.get('device'),'name':d.get('name')} for d in before['devices']]
                at=chain_position.insert_at(devices) if before.get('category')!='master' else None
                made=host.insert(t,k,'%s %s %s'%(OWNED,k.upper() if k=='eq' else k.title(),rec['id'][:6]),at)
                rec['created'].append(made['id']);rec['device']=made['id'];rec['placement']=chain_position.describe(devices,at)
                _save(rec,journal)
            else:rec['device']=existing[-1]['id']
            dev=_device(host.state(t),rec['device'])
            stages,want=host.configure(k,dev,settings(a),fresh)
            for stage in stages:
                host.write(rec['device'],stage['writes']);rec['stages'].append(stage);_save(rec,journal)
                if stage.get('fields'):rec['fields']=dict(stage['fields'])   # setting -> control, for an automation lane on it
            if k=='compressor' and 'sidechain' in a:
                dev=_device(host.state(t),rec['device'])
                side=dev.get('sidechain')
                if side is None:raise ValueError('This host does not expose the compressor sidechain input.')
                source=host.state(a['sidechain']['track'])
                if hasattr(host,'routing_for'):name,by=host.routing_for(side,a['sidechain']['track'],source['name'])
                else:name,by=host.routing_name(side['types'],source['name']),None
                host.set_sidechain(rec['device'],name,a['sidechain']['tap'],expect_type=side['type'],**({'source':by} if by else {}))
                rec['sidechainRouting']={'type':name,'channel':a['sidechain']['tap']}
                dev=_device(host.state(t),rec['device'])
                on=[c for c in dev['controls'] if c['name'].lower() in ('s/c on','sidechain on')]
                if len(on)!=1:raise ValueError('No single sidechain switch on this compressor.')
                sw={'id':on[0]['id'],'name':on[0]['name'],'value':float(on[0]['max']),'expect':on[0]['raw']}
                host.write(rec['device'],[sw]);rec['stages'].append({'writes':[sw]})
            dev=_device(host.state(t),rec['device'])
            switch=host.switch(dev,True)
            if switch['value']!=switch['expect']:host.write(rec['device'],[switch]);rec['stages'].append({'writes':[switch]})
        after=host.state(t)
        rec['after']=after;rec['changes']=diff(before,after)
        rec['verified']=_verify_want(k,after,rec.get('device'),want)
        if not all(v['ok'] for v in rec['verified']):raise ValueError('Readback differs: %s'%[v for v in rec['verified'] if not v['ok']])
        rec['status']='applied';_save(rec,journal)
        return rec
    except Exception as exc:
        rec['status']='failed';rec['error']=str(exc);_save(rec,journal)
        try:
            rec['rollback']=_restore(rec,host,conflict_check=False)
        except Exception as again:rec['rollback']={'error':str(again)}
        _save(rec,journal)
        raise

def _verify_want(kind,state,device,want):
    out=[]
    if kind=='gain':
        out.append({'control':'gain','want':want['gain'],'display':state['gain']['display'],'ok':state['gain']['db']==want['gain']})
        return out
    if kind=='pan':
        got=state['pan']['pan']*50
        out.append({'control':'pan','want':want['pan'],'display':state['pan']['display'],'ok':abs(got-want['pan'])<1e-9})
        return out
    import daw_mix
    c={x['name']:x for x in _device(state,device)['controls']}
    for name,w in want.items():
        disp=c[name].get('display')
        ok=(str(disp).lower()==w.lower()) if isinstance(w,str) else _matches(daw_mix.number(disp),w)
        out.append({'control':name,'want':w,'display':disp,'ok':bool(ok)})
    return out

def _restore(rec,host,conflict_check=True):
    """Put the track back to rec['before']: remove created devices, write back raw values (settings
    before switches), restore routing, then read and compare."""
    t=rec['track'];before=rec['before'];now=host.state(t)
    if conflict_check and rec.get('after'):
        # Anything that moved since the action is his edit - unless it is only the A side of an A/B
        # (a value back at its original, or a switch on a device Scrunch inserted).
        def ab_side(d):
            if d['what']=='control':
                if d['device'] in rec['created']:return True
                old=_device(before,d['device']);c=_controls(old).get(d['id']) if old else None
                return c is not None and c['raw']==d['after']
            if d['what'] in ('gain','pan'):return before[d['what']]['raw']==d['after']
            return False
        since=[d for d in diff(rec['after'],now) if not ab_side(d)]
        if since:
            return {'identical':False,'refused':True,'why':'the track changed after this action; nothing was undone','changedSince':since}
    for ident in rec.get('created',()):
        if _device(now,ident) is not None:host.remove(ident)
    now=host.state(t)
    mix={}
    if now['gain']['raw']!=before['gain']['raw'] and not _following_envelope(now['gain'],before['gain']):mix['gain']=before['gain']['raw']
    if now['pan']['raw']!=before['pan']['raw']:mix['pan']=before['pan']['raw']
    if mix:host.set_mixer(t,**mix,expect={k:now[k]['raw'] for k in mix})
    for d in before['devices']:
        e=_device(now,d['id'])
        if e is None:continue
        cn=_controls(e)
        changed=[c for cid,c in _controls(d).items() if cid in cn and cn[cid]['raw']!=c['raw']]
        order=[c for c in changed if not _is_switch(c)]
        side_b,side_n=d.get('sidechain'),e.get('sidechain')
        if order:host.restore(d['id'],[{'id':c['id'],'name':c['name'],'value':c['raw'],'expect':cn[c['id']]['raw']} for c in order])
        if side_b and side_n and (side_b.get('type'),side_b.get('channel'),side_b.get('typeTrack'))!=(side_n.get('type'),side_n.get('channel'),side_n.get('typeTrack')):
            ch=side_b.get('channel') if side_b.get('channel') else None
            host.set_sidechain(d['id'],side_b['type'],ch,expect_type=side_n['type'],**({'source':side_b['typeTrack']} if side_b.get('typeTrack') else {}))
        switches=[c for c in changed if _is_switch(c)]
        if switches:host.restore(d['id'],[{'id':c['id'],'name':c['name'],'value':c['raw'],'expect':cn[c['id']]['raw']} for c in switches])
    final=host.state(t);left=diff(before,final)
    return {'identical':not left,'differences':left,'state':final}

def undo(rec,host,journal=JOURNAL):
    """Restore exactly the state before `rec` was applied, and prove it with a fresh read."""
    if rec.get('status') not in ('applied','ab'):raise ValueError('Only an applied action can be undone (this one is %s).'%rec.get('status'))
    result=_restore(rec,host)
    rec['undo']={k:v for k,v in result.items() if k!='state'};rec['undoAt']=time.time()
    if result.get('identical'):rec['status']='undone'
    _save(rec,journal)
    return result

def ab(rec,which,host,journal=JOURNAL):
    """Switch between A (the original sound) and B (the change). A bypasses devices Scrunch inserted
    and puts every other changed value back; B restores the change. Refused if the track was edited."""
    if which not in ('A','B'):raise ValueError('A or B.')
    if rec.get('status') not in ('applied','ab'):raise ValueError('A/B needs an applied action.')
    t=rec['track'];now=host.state(t)
    b_state,a_state=rec['after'],rec['before']
    # The states A and B differ only in what the action touched; everything else must be untouched.
    touched=diff(a_state,b_state)
    target=b_state if which=='B' else a_state
    mix={}
    for key in ('gain','pan'):
        if a_state[key]['raw']!=b_state[key]['raw'] and not _following_envelope(a_state[key],b_state[key]):
            if now[key]['raw'] not in (a_state[key]['raw'],b_state[key]['raw']):raise ValueError('The %s was changed since; A/B refused.'%key)
            if now[key]['raw']!=target[key]['raw']:mix[key]=target[key]['raw']
    if mix:host.set_mixer(t,**mix,expect={k:now[k]['raw'] for k in mix})
    for ident in rec.get('created',()):
        dev=_device(now,ident)
        if dev is None:raise ValueError('The inserted device is gone; A/B refused.')
        sw=host.switch(dev,which=='B')
        if sw['value']!=sw['expect']:host.write(ident,[sw])
    for d in b_state['devices']:
        if d['id'] in rec.get('created',()):continue
        prev=_device(a_state,d['id']);e=_device(now,d['id'])
        if prev is None or e is None:continue
        ca,cb,cn=_controls(prev),_controls(d),_controls(e)
        moves=[]
        for cid in cb:
            if cid in ca and ca[cid]['raw']!=cb[cid]['raw']:
                if cn[cid]['raw'] not in (ca[cid]['raw'],cb[cid]['raw']):raise ValueError('%s was changed since; A/B refused.'%cb[cid]['name'])
                want=(cb if which=='B' else ca)[cid]
                if cn[cid]['raw']!=want['raw']:moves.append({'id':cid,'name':want['name'],'value':want['raw'],'expect':cn[cid]['raw']})
        # Going to A, values first and switches last (a band is switched off after its values are back);
        # going to B, switches first (a band must be on before its values can be written).
        groups=[[m for m in moves if not _is_switch(cb[m['id']])],[m for m in moves if _is_switch(cb[m['id']])]]
        for group in (groups if which=='A' else groups[::-1]):
            if group:host.restore(d['id'],group)
    rec['status']='ab';rec['playing']=which;_save(rec,journal)
    return {'playing':which,'state':host.state(t)}

# ---------------------------------------------------------------- measuring
def _dsp():
    import reference_dsp
    return reference_dsp

def measure(audio,sr):
    """Loudness, dynamics, width and band energies of a capture (3..120 s)."""
    dsp=_dsp()
    y=dsp.prepare(np.asarray(audio,dtype=float),int(sr));r=dsp.analyse(y)
    P={b['name']:(10**(b['energyDbFS']/10) if b['energyDbFS'] is not None else 0.0) for b in r['bands']}
    def wavg(key,names):
        w=[(P[b['name']],b[key]) for b in r['bands'] if b['name'] in names and b[key] is not None]
        s=sum(x for x,_ in w);return sum(x*v for x,v in w)/s if s>0 else None
    names=[b['name'] for b in r['bands']];low=names[:2];wide=names[2:]
    mono=y.mean(axis=1);hop=int(.02*48000)
    frames=np.sqrt(np.mean(mono[:len(mono)//hop*hop].reshape(-1,hop)**2,axis=1))
    fdb=20*np.log10(np.maximum(frames,1e-9));act=fdb[fdb>fdb.max()-50] if len(fdb) else fdb
    rms=r['rmsDbFS']
    f=np.asarray(r['spectrum']['hz']);p=10**(np.asarray(r['spectrum']['powerDbPerHz'])/10)
    return {'seconds':round(len(y)/48000,3),'integratedLUFS':r['integratedLUFS'],'truePeakDbTP':r['truePeakDbTP'],
            'rmsDbFS':10*math.log10(max(1e-20,np.mean(y*y))),'crestFactorDb':r['crestFactorDb'],'correlation':r['correlation'],
            'balanceDb':(rms[1]-rms[0]) if None not in rms else None,
            'width':wavg('sideFraction',wide),'lowEndSide':wavg('sideFraction',low),'lowEndCorrelation':wavg('correlation',low),
            'envelopeRangeDb':float(np.percentile(act,95)-np.percentile(act,10)) if len(act) else None,
            'bands':{b['name']:b['energyDbFS'] for b in r['bands']},'bandEdges':{b['name']:[b['lowHz'],b['highHz']] for b in r['bands']},
            '_spectrum':(f,p)}

def region_db(m,lo,hi):
    """Energy (dBFS) of a measurement between lo and hi Hz, from its Welch spectrum."""
    f,p=m['_spectrum'];mask=(f>=lo)&(f<hi)
    if not mask.any():return None
    e=float(p[mask].sum()*(f[1]-f[0]));return 10*math.log10(e) if e>0 else None

SCALARS=('integratedLUFS','truePeakDbTP','rmsDbFS','crestFactorDb','correlation','balanceDb','width','lowEndSide','lowEndCorrelation','envelopeRangeDb')

def compare(before,after,repeat=None):
    """Change from `before` to `after`; with `repeat` (a second capture of the unchanged sound) each
    change is set against the measured wander, so a change inside the noise is not claimed."""
    def d(x,y):return None if x is None or y is None else round(y-x,3)
    out={'scalars':{},'bands':{}}
    for k in SCALARS:
        out['scalars'][k]={'before':before[k],'after':after[k],'change':d(before[k],after[k]),
                           'wander':None if repeat is None else (lambda w:None if w is None else abs(w))(d(before[k],repeat[k]))}
    for b in before['bands']:
        out['bands'][b]={'before':before['bands'][b],'after':after['bands'][b],'change':d(before['bands'][b],after['bands'][b]),
                         'wander':None if repeat is None else (lambda w:None if w is None else abs(w))(d(before['bands'][b],repeat['bands'][b]))}
    return out

def focus(action,before,after,repeat=None):
    """The measured change that matters for this action, in its own terms."""
    k=action['kind'];rows=[]
    def row(what,x,y,unit,w=None):
        ch=None if x is None or y is None else y-x
        rows.append({'measure':what,'before':None if x is None else round(x,2),'after':None if y is None else round(y,2),
                     'change':None if ch is None else round(ch,2),'unit':unit,
                     'wander':None if w is None else round(abs(w),2),
                     'beyondWander':None if w is None or ch is None else abs(ch)>abs(w)+.05})
    def rdb(lo,hi):
        return (region_db(before,lo,hi),region_db(after,lo,hi),None if repeat is None or region_db(repeat,lo,hi) is None else region_db(repeat,lo,hi)-region_db(before,lo,hi))
    def sc(key,label,unit):
        row(label,before[key],after[key],unit,None if repeat is None or repeat[key] is None or before[key] is None else repeat[key]-before[key])
    if k=='gain':sc('integratedLUFS','Integrated loudness','LU');sc('rmsDbFS','RMS level','dB')
    elif k=='pan':sc('balanceDb','Right minus left level','dB')
    elif k=='eq':
        hz=action['hz'];s=action['shape']
        spans={'bell':[(hz/2**(1/6),hz*2**(1/6),'third-octave at %g Hz'%hz)],'notch':[(hz/2**(1/6),hz*2**(1/6),'third-octave at %g Hz'%hz)],
               'low-shelf':[(20,hz/2,'below %g Hz'%(hz/2))],'high-shelf':[(hz*2,20000,'above %g Hz'%(hz*2))],
               'high-pass':[(hz/4,hz/2,'octave %g-%g Hz (stopband)'%(hz/4,hz/2)),(hz*2,hz*4,'octave %g-%g Hz (passband)'%(hz*2,hz*4))],
               'low-pass':[(hz*2,hz*4,'octave %g-%g Hz (stopband)'%(hz*2,hz*4)),(hz/4,hz/2,'octave %g-%g Hz (passband)'%(hz/4,hz/2))]}[s]
        for lo,hi,label in spans:
            x,y,w=rdb(lo,min(hi,23999));row('Energy '+label,x,y,'dB',w)
        sc('integratedLUFS','Integrated loudness','LU')
    elif k in ('compressor','glue'):
        sc('crestFactorDb','Crest factor','dB');sc('envelopeRangeDb','Level range (20 ms RMS, 10th-95th pct)','dB')
        sc('integratedLUFS','Integrated loudness','LU');sc('truePeakDbTP','True peak','dBTP')
    elif k=='utility':
        if 'gainDb' in action:sc('integratedLUFS','Integrated loudness','LU');sc('rmsDbFS','RMS level','dB')
        sc('width','Width (side share above 200 Hz)','');sc('lowEndSide','Low-end side share (20-200 Hz)','')
        sc('lowEndCorrelation','Low-end L/R correlation','');sc('correlation','L/R correlation','')
    return rows

# ---------------------------------------------------------------- models for predicting a move's effect
def _sos_biquad(b,a):
    return np.array([[b[0]/a[0],b[1]/a[0],b[2]/a[0],1.0,a[1]/a[0],a[2]/a[0]]])

def eq_sos(shape,hz,db=0.0,q=0.71,slope=48,sr=48000):
    """Standard (RBJ cookbook / Butterworth) filters: a model of the move, not Live's exact curve."""
    from scipy.signal import butter
    w=2*math.pi*min(hz,sr*.49)/sr;cs,sn=math.cos(w),math.sin(w);al=sn/(2*max(q,1e-3));A=10**(db/40)
    if shape=='bell':return _sos_biquad([1+al*A,-2*cs,1-al*A],[1+al/A,-2*cs,1-al/A])
    if shape=='notch':return _sos_biquad([1,-2*cs,1],[1+al,-2*cs,1-al])
    sa=2*math.sqrt(A)*al
    if shape=='low-shelf':
        return _sos_biquad([A*((A+1)-(A-1)*cs+sa),2*A*((A-1)-(A+1)*cs),A*((A+1)-(A-1)*cs-sa)],[(A+1)+(A-1)*cs+sa,-2*((A-1)+(A+1)*cs),(A+1)+(A-1)*cs-sa])
    if shape=='high-shelf':
        return _sos_biquad([A*((A+1)+(A-1)*cs+sa),-2*A*((A-1)+(A+1)*cs),A*((A+1)+(A-1)*cs-sa)],[(A+1)-(A-1)*cs+sa,2*((A-1)-(A+1)*cs),(A+1)-(A-1)*cs-sa])
    if shape in ('high-pass','low-pass'):
        kind='highpass' if shape=='high-pass' else 'lowpass'
        if slope==48:return butter(8,min(hz,sr*.49),kind,fs=sr,output='sos')
        if kind=='highpass':return _sos_biquad([(1+cs)/2,-(1+cs),(1+cs)/2],[1+al,-2*cs,1-al])
        return _sos_biquad([(1-cs)/2,1-cs,(1-cs)/2],[1+al,-2*cs,1-al])
    raise ValueError(shape)

def _stereo(y):
    y=np.asarray(y,dtype=float)
    if y.ndim==1:y=y[:,None]
    return np.repeat(y,2,axis=1) if y.shape[1]==1 else y

def simulate(action,audio,sr,sidechain=None):
    """The action's effect on `audio` under a standard model (see each branch). Pure; returns new audio."""
    from scipy.signal import sosfilt,sosfiltfilt,butter
    y=_stereo(audio);k=action['kind']
    if k=='gain':return y*10**(action.get('db',action.get('byDb',0))/20)
    if k=='pan':
        # Balance model: the far side is attenuated linearly, the near side left at unity.
        p=action['pan'];g=np.array([min(1,1-p),min(1,1+p)]);return y*g
    if k=='eq':
        return sosfilt(eq_sos(action['shape'],action['hz'],action.get('db',0.0),action.get('q',.71),action.get('slope',48),sr),y,axis=0)
    if k=='utility':
        m=(y[:,0]+y[:,1])/2;s=(y[:,0]-y[:,1])/2
        s=s*(action.get('widthPercent',100.0)/100)
        if action.get('bassMonoHz'):
            # Zero-phase Linkwitz-Riley-shaped split (a model): the side below the frequency is removed.
            lp=butter(2,action['bassMonoHz'],'lowpass',fs=sr,output='sos');s=s-sosfiltfilt(lp,s)
        return np.stack([m+s,m-s],axis=1)*10**(action.get('gainDb',0)/20)
    if k in ('compressor','glue'):
        det=_stereo(sidechain) if sidechain is not None else y
        n=min(len(det),len(y));det=det[:n];y=y[:n]
        lvl=20*np.log10(np.maximum(np.abs(det).max(axis=1),1e-9))
        T=action.get('thresholdDb',0.0);R=max(1.0,action.get('ratio',4.0));W=6.0
        over=lvl-T
        gr=np.where(over<=-W/2,0.0,np.where(over>=W/2,over*(1-1/R),(1-1/R)*(over+W/2)**2/(2*W)))
        att=math.exp(-1/(max(.01,action.get('attackMs',10.0))*1e-3*sr))
        rel_ms=action.get('releaseMs',100.0);rel=math.exp(-1/(max(1.0,200.0 if rel_ms=='auto' else rel_ms)*1e-3*sr))
        from scipy.signal import lfilter
        # One-pole attack/release smoothing of the gain reduction (sample loop avoided by two passes).
        env=np.empty_like(gr);g=0.0
        for i,x in enumerate(gr):
            g=att*g+(1-att)*x if x>g else rel*g+(1-rel)*x;env[i]=g
        gain=10**((-env+action.get('makeupDb',0.0))/20)[:,None]
        wet=action.get('mixPercent',100.0)/100
        return y*(wet*gain+(1-wet))
    raise ValueError(k)

def predict(action,track_audio,sr,mix_audio=None,sidechain=None):
    """Measured expected effect: the mix measured as it is, and with the track replaced by the model of
    the move applied to the track's own captured audio. Returns (before, after) measurements."""
    t=_stereo(track_audio);m=t if mix_audio is None else _stereo(mix_audio)
    n=min(len(t),len(m));t=t[:n];m=m[:n]
    moved=simulate(action,t,sr,sidechain)
    n=min(len(moved),n)
    return measure(m[:n],sr),measure(m[:n]-t[:n]+moved[:n],sr)

# ---------------------------------------------------------------- Mix Doctor finding -> action
def _band_of(metric):
    import reference_dsp
    if metric=='harsh2to6kHzDb':return 2000.0,6000.0
    if metric=='lowShareDb':return 20.0,200.0
    if metric.startswith('balance:'):
        name=metric.split(':',1)[1]
        for n,lo,hi in reference_dsp.BANDS:
            if n==name:return float(lo),float(hi)
    return None

def _bisect(fn,lo,hi,target,steps=24):
    """x in [lo,hi] with fn(x) nearest target, fn monotone. Returns (x, fn(x), reached)."""
    flo,fhi=fn(lo),fn(hi)
    if (flo-target)*(fhi-target)>0:
        x=lo if abs(flo-target)<abs(fhi-target) else hi
        return x,fn(x),False
    for _ in range(steps):
        mid=(lo+hi)/2;fm=fn(mid)
        if (fm-target)*(flo-target)<=0:hi,fhi=mid,fm
        else:lo,flo=mid,fm
    x=(lo+hi)/2;return x,fn(x),True

def _peak_hz(y,sr,lo,hi):
    from scipy.signal import welch
    f,p=welch(_stereo(y).mean(axis=1),sr,nperseg=8192)
    mask=(f>=lo)&(f<=hi)
    if not mask.any():return math.sqrt(lo*hi)
    # third-octave smoothing so a single harmonic does not pick the centre on its own
    sm=np.array([p[(f>=x/2**(1/6))&(f<=x*2**(1/6))].sum() for x in f[mask]])
    return float(f[mask][int(np.argmax(sm))])

def propose(finding,track,track_audio,sr,mix_audio=None):
    """A concrete action for `track` that answers a Mix Doctor finding, with its predicted effect.

    `finding` has 'metric' (a Mix Doctor metric key), 'value' and 'median' (the finding's measurement
    and its target), and optionally 'startSeconds'/'endSeconds'. `track_audio` is the target track over
    the finding's window; `mix_audio` the whole mix over the same window (omit it when the track is the
    mix). The kind of move follows from what was measured (which metric, which band, by how much, how
    much of it this track owns); the setting is solved against the model applied to the measured
    audio. Nothing here reads note text.
    """
    metric=str(finding.get('metric') or '');value=finding.get('value');median=finding.get('median')
    if value is None or median is None:raise ValueError('A finding needs a measured value and its target (median).')
    value=float(value);median=float(median);excess=value-median
    t=_stereo(track_audio);mix=t if mix_audio is None else _stereo(mix_audio)
    n=min(len(t),len(mix));t=t[:n];mix=mix[:n]
    base=measure(mix,sr);own=measure(t,sr)
    out={'finding':{k:finding.get(k) for k in ('id','kind','label','metric','value','median','startSeconds','endSeconds')},
         'track':track,'action':None,'model':'standard biquad / M-S / feed-forward compressor model applied to the captured track audio; the Live result is measured after, not assumed'}
    def result(action,key,label,target,reached,extra=None):
        mb,ma=predict(action,t,sr,None if mix_audio is None else mix)
        get=(lambda m:region_db(m,*key)) if isinstance(key,tuple) else (lambda m:m[key])
        out.update(action=action,expected={'measure':label,'before':_r(get(mb)),'after':_r(get(ma)),'change':_r(get(ma)-get(mb)) if get(ma) is not None and get(mb) is not None else None,
                                           'target':_r(target),'reached':reached,**(extra or {})})
        return out
    band=_band_of(metric)
    if band:
        lo,hi=band;hi=min(hi,23999.0)
        pm,pt=10**(region_db(base,lo,hi)/10),10**(region_db(own,lo,hi)/10)
        share=min(1.0,pt/pm);out['basis']={'band':[lo,hi],'excessDb':_r(excess),'trackShareOfBand':_r(share,3)}
        if share<.05:
            out['why']='This track holds %.1f%% of the %g-%g Hz energy in that window; changing it cannot move the mix there.'%(share*100,lo,hi);return out
        want=region_db(base,lo,hi)-excess
        low_own=region_db(own,20,hi)-10*math.log10(max(1e-20,np.mean(t*t)))
        if hi<=200 and excess>0 and low_own<-20:
            shape='high-pass';fn=lambda hz:region_db(predict({'kind':'eq','track':track,'shape':shape,'hz':hz,'slope':12,'q':.71},t,sr,None if mix_audio is None else mix)[1],lo,hi)
            hz,got,ok=_bisect(fn,20.0,hi*2,want)
            return result({'kind':'eq','track':track,'shape':shape,'hz':round(hz),'slope':12,'q':.71},(lo,hi),'%g-%g Hz energy (dBFS)'%(lo,hi),want,ok,
                          {'chosenBecause':'this track has %.1f dB of its own energy below %g Hz relative to its total, so it can be filtered out rather than reshaped'%(low_own,hi)})
        if hi<=200:shape,hz,q='low-shelf',hi,.71
        elif lo>=8000:shape,hz,q='high-shelf',lo,.71
        else:
            shape='bell';hz=_peak_hz(t,sr,lo,hi);q=float(np.clip(hz/(hi-lo),.5,8.0))
        rng=(-15.0,0.0) if excess>0 else (0.0,15.0)
        mk=lambda g:{'kind':'eq','track':track,'shape':shape,'hz':round(hz),'db':round(g,1),'q':round(q,2)}
        fn=lambda g:region_db(predict(mk(g),t,sr,None if mix_audio is None else mix)[1],lo,hi)
        g,got,ok=_bisect(fn,*rng,want)
        return result(mk(g),(lo,hi),'%g-%g Hz energy (dBFS)'%(lo,hi),want,ok,
                      {'chosenBecause':'%s: %s at the track\'s strongest point in the band (%d Hz), gain solved so the band moves %.1f dB'%(
                          'band above 8 kHz' if shape=='high-shelf' else 'band below 200 Hz' if shape=='low-shelf' else 'band inside the spectrum',shape,round(hz),-excess)})
    if metric=='width':
        if own['width'] is None or (own['width']<.01 and excess<0):
            out['why']='The track has no side content above 200 Hz (side share %s); width cannot be added to a mono source.'%_r(own['width'],3);return out
        mk=lambda w:{'kind':'utility','track':track,'widthPercent':round(w)}
        fn=lambda w:predict(mk(w),t,sr,None if mix_audio is None else mix)[1]['width']
        w,got,ok=_bisect(fn,0.0,400.0,median)
        return result(mk(w),'width','Width (side share above 200 Hz)',median,ok)
    if metric in ('lowEndSide','lowEndCorrelation'):
        target_side=float(median) if metric=='lowEndSide' else None
        chosen=None
        for f in (50,63,80,100,120,150,200,250,315,400,500):
            a={'kind':'utility','track':track,'bassMonoHz':float(f)}
            after=predict(a,t,sr,None if mix_audio is None else mix)[1]
            ok=(after['lowEndSide'] is not None and after['lowEndSide']<=max(target_side,.02)) if target_side is not None else \
               (after['lowEndCorrelation'] is not None and after['lowEndCorrelation']>=max(median,.9))
            chosen=(a,ok)
            if ok:break
        a,ok=chosen
        return result(a,'lowEndSide','Low-end side share (20-200 Hz)',target_side if target_side is not None else 0.0,ok,
                      {'chosenBecause':'lowest bass-mono frequency whose modelled result meets the target'})
    if metric in ('momentaryLUFS','shortTermLUFS','integratedLUFS'):
        target=base['integratedLUFS']-excess
        mk=lambda g:{'kind':'gain','track':track,'byDb':round(g,1)}
        fn=lambda g:predict(mk(g),t,sr,None if mix_audio is None else mix)[1]['integratedLUFS']
        g,got,ok=_bisect(fn,-24.0,6.0,target)
        return result(mk(g),'integratedLUFS','Integrated loudness (LUFS)',target,ok)
    if metric=='truePeakDbTP':
        target=-1.0
        mk=lambda g:{'kind':'gain','track':track,'byDb':round(g,1)}
        fn=lambda g:predict(mk(g),t,sr,None if mix_audio is None else mix)[1]['truePeakDbTP']
        g,got,ok=_bisect(fn,-24.0,0.0,target)
        return result(mk(g),'truePeakDbTP','True peak (dBTP)',target,ok)
    if metric=='crestFactorDb':
        if excess<=0:
            out['why']='The crest factor is %.1f dB below its target: the window is already squashed, and a compressor can only reduce it further.'%(-excess);return out
        mono=t.mean(axis=1);hop=int(.01*sr)
        fr=20*np.log10(np.maximum(np.sqrt(np.mean(mono[:len(mono)//hop*hop].reshape(-1,hop)**2,axis=1)),1e-9))
        active=fr[fr>fr.max()-40]
        threshold=float(np.percentile(active,50))
        onsets=np.flatnonzero(np.diff(fr)>6)
        ioi=float(np.median(np.diff(onsets))*10) if len(onsets)>2 else 200.0
        mk=lambda r:{'kind':'compressor','track':track,'thresholdDb':round(threshold,1),'ratio':round(r,2),'attackMs':10.0,'releaseMs':round(float(np.clip(ioi/2,30,1000)))}
        fn=lambda r:predict(mk(r),t,sr,None if mix_audio is None else mix)[1]['crestFactorDb']
        r,got,ok=_bisect(fn,1.0,20.0,base['crestFactorDb']-excess)
        return result(mk(r),'crestFactorDb','Crest factor (dB)',base['crestFactorDb']-excess,ok,
                      {'chosenBecause':'threshold at the median 10 ms level of the track (%.1f dBFS), release half the measured note spacing (%.0f ms)'%(threshold,ioi)})
    out['why']='No mix action is mapped to the measurement %r.'%metric
    return out

def _r(x,d=2):
    return None if x is None or (isinstance(x,float) and not math.isfinite(x)) else round(float(x),d)

def public(m):
    """A measurement without its spectrum arrays, for JSON."""
    return {k:v for k,v in m.items() if not k.startswith('_')}
