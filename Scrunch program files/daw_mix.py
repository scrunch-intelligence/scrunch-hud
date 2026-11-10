"""Live 12 side of Scrunch mix actions.

This is the only module that knows Live's device names, parameter names, option texts and routing
names. `mix_actions` (Scrunch-owned, DAW-neutral) talks to it through the small interface below, in
musical units; everything Live-specific is translated here.

Values are written as Live's own internal values. Numbers in musical units (dB, ms, Hz, %, ratio) are
turned into internal values by the bridge searching Live's own display (`mix_resolve`), except EQ
Eight frequency and Q, whose curves are exact (`eq_eight`). The display Live then shows is read back
and must equal the resolved number.

Neutral snapshot (what `state()` returns and `mix_actions` compares for exact undo):
  {'track', 'name', 'category', 'isGroup',
   'gain': {'raw', 'display', 'db'}, 'pan': {'raw', 'display', 'pan'},        # pan -1..+1
   'devices': [{'id', 'name', 'kind', 'device', 'controls': [{'id','name','raw','display','min','max','options'}],
                'sidechain': {'type','channel','types','channels'} | None}]}
"""
import math,re,time
import eq_eight

KINDS={'EQ Eight':'eq','Compressor':'compressor','Glue Compressor':'glue','Utility':'utility'}
DEVICE={v:k for k,v in KINDS.items()}
PAN_SPAN=50.0            # Live shows pan as 50L..C..50R over its internal -1..1
TAP_PLUGIN='Live Ears Bound'
def _send_db(v):
    """A send level as a number of dB, -inf for off, None when Live's reading is not a number Scrunch can trust."""
    if isinstance(v,(int,float)) and not isinstance(v,bool):return float(v)
    if isinstance(v,str):
        n=number(v)
        return n
    return None

def routing_map(adapter):
    """One read of the open Set's signal flow (no per-track reads): regular tracks with group and output, sends by return,
    return tracks, and the main output. Every send Live reports is kept; an unreadable level is marked, never guessed."""
    raw=adapter.read_live_set('tracks','routing','mixer')
    rets=[{'id':x.get('id'),'index':i,'name':x.get('name'),'kind':'return','output':(x.get('outputRoutingType') or {}).get('name'),
           'gainDb':x.get('gainDb'),'pan':x.get('pan')} for i,x in enumerate(raw.get('returnTracks') or [])]
    def sends(t):
        out=[]
        for i,x in enumerate(t.get('sends') or []):
            db=_send_db(x.get('gainDb'))
            out.append({'return':rets[i]['id'] if i<len(rets) else None,'returnName':x.get('return'),'db':db,
                        **({} if db is not None else {'unreadable':x.get('gainDb')})})
        return out
    tracks=[{'id':t['id'],'index':t.get('trackIndex'),'name':t.get('name'),'kind':'group' if t.get('isGroup') else t.get('type'),
             'group':t.get('groupId'),'output':(t.get('outputRoutingType') or {}).get('name'),'instrument':t.get('instrument'),
             'gainDb':t.get('gainDb'),'pan':t.get('pan'),'sends':sends(t)} for t in raw.get('tracks') or []]
    for r,x in zip(rets,raw.get('returnTracks') or []):r['sends']=sends(x)
    m=raw.get('masterTrack') or {}
    return {'tracks':tracks,'returns':rets,'main':{'id':m.get('id'),'name':m.get('name'),'kind':'master'}}

