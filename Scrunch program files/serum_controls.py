"""Legacy named Rack controls with live readback and per-control undo.

Known mappings use calibrated targets when available. Other compatible mappings
report observed host values without claiming calibrated physical-unit prediction.
"""
import json,math,time,uuid,re
from pathlib import Path
import engine,daw_adapter,serum_assets,instrument_identity,synth_identity_graph,workflow_requirements

RACK='Scrunch Serum - Verified Controls'
CONTROLS={'cutoff':('Filter 1 Freq',1),'drive':('Filter 1 Drive',2),
          'detune':('A Uni Detune',3),'attack':('Env 1 Attack',4),'release':('Env 1 Release',5)}

DIVA_CONTROLS={'cutoff':('Frequency',1),'resonance':('Resonance',2),'attack':('Attack',3),'decay':('Decay',4),'sustain':('Sustain',5)}
JUP_CONTROLS={'cutoff':('Cutoff',1),'resonance':('Resonance',2),'detune':('OSC 1 OSC 1 Super Saw Detune',3),'attack':('Amp Envelope Attack',4),'release':('Amp Envelope Release',5)}
PROFILES={
 'jup':{'controls':JUP_CONTROLS,'instrument':'instrument: Jup-8000 V','racks':{x['name'] for x in serum_assets.ASSETS.values() if x.get('profile')=='jup'},'journal':'jup-changes'},
 'serum':{'controls':CONTROLS,'instrument':'instrument: Serum 2','racks':{x['name'] for x in serum_assets.ASSETS.values() if x.get('profile','serum')=='serum'},'journal':'serum-changes'},
 'diva':{'controls':DIVA_CONTROLS,'instrument':'instrument: Diva','racks':{'Scrunch Diva - Verified Controls'},'journal':'diva-changes'}
}
def display_unit(parameter,profile):
    # JUP's PP units are inconsistent with AX values: never claim Hz/ms conversion.
    return 'host units' if profile=='jup' else parameter.get('unit') or 'host units'

def profile_for(profile):
    if profile not in PROFILES:raise ValueError('Unknown verified synth profile.')
    return PROFILES[profile]

def validate(spec,profile='serum'):
    controls=profile_for(profile)['controls']
    if not isinstance(spec,dict):raise ValueError('Invalid synth command.')
    if spec.get('op')=='restore':
        if set(spec)!={'op','transaction'} or not isinstance(spec['transaction'],str) or not re.fullmatch('[a-f0-9]{32}',spec['transaction']):raise ValueError('Restore needs the saved synth change ID.')
    elif spec.get('op')=='set':
        if set(spec)!={'op','track','control','macroValue'} or not isinstance(spec['track'],str) or not spec['track'].strip() or spec['control'] not in controls:raise ValueError('Name a track and a verified synth control.')
        v=spec['macroValue']
        if type(v) is not int or not 0<=v<=127:raise ValueError('synth macro value must be an integer from 0 to 127, not Hz or percent.')
    else:raise ValueError('Unsupported synth operation.')
    return spec

def _parameters(d):return {p['name']:p for p in d.get('parameters',[])}
def _values(d):return {p['name']:p.get('value') for p in d.get('parameters',[]) if p.get('state')!='inactive'}
def _root(profile='serum'):
    p=engine.DATA/profile_for(profile)['journal'];p.mkdir(exist_ok=True);return p

def _save(record,profile='serum'):
    from transaction_store import Store
    path=_root(profile)/(record['id']+'.json');Store(path.parent).write(path,record)

def _resolve_once(track,adapter=None,profile='serum'):
    config=profile_for(profile)
    a=adapter or daw_adapter.DEFAULT;session=a.read_session()
    matches=[t for t in session.get('tracks',[]) if t.get('name','').casefold()==track.strip().casefold()]
    if len(matches)!=1:raise ValueError('%d tracks are named "%s", so I can\'t tell which one you mean for a control change. Rename one so they\'re unique, then try again - this is the one place I won\'t guess, since it would send parameter changes to the wrong instrument.'%(len(matches),track.strip()))
    t=a.read_track(matches[0]['id']);devices=t.get('devices',[])
    candidates=[]
    for device in devices:
        if device.get('type')!='instrument-rack':continue
        candidate=a.read_rack(device['id'])
        for chain in candidate.get('chains',[]):
            for member in chain.get('devices',[]):
                if member.get('type')==config['instrument']:
                    candidates.append((candidate,a.read_device(member['id'])))
    if len(candidates)!=1:raise ValueError('This Rack adapter needs one identifiable '+config['instrument']+'; use Sound design for directly exposed instrument controls.')
    rack,child=candidates[0]
    if not rack.get('macros',{}).get('hasMappings'):raise ValueError('This Rack has no mapped macros; use Sound design for exposed instrument controls.')
    if child.get('type')!=config['instrument']:raise ValueError('The instrument inside this Rack has changed since it was mapped. Confirm which synth is intended before controls are adjusted - same reason as above, this one\'s not a guess.')
    rp,cp=synth_identity_graph.validate(matches[0],t,rack,child)
    for raw,n in config['controls'].values():
        macro=rp.get(f'{raw} (Macro {n})',{});p=cp.get(raw,{})
        if macro.get('min')!=0 or macro.get('max')!=127 or macro.get('state') in ('inactive','disabled') or macro.get('enabled') is False or not isinstance(macro.get('value'),(int,float)) or not math.isfinite(macro['value']):raise ValueError('The host reports a disabled or missing mapped macro.')
        if p.get('enabled') is not False or p.get('state')!='disabled':raise ValueError('A mapped control has been disconnected from its parameter, so the mapping is stale. Re-map it in Ableton before I can use it.')
    return {'track':t,'rack':rack,'child':child,'session':session.get('name')}


