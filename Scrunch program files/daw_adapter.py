"""Scrunch DAW boundary. Producer Pal wire details live only in this module.

Scrunch-owned contract (Phase 1). Callers use the named operations below; they
never build host commands.
- Positions and lengths are quarter-note beats from Arrangement start (4/4).
- Notes are music.validate dicts {p,t,d,v}; the backend parses host notation.
- Host ids are opaque references valid for the open Live process only.
- Track: {id,trackIndex,name,kind,muted,soloed,mutedViaSolo,arrangementClipCount,
  sessionClipCount}; track() adds gainDb, instrument, devices, clips, mainOutput.
- Clip: {id,name,kind,view,start,length,span,offset,looping,timeSignature,notes}.
  Unreadable positions/lengths are None, never guessed.
- Transport: {playing, loop:{start,end}|None}.
Unsupported operations raise AdapterCapabilityError before any host write.

Native devices and parameters (Scrunch-owned; requirements in DEVICE_REQUIREMENTS):
- devices(track, parameters=False) -> device tree for one regular track. Device:
  {id,trackId,path,name,className,deviceName,role,plugin,active,parameterCount,
  unavailable,truncated,chains,returnChains,parameters}.
- device(id, parameters=True) -> one Device record (fields as above) whose
  `parameters` is a list of Parameter records.
- device_parameter(device, id) -> one Parameter record:
  {id,index,name,originalName,value,min,max,defaultValue,quantized,options,
  enabled,state,automation,display,unavailable}. Values are the host's internal
  units within the parameter's own min..max; display is host text, never parsed.
  Attributes the host does not expose are listed in `unavailable`, never guessed.
- set_device_parameter(device, id, name, value=|option=, expect_value=) writes one
  parameter and returns verified readback (with previousValue). A name, state,
  automation or expect_value mismatch raises DeviceParameterConflict with nothing
  written; failed readback raises DeviceParameterUnverified and is never retried
  or auto-reverted. DEVICE_CONTRACT_GAPS are refused explicitly.

Device, synth and analysis layers use read_session/read_live_set/read_track/
read_track_detail/read_device/read_device_detail/read_rack, which return records
in the declared HOST_SCHEMA (field names compatible with stored journals).
"""
import json,re,subprocess,urllib.request,urllib.error
from fractions import Fraction
import music

ENDPOINT='http://localhost:3350/api/tools/'

def live_process_id():
    try:
        pids=subprocess.check_output(['/usr/bin/pgrep','-x','Live'],text=True,timeout=3).strip().splitlines()
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Timed out checking whether Live is running.') from exc
    except subprocess.CalledProcessError as exc:
        if exc.returncode==1:raise ValueError('Live is not running.') from exc
        raise ValueError('Could not check whether Live is running: pgrep failed.') from exc
    if not pids:raise ValueError('Live is not running.')
    if len(pids)!=1 or not pids[0].isdigit():
        raise ValueError('More than one Ableton Live process is running, so instrument controls are paused until that is resolved. Quit the extra copy, or say "use the newest" to target the most recently launched one.')
    return pids[0]

def live_process_epoch():
    pid=live_process_id()
    try:
        launched=subprocess.check_output(['/bin/ps','-p',pid,'-o','lstart='],text=True,timeout=3).strip()
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Timed out reading the Live process start time.') from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError('Live is no longer running or its process start time could not be read.') from exc
    if not launched:raise ValueError('Live is no longer running or its process start time is unavailable.')
    return pid+':'+launched

class AdapterCapabilityError(ValueError):
    """A missing host contract, not a transient transport failure."""
    def __init__(self, capability, reason):
        self.capability=capability
        super().__init__(f'DAW adapter cannot {capability}: {reason}')

class DeviceParameterConflict(ValueError):
    """The parameter no longer matches what was read, or is disabled or automated. Nothing was written."""

class DeviceParameterUnverified(ValueError):
    """A parameter write was sent but readback did not confirm it. Inspect Live; never retry blindly."""

