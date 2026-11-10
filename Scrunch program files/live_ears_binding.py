"""Identity joins, never track-index/name guesses. Pure validation boundary."""
import json,os,re,stat
from pathlib import Path

def metadata(stream):
 if not re.fullmatch(r'/se_\d+_[0-9a-f]{8}',stream):raise ValueError('Invalid stream identifier')
 p=Path(f'/tmp/scrunch-ears-{os.getuid()}')/stream[1:]
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
 try:
  st=os.fstat(fd)
  if st.st_uid!=os.getuid() or not stat.S_ISREG(st.st_mode) or st.st_size>2048:raise ValueError('Invalid stream metadata')
  m=json.loads(os.read(fd,2048))
 finally:os.close(fd)
 if m.get('version')!=1 or len(m.get('identity',[]))!=4 or any(type(x)!=int or not 0<x<16777216 for x in m['identity']):raise ValueError('Stream has no supported identity metadata')
 return m

def device_identity(device):
 values=[]
 for i in range(1,5):
  matches=[p for p in device.get('parameters',[]) if p.get('name')==f'Scrunch Identity {i}']
  if len(matches)!=1:return None
  try:v=float(matches[0]['value'])
  except (KeyError,ValueError,TypeError):return None
  if not v.is_integer() or not 0<v<16777216:return None
  values.append(int(v))
 return values

def bind(identity,observation):
 if not observation.get('available'):raise ValueError('Live observation is unavailable')
 matches=[]
 for track in observation.get('tracks',[]):
  for device in track.get('devices',[]):
   if device_identity(device)==identity:
    matches.append({'trackId':track.get('id'),'deviceId':device.get('id'),'trackName':track.get('name'),
                    'trackColor':track.get('color'),'identity':identity,'observedAt':observation.get('observedAt'),'observationId':observation.get('observationId'),'tapPoint':'device-position','postFaderVerified':False,'automaticEditsAllowed':False})
 if len(matches)!=1 or not matches[0]['trackId'] or not matches[0]['deviceId']:raise ValueError('Stream cannot be uniquely bound to a current Live device')
 return matches[0]

def revalidate(binding,observation):
 current=bind(binding['identity'],observation)
 if (current['trackId'],current['deviceId'])!=(binding['trackId'],binding['deviceId']):raise ValueError('Tap moved or session changed; binding must be re-established')
 return current

def verified_position(binding,target,device):
 """Final readback binds identity and chain position; never infers mixer output."""
 if target.get('id')!=binding['trackId'] or device.get('id')!=binding['deviceId'] or device_identity(device)!=binding['identity']:
  raise ValueError('Bound device identity changed during scan')
 ids=[d.get('id') for d in target.get('devices',[])]
 if any(not i for i in ids) or len(set(ids))!=len(ids) or ids.count(binding['deviceId'])!=1:
  raise ValueError('Bound device chain is unavailable or ambiguous')
 index=ids.index(binding['deviceId'])
 return {**binding,'deviceChain':{'position':index+1,'deviceCount':len(ids),'upstreamDeviceIds':ids[:index],'downstreamDeviceIds':ids[index+1:],'scope':'top-level device chain only; rack internals and mixer excluded'},'sessionIdentity':'UNVERIFIED','postFaderVerified':False}

def observe_binding(stream):
 """Read host identities, including Master/returns. Nested racks stay unverified."""
 import engine,time,uuid
 m=metadata(stream);session=engine.live_set();tracks=session.get('tracks',[])+session.get('returnTracks',[])+([session['masterTrack']] if session.get('masterTrack') else [])
 result={'available':True,'observedAt':time.time(),'observationId':uuid.uuid4().hex,'tracks':[]}
 if len(tracks)>100:raise ValueError('Binding scan exceeds 100 tracks')
 for t in tracks:
  track=engine.host().read_track_detail(t['id'],'devices','mixer')
  if track.get('id')!=t['id']:raise ValueError('Live track changed during binding scan')
  devices=[]
  for d in track.get('devices',[]):
   if 'Live Ears Bound' not in str(d):continue
   device=engine.host().read_device_detail(d['id'],aspects=('params','param-values'))
   if device.get('id')!=d['id']:raise ValueError('Live device changed during binding scan')
   devices.append(device)
  result['tracks'].append({**track,'devices':devices})
 binding=bind(m['identity'],result)
 # Double-read target proves the identity still resides there at scan completion.
 target=engine.host().read_track_detail(binding['trackId'],'devices')
 device=engine.host().read_device_detail(binding['deviceId'],aspects=('params','param-values'))
 verified=verified_position(binding,target,device)
 # Observation freshness starts at completed readback, not at the start of a long scan.
 verified['observedAt']=time.time()
 return verified