def resolve(track,adapter=None,profile='serum'):
    # Host reads are not atomic. Re-read the entire graph and fail on any drift;
    # do not retry under a newly matching name or silently switch instances.
    first=_resolve_once(track,adapter,profile)
    second=_resolve_once(track,adapter,profile)
    if instrument_identity.signature(first)!=instrument_identity.signature(second):
        raise ValueError('Instrument identity changed during inspection; no control was changed.')
    return second


def context(profile='serum'):
    config=profile_for(profile)
    a=daw_adapter.DEFAULT;out=[]
    try:
        for t in a.read_session().get('tracks',[]):
            if t.get('instrument')!='Instrument Rack':continue
            try:
                r=resolve(t['name'],a,profile);rp=_parameters(r['rack']);cp=_parameters(r['child'])
                out.append({'track':t['name'],'controls':{k:{'macroValue':rp[f'{raw} (Macro {n})']['value'],'measuredValue':cp[raw]['value'],'unit':display_unit(cp[raw],profile),'adapterReportedUnit':cp[raw].get('unit'),'maxStep':127} for k,(raw,n) in config['controls'].items()}})
            except ValueError:continue
        return {'racks':out,'limits':'This rack adapter exposes the listed controls across their full 0-127 range; those are macro positions, not physical units. Sound design also supports the current instrument through its exposed controls. Macro snapshots are not complete presets; this adapter does not create modulation routing.'}
    except Exception as e:return {'racks':[],'unavailable':str(e)}

def _same_target(old,new):
    if any(old[k]['id']!=new[k]['id'] for k in ('track','rack','child')):raise ValueError('synth instance changed; inspect the saved change before recovery.')

def _poll(read,expected):
    for _ in range(20):
        r=read()
        if _values(r)==expected:return r
        time.sleep(.1)
    raise ValueError('synth readback did not match. Recovery remains pending.')

def expected_parameter(resolved,profile,raw,target):
    """Independent, offline-calibrated target; never learn from this write.

    Plugin display scales are nonlinear. The versioned table records all 128
    integer positions for each supported rack, with exact host display values.
    Unknown schemas or mappings fail closed instead of assuming a linear scale.
    """
    try:
        data=json.loads(Path(__file__).with_name('synth-target-values.json').read_text())
        rack=data['racks'][resolved['rack']['name']]
        p=rack['parameters'][raw];observed=_parameters(resolved['child'])[raw]
        if (data['version']!=1 or rack['profile']!=profile or rack['instrument']!=resolved['child']['type']
                or p['macro']!=next(n for name,n in profile_for(profile)['controls'].values() if name==raw)
                or p['metadata']!={k:observed.get(k) for k in ('min','max','unit','options')}
                or len(p['values'])!=128):raise ValueError()
        expected=p['values'][target]
        if type(expected) not in (int,float) or not math.isfinite(expected):raise ValueError()
        return expected
    except (OSError,ValueError,KeyError,TypeError,IndexError,StopIteration):
        return None

def _verify_parameter(record,a,profile):
    raw,_=profile_for(profile)['controls'][record['spec']['control']]
    stable=0
    for _ in range(20):
        current=resolve(record['spec']['track'],a,profile)
        if (instrument_identity.signature(current)!=instrument_identity.signature(record['target'])
                or instrument_identity.ensure(current,a,profile)!=record['identityProof']):
            raise ValueError('Instrument identity changed during parameter verification.')
        values=_values(current['child']);actual=values.get(raw)
        record['lastObservedChild']={k:('non-finite: '+str(v) if isinstance(v,float) and not math.isfinite(v) else v) for k,v in values.items()}
        if any(values.get(k)!=v for k,v in record['childBefore'].items() if k!=raw):
            raise ValueError('Another instrument parameter changed; the mapping needs inspection.')
        matched=(type(actual) in (int,float) and math.isfinite(actual)
                 and (record['expectedChildValue'] is None or actual==record['expectedChildValue'])
                 and _values(current['rack'])==record['expectedMacros'])
        stable=stable+1 if matched else 0
        if stable==2:return current
        time.sleep(.1)
    raise ValueError('Underlying synth parameter did not reach the expected target; macro movement alone is not verified.')

