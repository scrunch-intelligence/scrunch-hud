"""Read-only, request-scoped host context for every provider's planner."""
from datetime import datetime, timezone
import uuid
import engine
import daw_adapter

def session_signature(session):
    """Stable identity/order and tempo; playback position is deliberately excluded."""
    return {k:session.get(k) for k in ('name','tempo','timeSignature')} | {
        'trackIds':[t.get('id') for t in session.get('tracks',[])],
        'returnIds':[t.get('id') for t in session.get('returnTracks',[])],
        'masterId':(session.get('masterTrack') or {}).get('id')}

def observe(adapter=None):
    adapter=adapter or daw_adapter.DEFAULT
    result={'schemaVersion':1,'observationId':uuid.uuid4().hex,
            'observedAt':datetime.now(timezone.utc).isoformat(),
            'available':False,'tracks':[],
            'limitations':['Third-party loading and preset selection are not implemented.',
                           'Host metadata is not proof of audible output.','Plugin delay compensation, nested routing, automation and freeze state are unknown unless explicitly reported.']}
    try:
        session=adapter.read_session()
        tracks=session.get('tracks',[])+session.get('returnTracks',[])+([session['masterTrack']] if session.get('masterTrack') else [])
        identifiers=[t.get('id') for t in tracks]
        if any(i is None for i in identifiers) or len(set(identifiers))!=len(identifiers):
            raise ValueError('Session track identities are missing or duplicated.')
        if len(tracks)>100:
            raise ValueError('Session exceeds the current 100-track observation limit.')
        for track in tracks:
            detail=adapter.read_track(track['id'])
            if detail.get('id')!=track['id']:
                raise ValueError('Track changed while reading the session.')
            result['tracks'].append(detail)
        markers=adapter.read_markers()
        if any(k in markers and markers[k]!=session.get(k) for k in ('name','tempo','timeSignature')):
            raise ValueError('Session changed while reading arrangement markers.')
        marker_list=markers.get('locators')
        if isinstance(marker_list,list):result['markers']=marker_list
        if session_signature(adapter.read_session())!=session_signature(session):
            raise ValueError('Session identity, track order or tempo changed during observation.')
        result['coverage']={
            'tracksClipsDevices':'read through the '+str(getattr(adapter,'name','DAW'))+' backend; not an atomic host snapshot',
            'routing':'reported track routing metadata only; nested/sidechain completeness UNVERIFIED',
            'automation':'UNVERIFIED: envelope readback unavailable in this adapter',
            'latency':'UNVERIFIED: per-device latency and compensated signal paths unavailable',
            'freeze':'UNVERIFIED unless explicitly present in raw track data',
            'markers':'read from Live arrangement locators' if isinstance(marker_list,list) else 'UNVERIFIED: host omitted locator data',
            'sessionIdentity':'name and ordered host IDs checked before/after; saved project identity UNVERIFIED'}
        result['routingAndLatencyVerified']=False
        result['adapter']={'name':adapter.name,'capabilities':adapter.capabilities()}
        result['limitations'].extend(adapter.limitations())
        result.update(available=True,session={k:v for k,v in session.items() if k!='tracks'})
    except Exception as exc:
        result.update(available=False,tracks=[],error=str(exc)[:300])
    return result
