"""Scrunch client for the owned Ableton Remote Script bridge.

Implements the daw_adapter Scrunch contract over the loopback protocol served by
native/remote_script/ScrunchBridge. It is Scrunch's default backend (see
daw_adapter.select_backend); Producer Pal is an explicit fallback only.

Unsupported operations raise AdapterCapabilityError before any request is sent.
A write that times out or loses its connection is never retried: it may have
executed, so callers must inspect Live first.
"""
import json,math,os,socket,threading,time,uuid
import daw_adapter,music
from daw_adapter import AdapterCapabilityError,DeviceParameterConflict,DeviceParameterUnverified

CONFIG_PATH=os.path.expanduser('~/Library/Application Support/Scrunch/remote-script-bridge.json')
PROTOCOL=1

# Evidence level per native device operation: exercised in Live 12.3.5 with bridge
# 2026-09-17.4 in a manual step-by-step run (evidence/bridge-lab/
# BRIDGE-DEVICES-MANUAL-SUMMARY-2026-09-17.md), not a formal runner pass.
DEVICE_EVIDENCE='manually-verified'
SUPPORTED=('connect','heartbeat','session','track','clip','selected_track','transport','update_track',
           'create_midi_track','create_arrangement_clip','delete_clip','delete_track','stop_playback',
           'stop_session_clips','play_arrangement','set_arrangement_loop','move_playhead',
           'duplicate_track','replace_clip_notes','select_track','select_clip','open_plugin_window',
           'read_session','read_live_set','read_track','read_track_detail','read_markers','read_device',
           'read_device_detail','read_rack','save_checkpoint','checkpoint_rack','set_rack_parameter',
           'set_rack_parameters','store_rack_variation','restore_rack','create_native_effect','set_device_parameters','delete_device','browser_find','load_device','reload_bridge',
           'automation_target','record_automation','automation_take_status','stop_automation_take','undo_automation_take','sample_automation',
           'play_span')
# Exercised through real Scrunch workflows in Live 12.3.5 with bridge 2026-09-18.x, with
# readback and cleanup (evidence/bridge-lab/e2e-20260917, e2e-20260917b and e2e-20260917c
# SUMMARY.md; run C added select_clip, read_device_detail and looping clip creation).
LAB_VERIFIED=frozenset({'connect','heartbeat','session','track','clip','selected_track','transport','update_track',
    'create_midi_track','create_arrangement_clip','delete_clip','delete_track','stop_playback','stop_session_clips',
    'play_arrangement','set_arrangement_loop','move_playhead','duplicate_track','replace_clip_notes','select_track','select_clip',
    'read_session','read_live_set','read_track','read_track_detail','read_markers','read_device','read_device_detail','read_rack',
    'save_checkpoint','checkpoint_rack','set_rack_parameter','set_rack_parameters','store_rack_variation','restore_rack',
    'create_native_effect','set_device_parameters','delete_device','open_plugin_window',
    # Arrangement automation, live in Live 12.3.5 on 26 Sep 2026 (evidence/automation-envelopes-2026-09-26/SUMMARY.md).
    'automation_target','record_automation','automation_take_status','undo_automation_take','sample_automation'})
UNSUPPORTED={
    'session_clip_reads':'Session clip records, Simpler sample paths and available routing lists are not provided (Arrangement workflows only).',
    'drum_rack_reads':'Drum Rack pads and chains are not modelled.',
    'specialized_device_parameters':'Devices Producer Pal 2.2.0 models with device-specific parameters (Compressor, Drift, EQ Eight, Hybrid Reverb, Meld, Auto Filter, Auto Pan-Tremolo, Phaser-Flanger, Roar, Simpler, Spectral Resonator, Wavetable) are refused for name-based reads and writes; use device/set_device_parameter.',
    **daw_adapter.CONTRACT_GAPS,**daw_adapter.DEVICE_CONTRACT_GAPS}

def _lab_document_name():
    import fresh_session
    try:return fresh_session.read_current_path().stem
    except ValueError as exc:
        # A new Set that has never been saved has no file yet; Scrunch still works in it.
        if 'no open Set window' in str(exc):return 'Untitled'
        raise