def contributors(flow,bus):
    """Which tracks' audio actually reaches `bus` (a group, a return or the main output), from the signal flow read by
    routing_map. Returns (ids, uncertain): a group carries its members (nested groups included); a return carries every
    track or return whose send to it is above -inf (a group's send carries its members); the main output carries
    everything. A send Live reported without a readable level counts as reaching it, and is listed in uncertain."""
    tracks={t['id']:t for t in flow['tracks']};rets={r['id']:r for r in flow['returns']}
    members={}
    for t in flow['tracks']:
        if t.get('group'):members.setdefault(t['group'],[]).append(t['id'])
    def inside(g,seen):
        out=[]
        for m in members.get(g,[]):
            if m in seen:continue
            seen.add(m);out.append(m);out+=inside(m,seen)
        return out
    if bus==(flow.get('main') or {}).get('id'):
        return [t['id'] for t in flow['tracks']]+list(rets),[]
    if bus in tracks:
        if tracks[bus]['kind']!='group':
            # an audio track another track is routed into by name (Live reports routing by display name only)
            fed=[t['id'] for t in flow['tracks'] if t['id']!=bus and t.get('output')==tracks[bus]['name']]
            return fed,([] if not fed else ['routed by output name "%s"'%tracks[bus]['name']])
        return inside(bus,set()),[]
    if bus in rets:
        out=[];uncertain=[]
        for row in list(flow['tracks'])+list(flow['returns']):
            for x in row.get('sends') or []:
                if x.get('return')!=bus:continue
                if x.get('db') is None:uncertain.append('%s send level unreadable (%r)'%(row.get('name'),x.get('unreadable')))
                elif x['db']==float('-inf') or x['db']<=-70:continue
                out.append(row['id'])
                if row.get('kind')=='group':out+=inside(row['id'],set())
        return list(dict.fromkeys(out)),uncertain
    raise ValueError('%s is not a track, return or the main output of the open Set.'%bus)

def structure(adapter):
    """Read-only map of the open Set for the engineer model: every track (regular, group), return and the main output,
    with kind, group membership, output, sends, gain, pan, instrument, devices and Arrangement clip spans (beats). Names
    are shown as Live shows them; no decision in Scrunch reads them. Reads that fail are listed in 'incomplete'."""
    flow=routing_map(adapter)
    rows=[];incomplete=[]
    for t in flow['tracks']:
        try:detail=adapter.track(t['id'],clips=True,devices=True)
        except Exception as exc:
            incomplete.append('track %s (%s): %s'%(t.get('name'),t['id'],str(exc)[:160]));detail={}
        spans=sorted((float(c['start']),float(c['start'])+float(c['span'])) for c in detail.get('clips') or [])
        merged=[]
        for a,b in spans:
            if merged and a<=merged[-1][1]+1e-9:merged[-1][1]=max(merged[-1][1],b)
            else:merged.append([a,b])
        rows.append(dict(t,muted=bool(detail.get('muted')),soloed=bool(detail.get('soloed')),
                         devices=[{'id':d['id'],'name':d.get('name'),'class':d.get('deviceName'),'role':d.get('role')} for d in detail.get('devices') or []],
                         clipSpansBeats=[[round(a,3),round(b,3)] for a,b in merged]))
    main=dict(flow['main'])
    def chain(tid,holder):
        try:holder['devices']=[{'id':d['id'],'name':d.get('name'),'class':d.get('device'),'role':None} for d in LiveMix(adapter).state(tid)['devices']]
        except Exception as exc:
            holder['devicesError']=str(exc)[:200];incomplete.append('devices of %s: %s'%(holder.get('name'),str(exc)[:160]))
    if main.get('id'):chain(main['id'],main)
    returns=[dict(r) for r in flow['returns']]
    for r in returns:
        chain(r['id'],r)
        r['fedBy'],unsure=contributors(flow,r['id'])
        if unsure:r['fedByUncertain']=unsure
    members={}
    for r in rows:
        if r['group']:members.setdefault(r['group'],[]).append(r['id'])
    for r in rows:
        if r['kind']=='group':r['members']=members.get(r['id'],[])
    unreadable=[x for row in rows+returns for x in row.get('sends') or [] if x.get('db') is None]
    if unreadable:incomplete.append('%d send level(s) not readable as numbers'%len(unreadable))
    return {'tracks':rows,'main':main,'returns':returns,'incomplete':incomplete}

