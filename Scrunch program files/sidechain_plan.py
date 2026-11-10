"""Explicit native Compressor sidechain preview; no writes or PDC claims."""
import copy,hashlib,json
import session_plans

def read_preview(body,observe,read_device):
    """Read original state from the host, never from client-supplied parameters."""
    if not isinstance(body,dict) or set(body)!={'trackId','deviceId','sourceTrackId','channel'}:raise ValueError('Explicit track, device, source and channel only')
    if any(not isinstance(v,str) or not v.strip() or len(v)>200 for v in body.values()):raise ValueError('Nonempty bounded identifiers and channel required')
    before=observe();session_plans.fingerprint(before)
    targets=[t for t in before['tracks'] if t.get('id')==body['trackId']]
    if len(targets)!=1 or sum(d.get('id')==body['deviceId'] for d in targets[0].get('devices',[]))!=1:raise ValueError('Device is not on the current target track')
    device=read_device(body['deviceId'])
    if device.get('id')!=body['deviceId']:raise ValueError('Host returned a different device')
    result=preview(before,body['trackId'],device,body['sourceTrackId'],body['channel'])
    after=observe()
    if session_plans.fingerprint(after)!=result['observationFingerprint']:raise ValueError('Session changed while reading sidechain state')
    if read_device(body['deviceId'])!=device:raise ValueError('Device changed while reading sidechain state')
    return result

def preview(observation,target_track,device,source_id,channel):
    fingerprint=session_plans.fingerprint(observation)
    tracks=observation['tracks'];targets=[t for t in tracks if t.get('id')==target_track]
    sources=[t for t in tracks if t.get('id')==source_id]
    if len(targets)!=1 or len(sources)!=1 or source_id==target_track:raise ValueError('Distinct current source and target required')
    if sum(t.get('name')==sources[0].get('name') for t in tracks)!=1:raise ValueError('Source name is ambiguous in Producer Pal routing')
    if device.get('type')!='audio-effect: Compressor' or not device.get('id') or sum(d.get('id')==device['id'] for d in targets[0].get('devices',[]))!=1:raise ValueError('Verified native Compressor on target required')
    params={}
    for p in device.get('parameters',[]):
        if p.get('name') in params:raise ValueError('Duplicate parameter names cannot be safely restored')
        params[p['name']]=p.get('value')
    if not {'sidechainSourceTrackId','sidechainChannel','S/C On'}<=params.keys():raise ValueError('Sidechain original state is incomplete')
    if source_id not in device.get('options',{}).get('sidechainSourceTrackIds',[]):raise ValueError('Source not offered by current device')
    if channel not in ('Pre FX','Post FX','Post Mixer'):raise ValueError('Explicit supported channel semantics required')
    discovery=params['sidechainSourceTrackId']!=source_id
    if not discovery and channel not in device.get('options',{}).get('sidechainChannels',[]):raise ValueError('Requested channel not offered for current source')
    result={'mode':'dry-run','observationFingerprint':fingerprint,'trackId':target_track,'deviceId':device['id'],
            'sourceTrackId':source_id,'channel':channel,'originalParameters':copy.deepcopy(device['parameters']),
            'requiresChannelDiscoveryAfterSourceSelection':discovery,'executed':False,'actAvailable':False,
            'rollbackSourceTransportValue':'null' if params['sidechainSourceTrackId'] is None else params['sidechainSourceTrackId'],
            'summary':f"Propose {sources[0]['name']} ({channel}) as Compressor sidechain on {targets[0]['name']}. No changes.",
            'gates':['Channel choices must be freshly read after selecting a different source.',
                     'Source routing does not enable S/C On or establish audible compression.',
                     'Routing cycles, compensation, automation ownership and production rollback remain gated.']}
    result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return result