def _number(value,what):
    if type(value) not in (int,float) or not math.isfinite(value):raise ValueError(f'Ableton bridge returned an invalid {what}.')
    return int(value) if float(value).is_integer() else value

def _position(value,what):
    return None if value is None else _number(value,what)

def _optional_number(value,what):
    return None if value is None else _number(value,what)

PARAMETER_FIELDS=('id','index','name','originalName','quantized','options','enabled','state','automation','display','unavailable','deviceId')
def _parameter(p):
    if not isinstance(p,dict) or not isinstance(p.get('id'),str):raise ValueError('Ableton bridge returned an invalid parameter.')
    out={k:p.get(k) for k in PARAMETER_FIELDS if k in p}
    for k in ('value','min','max','defaultValue','previousValue'):
        if k in p:out[k]=_optional_number(p[k],'parameter '+k)
    return out

def _device(d):
    if not isinstance(d,dict) or not isinstance(d.get('id'),str):raise ValueError('Ableton bridge returned an invalid device.')
    out={k:d[k] for k in ('id','trackId','path','name','className','deviceName','role','plugin','active','parameterCount','unavailable','truncated') if k in d}
    for kind in ('chains','returnChains'):
        if kind in d:
            out[kind]=[{'id':c.get('id'),'name':c.get('name'),'path':c.get('path'),'devices':[_device(x) for x in c.get('devices',[])]} for c in d[kind]]
    if 'parameters' in d:out['parameters']=[_parameter(p) for p in d['parameters']]
    return out

