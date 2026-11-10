"""Read-only exposed instrument inventory. No MIDI Learn or parameter probing."""
from datetime import datetime,timezone
import engine,re
import midi_transport

VOCABULARY=('cutoff','resonance','drive','mix','time','feedback')
# Exact, literal labels only. No hardware CC defaults or fuzzy name inference.
KNOWN={'Jup-8000 V','Diva'}
ALIASES={name:name for name in VOCABULARY}

def describe(device,track):
    identity=device.get('type','').partition(': ')[2]
    known=identity in KNOWN
    parameters=[]
    for p in device.get('parameters',[]):
        raw=dict(p);name=str(p.get('name',''))
        semantic=ALIASES.get(name.casefold()) if known else None
        # Host parameter IDs and indexes are NOT MIDI controller numbers.
        cc=p.get('midiCC')
        label=re.fullmatch(r'CC\s*(\d{1,3})',name,re.I)
        if cc is None and label:cc=int(label[1])
        if type(cc)!=int or not 0<=cc<=127:cc=None
        parameters.append({'raw':raw,'semantic':semantic,'semanticBasis':'exact literal parameter label' if semantic else None,
          'hostParameterId':p.get('id'),'midiCC':cc,'midiCCStatus':'reported' if cc is not None else 'not-reported',
          'hostAddressAvailable':bool(p.get('id') and name),'writeTested':False,'controlStatus':'exposed-not-write-tested' if p.get('id') and name else 'no-host-address'})
    present={p['semantic'] for p in parameters if p['semantic']}
    return {'deviceId':device.get('id'),'path':device.get('path'),'instrument':identity or device.get('name','Unknown'),
      'track':{k:track.get(k) for k in ('id','name','trackIndex')},'knownVocabulary':known,'midiFallback':midi_transport.capability(),
      'parameters':parameters,'capabilities':{v:[p['hostParameterId'] for p in parameters if p['semantic']==v] for v in VOCABULARY},
      'unavailableVocabulary':[v for v in VOCABULARY if v not in present],
      'limitations':['Only parameters exposed by this Live instance are listed.','MIDI CC assignments are not provided by the current Producer Pal read-device API.','Reading a parameter does not verify writing it. This module performs no writes.']}

def scan():
    report={'schemaVersion':1,'observedAt':datetime.now(timezone.utc).isoformat(),'scope':'loaded-session-instruments',
      'available':False,'complete':False,'instruments':[],'errors':[],
      'limitations':['Installed but unloaded plug-ins are outside session-state inventory.','No CC assignments are inferred from parameter IDs, order or hardware conventions.']}
    seen=set()
    def visit(stub,track,depth=0):
        if depth>16:raise ValueError('Rack nesting exceeds 16 levels.')
        ident=stub.get('id');path=stub.get('path')
        key=ident or path
        if not key or key in seen:return
        if len(seen)>=512:raise ValueError('Device inventory exceeds 512 nodes.')
        seen.add(key)
        d=engine.host().read_device_detail(ident,path)
        if ident and d.get('id')!=ident:raise ValueError('Device identity changed during read.')
        if str(d.get('type','')).startswith('instrument:'):
            d=engine.host().read_device_detail(ident,path,('params','param-values','chains','return-chains','drum-pads'),1)
            if ident and d.get('id')!=ident:raise ValueError('Device identity changed during parameter read.')
            report['instruments'].append(describe(d,track))
        else:
            d=engine.host().read_device_detail(ident,path,('chains','return-chains','drum-pads'),1)
        for field in ('chains','returnChains','drumPads'):
            for chain in d.get(field,[]):
                for child in chain.get('devices',[]):visit(child,track,depth+1)
                if not chain.get('devices') and chain.get('deviceCount',0):visit(chain,track,depth+1)
        for child in d.get('devices',[]):visit(child,track,depth+1)
    try:
        session=engine.live_set();report['sessionName']=session.get('name');report['available']=True
        tracks=session.get('tracks',[])+session.get('returnTracks',[])+([session['masterTrack']] if session.get('masterTrack') else [])
        for track in tracks:
            try:
                detail=engine.host().read_track_detail(track['id'],'devices')
                if detail.get('id')!=track['id']:raise ValueError('Track identity changed during read.')
                for device in detail.get('devices',[]):visit(device,track)
            except Exception as exc:report['errors'].append({'trackId':track['id'],'error':str(exc)[:300]})
        report['complete']=not report['errors']
    except Exception as exc:report['errors'].append({'error':str(exc)[:300]})
    return report