def execute(spec,adapter=None,profile='serum'):
    config=profile_for(profile)
    validate(spec,profile);a=adapter or daw_adapter.DEFAULT
    daw_adapter.require(a,workflow_requirements.SYNTH_RESTORE if spec['op']=='restore' else workflow_requirements.SYNTH_SET,'change synth controls')
    with engine.PLAY_LOCK:
        if spec['op']=='restore':
            p=_root(profile)/(spec['transaction']+'.json')
            if not p.exists():raise ValueError('Unknown synth change.')
            record=json.loads(p.read_text())
            if record['status']=='restored':return 'That synth change is already restored.'
            return _restore(record,a,profile=profile)
        r=resolve(spec['track'],a,profile);identity=instrument_identity.ensure(r,a,profile);raw,n=config['controls'][spec['control']];name=f'{raw} (Macro {n})'
        before=_values(r['rack']);target=spec['macroValue']
        if target==before[name]:return 'No write was needed: the macro already displays the requested position.'
        expected_child=expected_parameter(r,profile,raw,target)
        child_before=_values(r['child']).get(raw)
        if type(child_before) not in (int,float) or not math.isfinite(child_before):raise ValueError('Underlying synth parameter state is unavailable; no write was made.')
        record={'id':uuid.uuid4().hex,'status':'preparing','spec':spec,'target':r,'before':before,'childBefore':_values(r['child']),'identityProof':identity}
        record.update(expectedChildValue=expected_child,expectedMacros={**before,name:target})
        _save(record,profile)
        try:
            fresh=resolve(spec['track'],a,profile);_same_target(r,fresh)
            if instrument_identity.ensure(fresh,a,profile)!=identity:raise ValueError('The project or instrument changed while I was checking it. Nothing further will be changed.')
            if _values(fresh['rack'])!=before or _values(fresh['child'])!=record['childBefore']:raise ValueError('synth changed before checkpointing.')
            fresh=resolve(spec['track'],a,profile);_same_target(r,fresh)
            if instrument_identity.ensure(fresh,a,profile)!=identity:raise ValueError('The project or instrument changed while I was checking it. Nothing further will be changed.')
            if _values(fresh['rack'])!=before or _values(fresh['child'])!=record['childBefore']:raise ValueError('synth changed before applying.')
            record['status']='applying';_save(record,profile)
            a.set_rack_parameter(r['rack']['id'],name,target)
            expected=record['expectedMacros']
            verified=_verify_parameter(record,a,profile)
            child_after=_values(verified['child'])
            record.update(status='applied',after=expected,childAfter=child_after);_save(record,profile)
            p=_parameters(verified['child'])[raw]
            return f"{r['track']['name']}: {spec['control']} macro {before[name]} → {target}; readback {p['value']} {display_unit(p,profile)}. Verified in Live. Change ID: {record['id']}."
        except Exception as failure:
            record['verificationError']=str(failure);_save(record,profile)
            if record['status']=='applying':
                try:_restore(record,a,failed_apply=True,profile=profile)
                except Exception as recovery:
                    record['status']='restore-pending';record['recoveryError']=str(recovery);_save(record,profile)
            raise

def _restore(record,a,failed_apply=False,profile='serum'):
    config=profile_for(profile)
    r=resolve(record['spec']['track'],a,profile);_same_target(record['target'],r)
    raw,n=config['controls'][record['spec']['control']];changed=f'{raw} (Macro {n})'
    expected_macros={**_values(r['rack']),changed:record['before'][changed]}
    expected_child=record['childBefore'] if failed_apply else {**_values(r['child']),raw:record['childBefore'][raw]}
    record['status']='restore-pending';_save(record,profile)
    try:a.set_rack_parameter(r['rack']['id'],changed,record['before'][changed])
    except Exception:
        # A lost acknowledgement does not imply the write failed; read it back once.
        _poll(lambda:a.read_rack(r['rack']['id']),expected_macros)
    _poll(lambda:a.read_rack(r['rack']['id']),expected_macros)
    _poll(lambda:a.read_device(r['child']['id']),expected_child)
    record['status']='restored';_save(record,profile)
    return f"Restored the saved synth macro settings on {r['track']['name']}; exposed values verified."