class RemoteScriptAdapter:
    name='ableton-remote-script'
    SET_HOST_DEVICE=None  # a control surface; no device has to travel with the Set
    def __init__(self,config_path=CONFIG_PATH,set_name=_lab_document_name,read_timeout=5.0,write_timeout=10.0):
        self._config_path=config_path;self._set_name=set_name
        self._read_timeout=read_timeout;self._write_timeout=write_timeout
        self._lock=threading.Lock();self._sock=None;self._file=None;self._token=None

    # ---- transport ----
    def _config(self):
        try:
            with open(self._config_path) as fh:cfg=json.load(fh)
        except (OSError,ValueError):
            raise ValueError('Ableton bridge is not running. Select the Scrunch Bridge control surface in Live.') from None
        if (cfg.get('protocol')!=PROTOCOL or cfg.get('host')!='127.0.0.1' or type(cfg.get('port')) is not int
                or not isinstance(cfg.get('token'),str) or len(cfg['token'])<32):
            raise ValueError('Ableton bridge configuration is incompatible with this Scrunch version.')
        return cfg
    def _open(self):
        cfg=self._config()
        try:self._sock=socket.create_connection((cfg['host'],cfg['port']),timeout=self._read_timeout)
        except OSError:
            raise ValueError('Ableton bridge is not reachable. Check that Live is open with the Scrunch Bridge control surface selected.') from None
        self._file=self._sock.makefile('rb');self._token=cfg['token']
    def close(self):
        with self._lock:self._drop()
    def _drop(self):
        for part in (self._file,self._sock):
            try:
                if part:part.close()
            except OSError:pass
        self._sock=self._file=None
    def _request(self,op,args=None,write=False):
        rid=uuid.uuid4().hex;payload=None
        with self._lock:
            for attempt in (1,2):
                if self._sock is None:self._open()
                payload={'v':PROTOCOL,'id':rid,'token':self._token,'op':op,'args':args or {}}
                try:
                    self._sock.settimeout(self._write_timeout if write else self._read_timeout)
                    self._sock.sendall((json.dumps(payload)+'\n').encode())
                    line=self._file.readline()
                except socket.timeout:
                    self._drop()
                    raise ValueError(f'Ableton bridge did not answer {op} in time.'+(' The change may have been made; inspect Live before retrying.' if write else '')) from None
                except OSError:
                    self._drop()
                    if write or attempt==2:
                        raise ValueError(f'Ableton bridge connection was lost during {op}.'+(' The change may have been made; inspect Live before retrying.' if write else '')) from None
                    continue  # a stale idle connection; reads are safe to resend once
                if not line:
                    self._drop()
                    if write or attempt==2:
                        raise ValueError(f'Ableton bridge closed the connection during {op}.'+(' The change may have been made; inspect Live before retrying.' if write else ''))
                    continue
                break
        try:
            response=json.loads(line.decode())
        except ValueError:
            self.close();raise ValueError('Ableton bridge returned malformed data.') from None
        if not isinstance(response,dict) or response.get('id')!=rid or response.get('v')!=PROTOCOL:
            self.close();raise ValueError('Ableton bridge reply did not match the request.')
        if response.get('ok') is True:return response.get('result')
        error=response.get('error') if isinstance(response.get('error'),dict) else {}
        code,message=error.get('code'),str(error.get('message','Unknown bridge error.'))[:300]
        if code=='unsupported':raise AdapterCapabilityError(op,message)
        if code=='unauthorized':self.close();raise ValueError('Ableton bridge rejected Scrunch\'s credentials. Reselect the control surface to refresh them.')
        if code=='not_found':raise ValueError(message)
        if code=='conflict':raise DeviceParameterConflict(message)
        if code=='unverified':raise DeviceParameterUnverified(message)
        raise ValueError('Ableton bridge: '+message)
    def _unsupported(self,capability):
        raise AdapterCapabilityError(capability,UNSUPPORTED[capability])

    # ---- identity and capabilities ----
    MISSING=frozenset()
    def supports(self,requirement):
        return requirement in daw_adapter.REQUIREMENTS and requirement not in self.MISSING
    def capabilities(self):
        return {**{op:'lab-verified' if op in LAB_VERIFIED else 'unverified' for op in SUPPORTED},
                **{op:DEVICE_EVIDENCE for op in daw_adapter.DEVICE_REQUIREMENTS},
                **{k:'unsupported' for k in UNSUPPORTED}}
    def limitations(self):
        return ['lab-verified operations ran inside real Scrunch workflows in Live 12.3.5 with readback (17 Sep 2026, runs A-C), including select_clip (detail clip read back), read_device_detail (regular, return and master tracks; rack chains by id or path) and looping clip creation. Lab verification is not John\'s musical acceptance.',
                'Parameter values in host-schema reads and name-based writes follow Producer Pal 2.2.0 label rules (purely numeric labels as Max single-precision atoms); display-value searches choose the closest displayed step.',
                'Plug-in windows use ordinary UI: the bridge selects the device, native/live_plugin_window presses its own header toggle through macOS Accessibility (Live is brought to the front) and the "<device>/<track>" window title is required. It needs Accessibility permission for the calling process and refuses when the shown chain has zero or several matching toggles.',
                'The four device operations (devices, device, device_parameter, set_device_parameter) are in the daw_adapter contract, offline-tested, and manually exercised in Live 12.3.5 (4 parameters on Auto Filter, Serum 2 and Valhalla Supermassive) in a manual run, not a formal LAB-DEVICE-TEST.md runner pass.',
                'Refusals other than a wrong parameter name (automated, disabled, stale expect_value, out of range) are tested offline only.',
                'Device coverage verified in Live is limited to the tested devices (Auto Filter, Delay, Reverb, Instrument Rack chains, Serum 2, Jup-8000 V, Valhalla Supermassive); other native devices and plug-ins are unverified, and plug-ins expose only the parameters Live lists for them.',
                'Live exposes no Set name to Remote Scripts; the document name comes from the Live window.',
                'Host reads are not atomic.',
                'Arrangement automation is recorded, not drawn: Live 12.3.5 has no track envelope API (Arrangement clips refuse envelopes), so record_automation records the planned values on Live\'s clock once per display tick (the ramp starts up to ~1.5 ticks early); sample_automation reads values by locating the playhead; breakpoints cannot be listed or deleted, and only Scrunch\'s own latest recording can be taken back (Live undo plus putting back the pre-recording value). stop_automation_take is tested offline only.']+list(UNSUPPORTED.values())
    def identity_scope(self):
        import fresh_session
        return {'projectPath':str(fresh_session.current_path()),'hostEpoch':daw_adapter.live_process_epoch()}

    # ---- contract reads ----
    def hello(self):return self._request('hello')
    def heartbeat(self):return self.connect()
    def document_path(self):
        """The open Set's file path as Live reports it, or '' for an unsaved Set. No window reads, no name check."""
        return str(self._request('connect').get('filePath') or '')
    def connect(self):
        r=self._request('connect')
        return {'name':self._set_name(),'tempo':_number(r.get('tempo'),'tempo'),'timeSignature':str(r.get('timeSignature'))}
    def _track(self,t):
        if not isinstance(t,dict) or not isinstance(t.get('id'),str):raise ValueError('Ableton bridge returned an invalid track.')
        out={k:t.get(k) for k in ('id','trackIndex','name','kind','muted','soloed','mutedViaSolo','arrangementClipCount','sessionClipCount')}
        if isinstance(t.get('color'),int):out['color']=t['color']      # Live's own track colour, for the scope's lanes
        for k in ('instrument','devices','mainOutput','gainDb'):
            if k in t:out[k]=t[k]
        if 'clips' in t:
            out['clips']=[{'id':c['id'],'name':c.get('name'),'start':_position(c.get('start'),'clip start'),
                           'span':_position(c.get('span'),'clip span')} for c in t['clips']]
        return out
    def session(self):
        r=self._request('session')
        return {'name':self._set_name(),'tempo':_number(r.get('tempo'),'tempo'),'timeSignature':r.get('timeSignature','4/4'),'tracks':[self._track(t) for t in r.get('tracks',[])]}
    def track(self,ident,clips=False,devices=False,mixer=False,routing=False):
        return self._track(self._request('track',{'id':ident,'clips':clips,'devices':devices,'mixer':mixer,'routing':routing}))
    def clip(self,ident,notes=True):
        c=self._request('clip',{'id':ident,'notes':notes})
        if not isinstance(c,dict) or c.get('id')!=ident:raise ValueError('Ableton bridge returned a different clip.')
        for k in ('start','length','span','offset'):c[k]=_position(c.get(k),'clip '+k)
        if c.get('notes') is not None:
            c['notes']=[{'p':int(n['p']),'t':_number(n['t'],'note time'),'d':_number(n['d'],'note duration'),'v':int(n['v'])} for n in c['notes']]
        return c
    def meter(self,track,ticks=12):
        """Track output level over about `ticks`/10 seconds. Only meaningful while the track is playing."""
        r=self._request('meter',{'track':track,'ticks':int(ticks)})
        return {'peak':_number(r.get('peak'),'meter peak'),'mean':_number(r.get('mean'),'meter mean'),
                'playing':bool(r.get('playing')),'samples':int(r.get('samples',0)),'basis':str(r.get('basis'))}
    def selected_track(self):return self._request('selected_track')
    def _transport(self,r):
        loop=r.get('loop')
        return {'playing':bool(r.get('playing')),'loop':None if not loop else {'start':_number(loop['start'],'loop start'),'end':_number(loop['end'],'loop end')}}
    def transport(self):return self._transport(self._request('transport'))
    playback_state=transport

    # ---- native devices and parameters (Scrunch-owned; values are Live internal units) ----
    def devices(self,track,parameters=False):
        r=self._request('devices',{'track':track,'parameters':bool(parameters)})
        if not isinstance(r,dict) or r.get('trackId')!=track:raise ValueError('Ableton bridge returned devices for a different track.')
        return [_device(d) for d in r.get('devices',[])]
    def device(self,ident,parameters=True):
        d=_device(self._request('device',{'id':ident,'parameters':bool(parameters)}))
        if d['id']!=ident:raise ValueError('Ableton bridge returned a different device.')
        return d
    def device_parameter(self,device,ident):
        p=_parameter(self._request('device_parameter',{'device':device,'id':ident}))
        if p['id']!=ident:raise ValueError('Ableton bridge returned a different parameter.')
        return p
    def set_device_parameter(self,device,ident,name,value=None,option=None,expect_value=None):
        """Write one parameter by its internal value (within its own min..max) or a named option.

        `name` must be the name read for that parameter; a renamed or shifted
        reference is refused instead of writing another control. Returns the
        verified readback including previousValue.
        """
        if (value is None)==(option is None):raise AdapterCapabilityError('set device parameter','give exactly one of value or option')
        if value is not None and (type(value) not in (int,float) or not math.isfinite(value)):raise AdapterCapabilityError('set device parameter','value must be a finite number in the parameter\'s own units')
        if option is not None and not isinstance(option,str):raise AdapterCapabilityError('set device parameter','option must be one of the parameter\'s named options')
        if not isinstance(name,str) or not name:raise AdapterCapabilityError('set device parameter','the expected parameter name is required')
        args={'device':device,'id':ident,'name':name}
        if value is not None:args['value']=value
        if option is not None:args['option']=option
        if expect_value is not None:args['expect_value']=expect_value
        p=_parameter(self._request('set_device_parameter',args,write=True))
        if p['id']!=ident or p['name']!=name:raise DeviceParameterUnverified('Ableton bridge confirmed a different parameter; inspect Live before retrying.')
        return p

    def set_device_parameters_together(self,device,writes,confirm=True):
        """Apply several knob changes to one device in a single Live tick; all verified, or none written.

        `writes`: [{id, name, value, expect_value?}] in the parameters' own units. Returns the
        verified readback for each. Use for one coordinated move; single edits keep set_device_parameter.
        """
        if not isinstance(writes,(list,tuple)) or not 1<=len(writes)<=64:
            raise AdapterCapabilityError('set device parameters','give between 1 and 64 writes')
        plan=[]
        for w in writes:
            value=w.get('value')
            if type(value) not in (int,float) or not math.isfinite(value):
                raise AdapterCapabilityError('set device parameters','each value must be a finite number in the parameter\'s own units')
            if not isinstance(w.get('name'),str) or not w['name']:
                raise AdapterCapabilityError('set device parameters','each write needs the expected parameter name')
            step={'id':w['id'],'name':w['name'],'value':value}
            if w.get('expect_value') is not None:step['expect_value']=w['expect_value']
            plan.append(step)
        r=self._request('set_device_parameters',{'device':device,'writes':plan,'confirm':bool(confirm)},write=True)
        if not confirm:return r          # applied on the next tick; confirm with verify_last_write()
        return [_parameter(p) for p in r.get('written',[])]
    def verify_last_write(self):
        """Confirm a grouped write sent with confirm=False; raises if the knobs did not take the values."""
        return [_parameter(p) for p in self._request('verify_last_write',write=True).get('written',[])]

    # ---- knob sweeps (sound coach): the bridge moves the knob each tick; stop returns the verified position ----
    def start_sweep(self,device,ident,name,target,seconds,expect_value=None):
        if type(target) not in (int,float) or not math.isfinite(target):raise AdapterCapabilityError('sweep a knob','target must be a finite number in the parameter\'s own units')
        if type(seconds) not in (int,float) or not 0<seconds<=120:raise AdapterCapabilityError('sweep a knob','seconds must be above 0 and at most 120')
        args={'device':device,'id':ident,'name':name,'target':target,'seconds':seconds}
        if expect_value is not None:args['expect_value']=expect_value
        return self._request('start_sweep',args,write=True)
    def sweep_status(self):return self._request('sweep_status')
    def stop_sweep(self):
        p=self._request('stop_sweep',write=True)
        return dict(_parameter(p),sweptFrom=p.get('sweptFrom'),sweptTo=p.get('sweptTo'),stoppedAt=p.get('stoppedAt'))

    # ---- contract writes (preflight before contacting Live) ----
    def update_track(self,ident,**values):
        unknown=set(values)-{'name','mute','solo','arm','gainDb','monitoring'}
        if unknown:raise AdapterCapabilityError('update track '+', '.join(sorted(unknown)),'field is not in the Scrunch contract')
        if 'gainDb' in values and (type(values['gainDb']) not in (int,float) or not math.isfinite(values['gainDb'])):
            raise AdapterCapabilityError('set track gain','gain must be a finite number of dB')
        if 'monitoring' in values and values['monitoring'] not in ('in','auto','off'):
            raise AdapterCapabilityError('set monitoring','monitoring must be in, auto or off')
        return self._request('update_track',{'id':ident,**values},write=True)
    def create_midi_track(self,name,index=None,mute=True,arm=False):
        return self._request('create_midi_track',{'name':name,'index':index,'mute':mute,'arm':arm},write=True)
    def create_muted_midi_track(self,name):return self.create_midi_track(name)
    def create_arrangement_clip(self,index,length,notes,start=0,name='Original phrase - Scrunch',looping=False):
        if type(length) not in (int,float) or not math.isfinite(length) or length<=0:raise AdapterCapabilityError('create clip','clip length must be positive beats')
        if type(start) not in (int,float) or not math.isfinite(start) or start<0:raise AdapterCapabilityError('address position','positions must be non-negative beats')
        return self._request('create_arrangement_clip',{'track_index':index,'length':length,'notes':notes,'start':start,'name':name,'looping':bool(looping)},write=True)
    def delete_clip(self,ident):return self._request('delete_clip',{'id':ident},write=True)
    def delete_track(self,ident):return self._request('delete_track',{'id':ident},write=True)
    def stop_playback(self):return self._transport(self._request('stop_playback',write=True))
    def stop_session_clips(self):return self._request('stop_session_clips',write=True)
    def play_arrangement(self,start,loop=None):
        return self._transport(self._request('play_arrangement',{'start':start,'loop':list(loop) if loop is not None else None},write=True))
    def set_arrangement_loop(self,enabled,start,end):
        return self._transport(self._request('set_arrangement_loop',{'enabled':enabled,'start':start,'end':end},write=True))
    def move_playhead(self,start):return self._transport(self._request('move_playhead',{'start':start},write=True))
    def play_span(self,start,end):
        """Loop [start, end) and play from start, verified by the playhead (the bridge locates, then continues:
        Live's start_playing ignores the playhead). The producer's loop comes back on stop_playback."""
        r=self._request('mix_play',{'start':float(start),'end':float(end)},write=True)
        return {'playing':bool(r.get('playing')),'beat':r.get('beat'),'loop':r.get('loop')}
    def duplicate_track(self,ident,name,clips=False,devices=True):
        r=self._request('duplicate_track',{'id':ident,'name':name,'clips':bool(clips),'devices':bool(devices)},write=True)
        return {'id':r['id'],'trackIndex':r.get('trackIndex')}
    def replace_clip_notes(self,ident,notes,name=None):
        return self._request('replace_clip_notes',{'id':ident,'notes':notes,'name':name},write=True)
    def select_track(self,ident):return self._request('select_track',{'id':ident},write=True)
    def select_clip(self,ident):
        return self._request('select_clip',{'id':ident},write=True).get('selectedClip')==ident
    PLUGIN_WINDOW_HELPER=os.path.join(os.path.dirname(os.path.abspath(__file__)),'native','live_plugin_window')
    def open_plugin_window(self,device_id,arrangement=True):
        """Select the device through the bridge, then press its own header toggle (native AX helper)
        and require a Live window titled '<device>/<track>'."""
        r=self._request('open_plugin_window',{'device':device_id,'open':True},write=True)
        if r.get('editor')!='ui-required':return r
        import subprocess
        title=f"{r['deviceName']}/{r['trackName']}";last=''
        for _ in range(3):  # the device chain can take a moment to appear after selection
            time.sleep(.4)
            done=subprocess.run([self.PLUGIN_WINDOW_HELPER,'open',r['deviceName'],title],capture_output=True,text=True,timeout=15)
            if done.returncode==0:return dict(r,window=title,result=done.stdout.strip())
            last=done.stderr.strip()
            if 'found 0 of' not in last:break
        raise ValueError('Plug-in window was not verified: '+last[:300])

    # ---- host-schema reads for device, synth and analysis layers (daw_adapter.HOST_SCHEMA) ----
    # The bridge applies Producer Pal 2.2.0's value/label rules on Live's objects;
    # this client converts beat positions to the host text forms and projects.
    @staticmethod
    def _clip_refs(track):
        for c in track.get('arrangementClips',[]):
            start,span,length=c.pop('arrangementStartBeats'),c.pop('arrangementLengthBeats'),c.pop('lengthBeats')
            c['arrangementStart']=music.position_text(start)
            if span>0:c['arrangementLength']=music.duration_text(span)
            if length>0:c['length']=music.duration_text(length)
        return track
    def _aspects(self,aspects):
        return daw_adapter._include(aspects)
    def read_session(self):
        import engine
        engine.require_lab(self)
        return self.read_live_set('tracks')
    def read_live_set(self,*aspects):
        r=self._request('live_set',{'include':self._aspects(aspects or ('tracks',))})
        r['name']=self._set_name()
        for key in ('tracks','returnTracks'):
            for t in r.get(key,[]):self._clip_refs(t)
        if r.get('masterTrack'):self._clip_refs(r['masterTrack'])
        for loc in r.get('locators',[]):loc['time']=music.position_text(loc.pop('timeBeats'))
        return daw_adapter.project('session',r)
    def read_track(self,ident):
        return self.read_track_detail(ident,'mixer','devices','clips','timing','routing')
    def read_track_detail(self,ident,*aspects):
        return daw_adapter.project('track',self._clip_refs(self._request('host_track',{'id':ident,'include':self._aspects(aspects)})))
    def read_markers(self):return self.read_live_set('locators')
    def read_device(self,ident):
        return daw_adapter.project('device',self._request('host_device',{'id':ident,'include':['params','param-values','options']}))
    def read_device_detail(self,ident=None,path=None,aspects=(),depth=None):
        # Same precedence as Producer Pal: the id when given, otherwise the path.
        if not ident and not path:raise AdapterCapabilityError('read device','give an id or a path')
        args={'id':ident} if ident else {'path':path}
        args.update(include=self._aspects(aspects),max_depth=depth or 0)
        return daw_adapter.project('device',self._request('host_device',args))
    def read_rack(self,ident):
        return daw_adapter.project('device',self._request('host_device',{'id':ident,'include':['params','param-values','chains'],'max_depth':2}))

    # ---- Producer Pal-shaped device writes (name + display value text) ----
    def save_checkpoint(self):
        import fresh_session
        return fresh_session.save_current()
    def _params(self,params):
        if not isinstance(params,dict) or not params:raise AdapterCapabilityError('set device parameters','give at least one {name: value}')
        return {str(k):str(v) for k,v in params.items()}
    def set_device_parameters(self,ident,params):
        return self._request('set_parameters_by_name',{'device':ident,'params':self._params(params)},write=True)
    def set_rack_parameters(self,ident,params):return self.set_device_parameters(ident,params)
    def set_rack_parameter(self,ident,name,value):return self.set_device_parameters(ident,{name:value})
    def checkpoint_rack(self,ident):
        before=self.read_rack(ident);index=before.get('variations',{}).get('count',0)
        self._request('rack_variation',{'device':ident,'action':'create'},write=True)
        for _ in range(20):
            if self.read_rack(ident).get('variations',{}).get('count')==index+1:
                self.save_checkpoint();return index
            time.sleep(.1)
        raise ValueError('Rack checkpoint creation was not verified; do not retry blindly.')
    def store_rack_variation(self,ident):
        return self._request('rack_variation',{'device':ident,'action':'create'},write=True)
    def restore_rack(self,ident,index):
        if self.read_rack(ident).get('variations',{}).get('count',0)<=index:
            raise ValueError('Saved Rack variation is missing.')
        return self._request('rack_variation',{'device':ident,'action':'load','index':index},write=True)
    def create_native_effect(self,index,effect,name,params,at=None):
        """Insert a native device on a track; `at` places it at that position in the chain.

        `at` exists because a tap only hears what is in front of it - an EQ appended after the Scope
        plug-in cannot be verified by measurement. Refused rather than appended to the wrong place if
        this Live version will not position it.
        """
        if at is not None and (type(at) is not int or at<0):
            raise AdapterCapabilityError('create native device','the insertion position must be a whole number, 0 or more')
        args={'track_index':index,'device_name':effect,'name':name,
              'params':self._params(params) if params else None}
        if at is not None:args['index']=at
        r=self._request('create_native_device',args,write=True)
        return {'id':r['id'],'deviceIndex':r.get('deviceIndex'),'positioned':r.get('positioned',False)}
    def import_audio(self,track,path,start=0.0):
        """Put an audio file on an audio track's Arrangement at beat `start`; refuses an occupied spot."""
        return self._request('mix_import_audio',{'track':track,'path':str(path),'start':float(start)},write=True)
    def add_locators(self,items):
        """Add named Arrangement locators [{beat,name}]; existing ones at the same beat are kept untouched."""
        saved=self._write_timeout;self._write_timeout=max(saved,10.0+1.5*len(items))   # ~1.1 s per locator measured 8 Oct
        try:return self._request('mix_add_locators',{'items':list(items)},write=True)
        finally:self._write_timeout=saved
    def reload_bridge(self):
        """Re-import the bridge's code in the running Live and rebind it - no restart.

        Live caches Remote Script modules at startup, and toggling the control surface only makes a
        new instance of the old code (measured 20 Sep 2026). This is the only way to pick up a change
        without costing the producer their session.
        """
        return self._request('reload',{},write=True)
    def browser_find(self,name,shelves=None,limit=20):
        """Loadable browser items matching `name` - plug-ins and presets included. Loads nothing."""
        args={'name':name,'limit':int(limit)}
        if shelves:args['shelves']=list(shelves)
        return self._request('browser_find',args)
    def plugin_catalog(self,device):
        return self._request('plugin_catalog',{'device':device})

    def cc_control_info(self,device):
        return self._request('cc_control_info',{'device':device})

    def preset_clock(self):
        return self._request('preset_clock')

    def set_cc_control(self,device,controller,value,only_while_playing=False):
        return self._request('set_cc_control',{'device':device,'controller':controller,'value':value,
                             'only_while_playing':only_while_playing},write=True)

    def load_device(self,track,name,shelf=None,after_device=None,uri=None):
        """Load a device or preset onto a track by name, through Live's own browser.

        This is the only way to place a **plug-in**: `create_native_effect` takes native Live devices
        only, and devices cannot be moved afterwards. `after_device` loads it after that device, which
        is how a measuring tap gets placed behind something the producer added.
        """
        args={'track':track,'name':name}
        if uri is not None:args['uri']=uri
        if shelf:args['shelf']=shelf
        if after_device:args['after_device']=after_device
        return self._request('load_device',args,write=True)
    def delete_device(self,ident):return self._request('delete_device',{'device':ident},write=True)
    # ---- arrangement automation (recorded; see arrangement_automation.py) ----
    def automation_target(self,track,target,displays=None):
        """Identity, value, range and automation state of a mixer control or device parameter; optional display->internal conversion."""
        args={'track':track,'target':target}
        if displays is not None:args['displays']=displays
        return self._request('automation_target',args)
    def record_automation(self,track,lanes,lead=1.0):
        return self._request('record_automation',{'track':track,'lanes':lanes,'lead':lead},write=True)
    def automation_take_status(self):return self._request('automation_take_status')
    def stop_automation_take(self):return self._request('stop_automation_take',write=True)
    def undo_automation_take(self,take):return self._request('undo_automation_take',{'take':take},write=True)
    def sample_automation(self,track,targets,beats):
        # Each sample waits for Live to settle after locating (about 0.3 s), so allow for the count.
        saved=self._write_timeout;self._write_timeout=max(saved,10.0+0.5*len(beats))
        try:return self._request('sample_automation',{'track':track,'targets':targets,'beats':beats},write=True)
        finally:self._write_timeout=saved
    def read_automation(self,*args,**kwargs):self._unsupported('read_automation')
    def write_automation(self,*args,**kwargs):self._unsupported('write_automation')
    def read_compensated_latency(self,*args,**kwargs):self._unsupported('read_compensated_latency')