CONTRACT_GAPS={
    'read_automation':'No envelope point/time/interpolation collection is exposed. Parameter automation state is not an envelope snapshot.',
    'write_automation':'No arbitrary envelope writer or exact envelope restoration/readback contract is exposed.',
    'read_compensated_latency':'No complete per-device latency, compensation state and compensated routing-path timing contract is exposed.'}

# Device-layer operations no backend implements. Reported as unsupported so no
# workflow infers them from the native device operations.
DEVICE_CONTRACT_GAPS={
    'read_device_routing':'Device routing and sidechain sources are not exposed.',
    'read_macro_mappings':'Rack macro-to-parameter mappings are not exposed; macro-controlled parameters are refused.',
    'update_device_structure':'Loading, deleting, moving or switching devices on/off is not part of the native device contract.',
    'return_master_devices':'Native device operations (devices/device/set_device_parameter) search regular, return and master tracks, but devices(track) accepts regular tracks only; host-schema reads cover all three.',
    'plugin_step_options':'Plug-in stepped controls exposed as continuous 0-1 values have no named options to write.'}

# Scrunch-owned native device operations (see module docstring).
DEVICE_REQUIREMENTS=frozenset({'devices','device','device_parameter','set_device_parameter'})
# Recorded arrangement automation (see arrangement_automation.py); not a breakpoint envelope contract.
AUTOMATION_REQUIREMENTS=frozenset({'automation_target','record_automation','automation_take_status',
    'stop_automation_take','sample_automation','undo_automation_take'})
# Loop a span and play from its start, verified by the playhead (Remote Script bridge only).
SPAN_PLAYBACK=frozenset({'play_span'})
# Audio clip source file and warp markers, and loading a browser instrument (audio_to_midi.py; Remote Script bridge only).
AUDIO_TO_MIDI=frozenset({'clip.audio_source','load_device'})

# ---- Whole-workflow capability requirements ----
# Names for every backend operation a Scrunch workflow can depend on. Workflows
# declare their complete requirement set and call require() before their first
# mutation, so an unsupported plan cannot leave partial tracks or clips.
REQUIREMENTS=frozenset({
    'session','track','clip','transport','selected_track',
    'create_midi_track','duplicate_track','delete_track','delete_clip',
    'update_track.name','update_track.mute','update_track.solo','update_track.arm',
    'update_track.gainDb','update_track.monitoring',
    'create_arrangement_clip','create_arrangement_clip.looping','replace_clip_notes',
    'select_track','select_clip','open_plugin_window',
    'stop_playback','stop_session_clips','play_arrangement','set_arrangement_loop','move_playhead',
    'save_checkpoint','host_schema_reads','rack_checkpoint','set_rack_parameter','restore_rack',
    'create_native_effect','set_device_parameters','delete_device'})|DEVICE_REQUIREMENTS|AUTOMATION_REQUIREMENTS|SPAN_PLAYBACK|AUDIO_TO_MIDI

def require(adapter,requirements,workflow):
    """Refuse a whole workflow before any host write if any operation is unavailable."""
    requirements=set(requirements)
    unknown=requirements-REQUIREMENTS
    if unknown:raise ValueError('Unknown workflow requirement: '+', '.join(sorted(unknown)))
    supports=getattr(adapter,'supports',None)
    if not callable(supports):
        raise AdapterCapabilityError(workflow,'this DAW backend does not declare its capabilities. Nothing was changed.')
    missing=sorted(r for r in requirements if not supports(r))
    if missing:
        raise AdapterCapabilityError(workflow,'it needs operations this DAW backend does not support ('+', '.join(missing)+'). Nothing was changed.')

# ---- Producer Pal transport and wire translation (backend-private) ----