def number(text):
    """The number a Live display shows (dB, ms, Hz, %, ratio, pan with L negative), or None."""
    if not isinstance(text,str):return None
    t=text.strip()
    low=t.lower()
    if low.startswith('-inf'):return float('-inf')
    if low.startswith('inf'):return float('inf')
    m=re.fullmatch(r'(\d+)\s*([LR])',t,re.I)
    if m:return float(m[1])*(-1 if m[2].upper()=='L' else 1)
    if t.upper()=='C':return 0.0
    m=re.match(r'^[+-]?(?:\d+\.?\d*|\.\d+)',t)
    if not m:return None
    v=float(m[0]);rest=t[m.end():].strip().lower()
    if rest.startswith('khz'):v*=1000
    elif rest=='s' or rest.startswith('s '):v*=1000
    return v

class LiveMix:
    """Mix operations on the open Live Set through the ScrunchBridge Remote Script."""
    name='live-12'
    def __init__(self,adapter=None):
        if adapter is None:
            import daw_remote_script
            adapter=daw_remote_script.RemoteScriptAdapter()
        self.a=adapter
    def _r(self,op,args=None,write=False):return self.a._request(op,args or {},write=write)

    # ---- reads ----
    def state(self,track):
        s=self._r('mix_state',{'track':track})
        if s.get('id')!=track:raise ValueError('Live returned a different track.')
        vol,pan=s['mixer']['volume'],s['mixer']['panning']
        devices=[]
        for d in s['devices']:
            devices.append({'id':d['id'],'name':d.get('name'),'device':d.get('deviceName'),'kind':KINDS.get(d.get('deviceName')),
                            'controls':[{'id':p['id'],'name':p.get('name'),'raw':p.get('value'),'display':p.get('display'),
                                         'min':p.get('min'),'max':p.get('max'),'options':p.get('options'),
                                         'enabled':p.get('enabled'),'quantized':p.get('quantized'),
                                         'automation':p.get('automation')} for p in d['parameters']],
                            'sidechain':d.get('sidechain')})
        return {'track':track,'name':s.get('name'),'category':s.get('category'),'isGroup':bool(s.get('isGroup')),
                'mute':s.get('mute'),'solo':s.get('solo'),
                'gain':{'raw':vol['value'],'display':vol['display'],'db':number(vol['display']),
                        'automation':vol.get('automation')},
                'pan':{'raw':pan['value'],'display':pan['display'],'pan':(number(pan['display']) or 0.0)/PAN_SPAN},
                'devices':devices,'routing':s.get('routing')}
    def view_state(self,devices=False):
        """The producer's view/transport state and every track's state (bridge mix_view_state, read-only)."""
        return self._r('mix_view_state',{'devices':bool(devices)})
    def restore_view(self,state):
        """Put back loop brace/on, start marker, selection, focused and shown views, playhead where they differ from
        `state` (a view_state snapshot); returns what was put back and anything that still differs."""
        keep=('loop','loopStart','loopLength','songTime','startTime','backToArranger','selectedTrack','selectedScene','focusedView','visibleViews')
        return self._r('mix_restore_view',{'state':{k:state.get(k) for k in keep}},write=True)
    def resolve(self,owner,param,target):
        """Live's internal value whose own display shows `target` (see bridge op_mix_resolve)."""
        return self._r('mix_resolve',{'owner':owner,'param':param,'target':float(target)})

    # ---- mixer ----
    def gain_raw(self,track,db):return self.resolve(track,'volume',db)
    def pan_raw(self,track,pan):
        if not -1<=pan<=1:raise ValueError('Pan is -1 (hard left) .. +1 (hard right).')
        return self.resolve(track,'panning',round(pan*PAN_SPAN))
    def set_mixer(self,track,gain=None,pan=None,expect=None):
        args={'track':track}
        if gain is not None:args['volume']=gain
        if pan is not None:args['panning']=pan
        if expect:args['expect']={({'gain':'volume','pan':'panning'}[k]):v for k,v in expect.items()}
        return self._r('mix_set_mixer',args,write=True)

    # ---- devices ----
    def insert(self,track,kind,name,at=None):
        """Insert the native device for `kind`, switched off so its defaults are never heard."""
        if kind not in DEVICE:raise ValueError('No native device for %r.'%kind)
        return self._r('mix_insert_device',{'track':track,'device_name':DEVICE[kind],'name':name,'index':at,'off':True},write=True)
    def remove(self,device):return self._r('delete_device',{'device':device},write=True)
    def write(self,device,writes):
        """Grouped raw writes, validated whole before any is set and verified after settling."""
        plan=[{'id':w['id'],'name':w['name'],'value':w['value'],**({'expect_value':w['expect']} if w.get('expect') is not None else {})} for w in writes]
        return self._r('set_device_parameters',{'device':device,'writes':plan,'confirm':True},write=True)
    def restore(self,device,writes):
        return self._r('mix_restore_parameters',{'device':device,'writes':writes},write=True)
    def set_sidechain(self,device,type_name,channel=None,expect_type=None,source=None):
        args={'target':device,'kind':'sidechain','type':type_name}
        if source is not None:args['source']=source
        if channel is not None:args['channel']=channel
        if expect_type is not None:args['expect_type']=expect_type
        return self._r('mix_set_routing',args,write=True)
    def routing_for(self,routing,track_id,track_name):
        """(display name, source id or None) of the routing choice that carries `track_id`. Two tracks with one name
        show the same choice text; Live's attached track (typeTracks) tells them apart, and set_* passes the id on."""
        choices=routing.get('types') or []
        exact=[c for c in choices if c==track_name]
        if len(exact)>1:
            tracks=routing.get('typeTracks') or []
            if track_id not in tracks:raise ValueError('%d routing choices name track %r and Live does not say which is which.'%(len(exact),track_name))
            return track_name,track_id
        return self.routing_name(choices,track_name),None
    def routing_name(self,choices,track_name):
        """The routing choice Live shows for a track: its name, or its name after Live's 'N-' number."""
        exact=[c for c in choices if c==track_name]
        if len(exact)==1:return exact[0]
        numbered=[c for c in choices if re.fullmatch(r'\d+-'+re.escape(track_name),c or '')]
        if len(numbered)==1:return numbered[0]
        raise ValueError('%d routing choices name track %r.'%(len(exact) or len(numbered),track_name))

    # ---- configuring a device from musical settings ----
    def configure(self,kind,device,settings,fresh):
        """Stages of raw writes that put `settings` (musical units) on `device` (a neutral snapshot entry).

        Returns [{'writes':[{id,name,value,expect}], 'want':{name: number or option}}, ...]; stages are
        applied in order, each verified. The device's own 'Device On' is the last stage, so the sound
        changes once, with everything set. `fresh` means Scrunch inserted it just now.
        """
        c={x['name']:x for x in device['controls']}
        stages=[];want={};fields={}      # fields: the musical setting -> the control that holds it (for automation lanes)
        def field(key,ctrl):fields[key]={'id':ctrl['id'],'name':ctrl['name']}
        def need(*names):
            for n in names:
                if n in c:return c[n]
            raise ValueError('%s has none of %s.'%(device.get('device'),', '.join(names)))
        def num(ctrl,target):
            r=self.resolve(device['id'],ctrl['id'],target)
            want[ctrl['name']]=r['number']
            return {'id':ctrl['id'],'name':ctrl['name'],'value':r['value'],'expect':ctrl['raw'],'display':r['display'],'snapped':r['snapped']}
        def opt(ctrl,pattern):
            options=ctrl.get('options') or []
            hits=[o for o in options if re.search(pattern,o,re.I)]
            if len(hits)!=1:raise ValueError('%s: %d of %s match %r.'%(ctrl['name'],len(hits),options,pattern))
            want[ctrl['name']]=hits[0]
            return {'id':ctrl['id'],'name':ctrl['name'],'value':float((ctrl.get('min') or 0)+options.index(hits[0])),'expect':ctrl['raw']}
        if kind=='eq':
            band=self._eq_band(c,fresh)
            n=eq_eight.band_names(band)
            on,typ,hz,db,q=(need(n[k]) for k in ('on','type','hz','db','q'))
            first=[]
            if not on['raw']:first.append(opt(on,'^on$'))
            if fresh and 'Adaptive Q' in c and c['Adaptive Q']['raw']:first.append(opt(c['Adaptive Q'],'^off$'))
            if first:stages.append({'writes':first})
            shape=settings['shape']
            pattern={'bell':r'^bell$','low-shelf':r'^low\s*shelf$','high-shelf':r'^high\s*shelf$','notch':r'^notch$',
                     # Live 12.3.5 shows 'High Pass 48dB' / 'Low Pass 12dB' (read 26 Sep 2026)
                     'high-pass':r'^(high\s*pass|low\s*cut)\s*%d\s*db$'%settings.get('slope',48),
                     'low-pass':r'^(low\s*pass|high\s*cut)\s*%d\s*db$'%settings.get('slope',48)}[shape]
            second=[opt(typ,pattern)]
            fv=eq_eight.hz_to_value(settings['hz']);second.append({'id':hz['id'],'name':hz['name'],'value':fv,'expect':hz['raw']})
            want[hz['name']]=settings['hz']
            if shape in ('bell','low-shelf','high-shelf'):second.append(num(db,settings['db']))
            second.append({'id':q['id'],'name':q['name'],'value':eq_eight.q_to_value(settings.get('q') or 0.71),'expect':q['raw']})
            want[q['name']]=settings.get('q') or 0.71
            field('hz',hz);field('q',q)
            if shape in ('bell','low-shelf','high-shelf'):field('db',db)
            stages.append({'writes':second,'band':band})
        elif kind in ('compressor','glue'):
            w=[]
            for key,names in (('thresholdDb',('Threshold',)),('ratio',('Ratio',)),('attackMs',('Attack',)),('releaseMs',('Release',)),
                              ('mixPercent',('Dry/Wet',)),('makeupDb',('Output Gain','Makeup') if kind=='compressor' else ('Makeup',))):
                if any(n in c for n in names):field(key,need(*names))
            if settings.get('thresholdDb') is not None:w.append(num(need('Threshold'),settings['thresholdDb']))
            if settings.get('ratio') is not None:w.append(num(need('Ratio'),settings['ratio']))
            if settings.get('attackMs') is not None:w.append(num(need('Attack'),settings['attackMs']))
            if settings.get('releaseMs')=='auto':
                if kind=='compressor':w.append(opt(need('Auto Release On/Off'),'^on$'))
                elif need('Release').get('options'):w.append(opt(need('Release'),'auto'))
                else:
                    # A continuous Release whose top end reads 'Auto' (Glue Compressor, Live 12.3.5)
                    rel=need('Release');want[rel['name']]='Auto'
                    w.append({'id':rel['id'],'name':rel['name'],'value':float(rel['max']),'expect':rel['raw']})
            elif settings.get('releaseMs') is not None:
                # Glue Compressor shows Release as bare seconds ('.1' .. '1.2', then 'Auto'; live 26 Sep 2026)
                w.append(num(need('Release'),settings['releaseMs']/1000.0 if kind=='glue' else settings['releaseMs']))
            if settings.get('makeupDb') is not None:w.append(num(need('Output Gain','Makeup') if kind=='compressor' else need('Makeup'),settings['makeupDb']))
            if kind=='compressor' and settings.get('autoMakeup') is not None and 'Makeup' in c and c['Makeup'].get('options'):
                w.append(opt(c['Makeup'],'^on$' if settings['autoMakeup'] else '^off$'))
            if settings.get('mixPercent') is not None:w.append(num(need('Dry/Wet'),settings['mixPercent']))
            if w:stages.append({'writes':w})
        elif kind=='utility':
            w=[]
            if settings.get('gainDb') is not None:
                ctrl=need('Gain');field('gainDb',ctrl);w.append(num(ctrl,settings['gainDb']))
            if any(n in c for n in ('Stereo Width','Width')):field('widthPercent',need('Stereo Width','Width'))
            if settings.get('widthPercent') is not None:w.append(num(need('Stereo Width','Width'),settings['widthPercent']))
            if 'bassMonoHz' in settings:
                mono=need('Bass Mono')
                if settings['bassMonoHz'] is None:w.append(opt(mono,'^off$'))
                else:
                    w.append(opt(mono,'^on$'));w.append(num(need('Bass Freq','Bass Mono Frequency','Bass Frequency'),settings['bassMonoHz']))
            if w:stages.append({'writes':w})
        else:raise ValueError('No device settings for %r.'%kind)
        if stages:stages[-1]['fields']=fields
        return stages,want
    def _eq_band(self,c,fresh):
        """A band this move may take: an unused (switched-off) band, or on a fresh EQ a transparent one."""
        for band in range(1,eq_eight.BANDS+1):
            n=eq_eight.band_names(band)
            if n['on'] in c and not c[n['on']]['raw']:return band
        if fresh:
            for band in range(1,eq_eight.BANDS+1):
                n=eq_eight.band_names(band)
                if n['type'] in c and str(c[n['type']].get('display')).lower()=='bell' and c.get(n['db'],{}).get('raw')==0:return band
        raise ValueError('Every band of this EQ Eight is in use.')
    def switch(self,device,on):
        c={x['name']:x for x in device['controls']}
        s=c['Device On'];return {'id':s['id'],'name':'Device On','value':float(s['max'] if on else s['min']),'expect':s['raw']}

    def play(self,start,end):
        """Loop [start, end) and play from start; the playhead is read back inside the loop."""
        return self._r('mix_play',{'start':float(start),'end':float(end)},write=True)

    # ---- the capture rig for the measure step ----
    def capture_track(self,name,source_track,channel='Post Mixer',source_type=None):
        """A muted, unarmed audio track whose input is `source_track` at `channel`, output Sends Only,
        with only the Live Ears tap on it. Returns {'track','device','stream'}. The stream is matched by
        the identity the tap publishes, or else by being the one new stream that appeared."""
        from live_ears import LiveEarsSource
        import live_ears_binding
        before=set(LiveEarsSource.discover())
        t=self._r('mix_create_audio_track',{'name':name,'mute':True},write=True)
        try:
            st=self.state(t['id'])
            by=None
            if source_type is None:
                src=self.state(source_track)
                source_type,by=self.routing_for(st['routing']['input'],source_track,src['name'])
            self._r('mix_set_routing',{'target':t['id'],'kind':'input','type':source_type,'channel':channel,**({'source':by} if by else {})},write=True)
            self._r('mix_set_routing',{'target':t['id'],'kind':'output','type':'Sends Only'},write=True)
            self.a.update_track(t['id'],monitoring='in',arm=False,mute=True)
            dev=self.a.load_device(t['id'],TAP_PLUGIN,shelf='plugins')
        except Exception:
            self._r('delete_track',{'id':t['id']},write=True);raise
        stream=None
        ident=None
        for _ in range(30):
            try:ident=live_ears_binding.device_identity(self.a.device(dev['id']))
            except Exception:ident=None
            fresh=set(LiveEarsSource.discover())-before
            if ident:
                for s in LiveEarsSource.discover():
                    try:
                        if live_ears_binding.metadata(s)['identity']==ident:stream=s;break
                    except (OSError,ValueError):continue
            if stream is None and len(fresh)==1:stream=next(iter(fresh))
            if stream:break
            time.sleep(.2)
        return {'track':t['id'],'trackIndex':t['trackIndex'],'device':dev['id'],'stream':stream,
                'binding':'identity' if ident and stream else ('only new stream' if stream else None),'input':source_type,'channel':channel}
    def route_capture(self,capture,source_type,channel=None):
        return self._r('mix_set_routing',{'target':capture['track'],'kind':'input','type':source_type,
                                          **({'channel':channel} if channel else {})},write=True)
    def listen(self,capture,seconds):
        """(frames x channels float32 array, sample rate) from the capture track's tap while Live plays."""
        import numpy as np,sound_audio
        if not capture.get('stream'):raise ValueError('The capture tap has no bound audio stream.')
        frames,rate,channels=sound_audio.capture(seconds,discover=lambda:[capture['stream']])
        return np.asarray(frames,dtype=np.float32).reshape(-1,channels),rate