def call(name,args,timeout=12,log=True):
    req=urllib.request.Request(ENDPOINT+name,json.dumps(args).encode(),
        {'Content-Type':'application/json','x-producer-pal-notation':'midi-json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: out=json.load(r)
    except urllib.error.HTTPError as exc:
        message=f'Producer Pal HTTP {exc.code}: request to {name} was rejected; do not assume a connection failure or retry a write.'
        if log:
            import checkpoints
            checkpoints.event('host-error',tool=name,arguments=args,error=message,httpStatus=exc.code)
        raise ValueError(message) from None
    except (OSError,urllib.error.URLError):
        if log:
            import checkpoints
            checkpoints.event('host-error',tool=name,arguments=args,error='connection unavailable or timeout')
        raise ValueError('Ableton connection unavailable. Open a Scrunch session with the Producer Pal device running.') from None
    if log:
        import checkpoints
        checkpoints.event('host-call',tool=name,arguments=args,result=out.get('result'),isError=bool(out.get('isError')))
    if out.get('isError'): raise ValueError('Producer Pal: '+str(out.get('result'))[:500])
    return out['result']

def parse_notes(text):
    # Producer Pal midi-json permits bare keys and numeric fractions. Never eval.
    text = re.sub(r'([,{])\s*([A-Za-z_][A-Za-z_0-9]*)\s*:', r'\1"\2":', text)
    text = re.sub(r'(-?\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)',
                  lambda m: str(float(Fraction(m[1])/Fraction(m[2]))), text)
    return json.loads(text)

def to_beats(position):
    """Host 'bar|beat' (4/4, note-value offsets allowed) to beats; None when unrecognised."""
    return music.position_beats(position)

def to_position(beats):
    try:return music.position_text(beats)
    except ValueError as exc:raise AdapterCapabilityError('address position',str(exc)) from None

def _length_beats(text):
    return music.duration_beats(text)

def _host_length(beats):
    try:return music.duration_text(beats)
    except ValueError as exc:raise AdapterCapabilityError('create clip',str(exc)) from None

def _state(track):
    state=track.get('state','')
    return {'muted':state in ('muted','muted-also-via-solo'),'soloed':state=='soloed',
            'mutedViaSolo':state in ('muted-via-solo','muted-also-via-solo')}

def _track(raw):
    out={'id':raw.get('id'),'trackIndex':raw.get('trackIndex'),'name':raw.get('name'),'kind':raw.get('type'),
         **_state(raw),'arrangementClipCount':raw.get('arrangementClipCount',len(raw.get('arrangementClips',[]))),
         'sessionClipCount':raw.get('sessionClipCount',0)}
    if 'arrangementClips' in raw:
        out['clips']=[{'id':c.get('id'),'name':c.get('name'),'start':to_beats(c.get('arrangementStart')),
                       'span':_length_beats(c.get('arrangementLength'))} for c in raw['arrangementClips']]
    if 'devices' in raw:
        out['devices']=[_device(d) for d in raw['devices']]
    for key in ('gainDb','instrument'):
        if key in raw:out[key]=raw[key]
    if 'outputRoutingType' in raw:
        out['mainOutput']=(raw.get('outputRoutingType') or {}).get('name')=='Main'
    return out

def _device(raw):
    role,_,name=str(raw.get('type','')).partition(': ')
    return {'id':raw.get('id'),'name':raw.get('name'),'role':role,'deviceName':name or None}

def _clip(raw):
    notes=raw.get('notes')
    return {'id':raw.get('id'),'name':raw.get('name'),'kind':raw.get('type'),'view':raw.get('view'),
            'start':to_beats(raw.get('arrangementStart')),'length':_length_beats(raw.get('length')),
            'span':_length_beats(raw.get('arrangementLength',raw.get('length'))),
            'offset':to_beats(raw.get('start','1|1')),'looping':bool(raw.get('looping')),
            'timeSignature':raw.get('timeSignature','4/4'),
            'notes':parse_notes(notes) if isinstance(notes,str) else notes}

def _transport(raw):
    loop=raw.get('arrangementLoop')
    return {'playing':bool(raw.get('playing')),
            'loop':{'start':to_beats(loop.get('start')),'end':to_beats(loop.get('end'))} if loop else None}

TRACK_FIELDS={'name':'name','gainDb':'gainDb','mute':'mute','solo':'solo','arm':'arm','monitoring':'monitoringState'}
READ_ASPECTS={'mixer':'mixer','devices':'devices','clips':'arrangement-clips','session-clips':'session-clips',
              'timing':'timing','routing':'routings','available-routing':'available-routings','sample':'sample',
              'params':'params','param-values':'param-values','options':'options','chains':'chains',
              'return-chains':'return-chains','drum-pads':'drum-pads','tracks':'tracks','locators':'locators'}

def _include(aspects):
    unknown=[a for a in aspects if a not in READ_ASPECTS]
    if unknown:raise AdapterCapabilityError('read '+', '.join(unknown),'unknown read aspect')
    return [READ_ASPECTS[a] for a in aspects]

# ---- Scrunch host-object schema v1 (device, synth and analysis layers) ----
# These layers read tracks, devices, parameters and routing as plain records.
# Field names and value formats intentionally equal those already persisted in
# instrument identity signatures, synth calibration tables and change journals,
# so stored confirmations stay valid. A backend must produce exactly these
# fields; anything else a host reports is dropped at this boundary.
# Enumerations: track state in {active, muted, soloed, muted-via-solo,
# muted-also-via-solo} (optionally with 'armed'); device type is
# "<role>: <class display name>" or a bare rack role such as "instrument-rack".
HOST_SCHEMA={
    'session':(('name','tempo','timeSignature','scale','scalePitches','sceneCount','regularTrackCount','returnTrackCount'),
               {'tracks':'track[]','returnTracks':'track[]','masterTrack':'track','locators':'locator[]'}),
    'track':(('id','type','name','trackIndex','returnTrackIndex','path','color','state','gainDb','pan','panningMode','leftPan','rightPan',
              'instrument','isArmed','isGroup','isGroupMember','groupId','isFrozen','frozen','monitoringState','deviceCount',
              'arrangementClipCount','sessionClipCount','playingSlotIndex'),
             {'devices':'device[]','arrangementClips':'clipRef[]','sessionClips':'clipRef[]','sends':'send[]',
              'inputRoutingType':'route','inputRoutingChannel':'route','outputRoutingType':'route','outputRoutingChannel':'route',
              'availableInputRoutingTypes':'route[]','availableInputRoutingChannels':'route[]',
              'availableOutputRoutingTypes':'route[]','availableOutputRoutingChannels':'route[]'}),
    'device':(('id','name','type','path','deactivated','deviceCount'),
              {'parameters':'parameter[]','chains':'chain[]','returnChains':'chain[]','drumPads':'drumPad[]','devices':'device[]',
               'options':'deviceOptions','macros':'macros','variations':'variations'}),
    'chain':(('id','name','path','type','color','state','deviceCount'),{'devices':'device[]'}),
    'drumPad':(('id','note','pitch','name','state','hasInstrument'),{'chains':'chain[]'}),
    'parameter':(('id','name','value','min','max','unit','options','state','enabled'),{}),
    'deviceOptions':(('sidechainSourceTrackIds','sidechainChannels'),{}),
    'macros':(('count','hasMappings'),{}),
    'variations':(('count','selected'),{}),
    'clipRef':(('id','name','type','view','path','color','arrangementStart','arrangementLength','start','end','length',
                'looping','timeSignature','sampleFile'),{}),
    'send':(('return','gainDb'),{}),
    'route':(('name','inputId','outputId'),{}),
    'locator':(('id','name','time'),{})}

def project(kind,value):
    """Keep only schema fields; nested records are projected recursively."""
    many=kind.endswith('[]');kind=kind.rstrip('[]')
    if many:
        return [project(kind,v) for v in value] if isinstance(value,list) else value
    if not isinstance(value,dict):return value
    scalars,nested=HOST_SCHEMA[kind]
    out={k:value[k] for k in scalars if k in value}
    out.update({k:project(t,value[k]) for k,t in nested.items() if k in value})
    return out

PRODUCER_PAL_DEVICE_GAP=('Producer Pal has no stable parameter ids, internal-unit ranges or verified single-parameter '
    'writes; its name/display-string device methods (read_device, set_device_parameters) remain available.')

class ProducerPalAdapter:
    name='producer-pal'
    # Producer Pal runs inside the Set as this device, so rehearsal templates must keep its track.
    SET_HOST_DEVICE='Producer_Pal.amxd'
    def capabilities(self):
        return {'read_session':'partial','read_track':'partial','read_device':'partial',
                'read_markers':'supported','execute_transaction':'unverified',
                'read_automation':'unsupported','write_automation':'unsupported',
                'read_compensated_latency':'unsupported','group_tracks':'unverified',
                'complete_routing_latency':'unverified',
                'plugin_parameters':'unverified',
                'fractional_clip_length':'supported',
                **{op:'unsupported' for op in DEVICE_REQUIREMENTS|AUTOMATION_REQUIREMENTS|SPAN_PLAYBACK},
                **{gap:'unsupported' for gap in DEVICE_CONTRACT_GAPS}}
    def supports(self,requirement):
        return requirement in REQUIREMENTS and requirement not in DEVICE_REQUIREMENTS|AUTOMATION_REQUIREMENTS|SPAN_PLAYBACK|AUDIO_TO_MIDI
    def identity_scope(self):
        import fresh_session
        epoch=live_process_epoch()
        path=fresh_session.current_path()
        # PID plus launch time avoids accepting a reused process ID.
        return {'projectPath':str(path),'hostEpoch':epoch}

    # ---- Scrunch contract: reads ----
    def heartbeat(self):
        r=call('ppal-connect',{},timeout=2,log=False)
        s=r.get('liveSet') if isinstance(r,dict) else None
        if not isinstance(s,dict):raise ValueError('Invalid Live heartbeat')
        return {k:s.get(k) for k in ('name','tempo','timeSignature')}
    def connect(self):
        s=call('ppal-connect',{}).get('liveSet',{})
        return {'name':s.get('name'),'tempo':s.get('tempo'),'timeSignature':s.get('timeSignature')}
    def session(self):
        raw=call('ppal-read-live-set',{'include':['tracks']})
        return {'name':raw.get('name'),'tempo':raw.get('tempo'),'timeSignature':raw.get('timeSignature','4/4'),'tracks':[_track(t) for t in raw.get('tracks',[])]}
    def track(self,ident,clips=False,devices=False,mixer=False,routing=False):
        include=[n for flag,n in ((clips,'arrangement-clips'),(clips,'timing'),(devices,'devices'),(mixer,'mixer'),(routing,'routings')) if flag]
        return _track(call('ppal-read-track',{'id':ident,'include':include}))
    def clip(self,ident,notes=True):
        return _clip(call('ppal-read-clip',{'id':ident,'include':['notes','timing'] if notes else []}))
    def selected_track(self):return call('ppal-select',{}).get('selectedTrack',{}).get('id')
    def transport(self):return _transport(call('ppal-playback',{'action':'update-arrangement'}))
    playback_state=transport

    # ---- Scrunch contract: writes (validated before the host is contacted) ----
    def update_track(self,ident,**values):
        unknown=set(values)-set(TRACK_FIELDS)
        if unknown:raise AdapterCapabilityError('update track '+', '.join(sorted(unknown)),'field is not in the Scrunch contract')
        if 'monitoring' in values and values['monitoring']!='off':raise AdapterCapabilityError('set monitoring','only off is verified')
        return call('ppal-update-track',{'id':ident,**{TRACK_FIELDS[k]:v for k,v in values.items()}})
    def create_midi_track(self,name,index=None,mute=True,arm=False):
        args={'type':'midi','name':name,'mute':mute,'arm':arm}
        if index is not None:args.update(count=1,trackIndex=index)
        r=call('ppal-create-track',args)
        return {'id':r['id'],'trackIndex':r.get('trackIndex')}
    def create_muted_midi_track(self,name):return self.create_midi_track(name)
    def duplicate_track(self,ident,name,clips=False,devices=True):
        r=call('ppal-duplicate',{'id':ident,'type':'track','count':1,'withoutClips':not clips,'withoutDevices':not devices,'name':name})
        return {'id':r['id']}
    def create_arrangement_clip(self,index,length,notes,start=0,name='Original phrase - Scrunch',looping=False):
        args={'path':f't{index}','arrangementStart':to_position(start),'length':_host_length(length),
              'looping':looping,'name':name,'notes':json.dumps(notes)}
        return {'id':call('ppal-create-clip',args)['id']}
    def replace_clip_notes(self,ident,notes,name=None):
        args={'id':ident,'preTransforms':'delete','notes':json.dumps(notes)}
        if name is not None:args['name']=name
        return call('ppal-update-clip',args)
    def select_track(self,ident):return call('ppal-select',{'id':ident,'view':'arrangement'})
    def select_clip(self,ident):
        return call('ppal-select',{'id':ident,'view':'arrangement'}).get('selectedClip',{}).get('id')==ident
    def open_plugin_window(self,device_id,arrangement=True):
        args={'id':device_id,'openPluginWindow':True}
        if arrangement:args['view']='arrangement'
        return call('ppal-select',args)
    def stop_playback(self):return _transport(call('ppal-playback',{'action':'stop'}))
    def stop_session_clips(self):return call('ppal-playback',{'action':'stop-all-session-clips'})
    def play_arrangement(self,start,loop=None):
        args={'action':'play-arrangement','startTime':to_position(start),'loop':loop is not None}
        if loop is not None:args.update(loopStart=to_position(loop[0]),loopEnd=to_position(loop[1]))
        return _transport(call('ppal-playback',args))
    def set_arrangement_loop(self,enabled,start,end):
        return _transport(call('ppal-playback',{'action':'update-arrangement','loop':enabled,'loopStart':to_position(start),'loopEnd':to_position(end)}))
    def move_playhead(self,start):
        return _transport(call('ppal-playback',{'action':'update-arrangement','startTime':to_position(start)}))
    def delete_track(self,ident):return call('ppal-delete',{'type':'track','id':ident})
    def delete_clip(self,ident):return call('ppal-delete',{'type':'clip','id':ident})

    # ---- Host-schema reads for device/synth/analysis layers (see HOST_SCHEMA) ----
    def read_session(self):
        import engine
        return engine.live_set()
    def read_live_set(self,*aspects):return project('session',call('ppal-read-live-set',{'include':_include(aspects or ('tracks',))}))
    def read_track(self,ident):
        return project('track',call('ppal-read-track',{'id':ident,'include':['mixer','devices','arrangement-clips','timing','routings']}))
    def read_track_detail(self,ident,*aspects):return project('track',call('ppal-read-track',{'id':ident,'include':_include(aspects)}))
    def read_markers(self):return self.read_live_set('locators')
    def read_device(self,ident):
        return project('device',call('ppal-read-device',{'id':ident,'include':['params','param-values','options']}))
    def read_device_detail(self,ident=None,path=None,aspects=(),depth=None):
        args={'id':ident} if ident else {'path':path}
        if aspects:args['include']=_include(aspects)
        if depth is not None:args['maxDepth']=depth
        return project('device',call('ppal-read-device',args))
    def read_rack(self,ident):
        return project('device',call('ppal-read-device',{'id':ident,'include':['params','param-values','chains'],'maxDepth':2}))

    # ---- Device writes (synth layer; unchanged wire behaviour) ----
    def save_checkpoint(self):
        import fresh_session
        return fresh_session.save_current()
    def checkpoint_rack(self,ident):
        import time
        before=self.read_rack(ident);index=before.get('variations',{}).get('count',0)
        call('ppal-update-device',{'id':ident,'macroVariation':'create'})
        for _ in range(20):
            if self.read_rack(ident).get('variations',{}).get('count')==index+1:
                self.save_checkpoint();return index
            time.sleep(.1)
        raise ValueError('Rack checkpoint creation was not verified; do not retry blindly.')
    def set_rack_parameter(self,ident,name,value):
        return call('ppal-update-device',{'id':ident,'params':[{'name':name,'value':str(value)}]})
    def set_rack_parameters(self,ident,params):
        return call('ppal-update-device',{'id':ident,'params':[{'name':k,'value':str(v)} for k,v in params.items()]})
    def store_rack_variation(self,ident):
        """Append one macro variation without saving the Set (checkpoint_rack also saves)."""
        return call('ppal-update-device',{'id':ident,'macroVariation':'create'})
    def restore_rack(self,ident,index):
        if self.read_rack(ident).get('variations',{}).get('count',0)<=index:
            raise ValueError('Saved Rack variation is missing.')
        return call('ppal-update-device',{'id':ident,'macroVariation':'load','macroVariationIndex':index})
    def create_native_effect(self,index,effect,name,params,at=None):
        if at is not None:
            raise AdapterCapabilityError('create native device at a position','Producer Pal appends devices; use the Scrunch bridge')
        return call('ppal-create-device',{'deviceName':effect,'path':f't{index}','name':name,'params':[{'name':k,'value':str(v)} for k,v in params.items()]})
    def set_device_parameters(self,ident,params):
        return call('ppal-update-device',{'id':ident,'params':[{'name':k,'value':str(v)} for k,v in params.items()]})
    def delete_device(self,ident):return call('ppal-delete',{'type':'device','id':ident})
    # ---- Scrunch-owned native device contract: not served by Producer Pal ----
    def _native_device(self,*args,**kwargs):
        raise AdapterCapabilityError('use the native device contract',PRODUCER_PAL_DEVICE_GAP)
    devices=device=device_parameter=set_device_parameter=_native_device
    def read_automation(self,*args,**kwargs):
        raise AdapterCapabilityError('read_automation',CONTRACT_GAPS['read_automation'])
    def write_automation(self,*args,**kwargs):
        raise AdapterCapabilityError('write_automation',CONTRACT_GAPS['write_automation'])
    def read_compensated_latency(self,*args,**kwargs):
        raise AdapterCapabilityError('read_compensated_latency',CONTRACT_GAPS['read_compensated_latency'])
    def limitations(self):
        return list(CONTRACT_GAPS.values())+['Host reads are not atomic.',
                'Producer Pal 2.2.0 / Live 12.3.5 compatibility may affect PluginDevice reads; Device On alone is not definitive raw parameter evidence.',
                'Extensions are a future candidate only. No beta installation or migration is active.',
                PRODUCER_PAL_DEVICE_GAP]+list(DEVICE_CONTRACT_GAPS.values())

BACKENDS=('producer-pal','remote-script')

def select_backend(name=None):
    """Explicit, single backend choice made at startup.

    ScrunchBridge (remote-script) is the default since its lab verification on
    17 Sep 2026. Producer Pal stays available only when chosen explicitly
    (SCRUNCH_DAW_BACKEND=producer-pal). There is deliberately no fallback chain: a
    failed or uncertain mutation is never retried through the other backend."""
    import os
    name=name or os.environ.get('SCRUNCH_DAW_BACKEND') or 'remote-script'
    if name=='producer-pal':return ProducerPalAdapter()
    if name=='remote-script':
        import daw_remote_script
        return daw_remote_script.RemoteScriptAdapter()
    raise ValueError('Unknown DAW backend %r; choose one of %s.'%(name,', '.join(BACKENDS)))

def __getattr__(name):
    """Create DEFAULT on first use (PEP 562). Selecting at import time made
    `import daw_remote_script` fail: it imports this module, whose selection then
    imported the half-initialised daw_remote_script."""
    if name=='DEFAULT':
        global DEFAULT
        DEFAULT=select_backend()
        return DEFAULT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
