"""Native Delay/Reverb insertion and removal with host readback and change records."""
import json,uuid,re,time,math
import engine,daw_adapter,serum_controls,workflow_requirements

def validate(s):
 if not isinstance(s,dict):raise ValueError('Invalid effect command.')
 if s.get('op')=='remove':
  if set(s)!={'op','transaction'} or not isinstance(s['transaction'],str) or not re.fullmatch('[a-f0-9]{32}',s['transaction']):raise ValueError('Use the saved effect change ID.')
  return s
 fields={'op','track','effect','wet'}
 if s.get('effect')=='delay':fields|={'feedback','timeMs'}
 elif s.get('effect')=='reverb':fields|={'decayMs'}
 else:raise ValueError('Only native Delay and Reverb are supported.')
 if set(s)!=fields or s.get('op')!='add' or not isinstance(s['track'],str) or not s['track'].strip():raise ValueError('Invalid effect insertion fields.')
 ranges={'wet':(0,100),'feedback':(0,100),'decayMs':(200,60000),'timeMs':(1,5000)}
 for k,(lo,hi) in ranges.items():
  if k in s and (type(s[k]) is not int or not lo<=s[k]<=hi):raise ValueError('Effect setting outside verified limits: '+k)
 return s

def root():p=engine.DATA/'effect-changes';p.mkdir(exist_ok=True);return p
def save(r):
 from transaction_store import Store
 p=root()/(r['id']+'.json');Store(p.parent).write(p,r)
def values(d):return {p['name']:p.get('value') for p in d.get('parameters',[])}
def matches(actual,expected):
    # Producer Pal display-unit conversion is quantised: actual Reverb proof
    # returned 1790 ms for 1800, 1190 for 1200 and 9.9% for 10. Report actual values.
    for k,v in expected.items():
        got=actual.get(k)
        tolerance=.11 if k=='Dry/Wet' else min(10.1,max(1,abs(v)*.01)) if k=='Decay Time' else 0
        if tolerance:
            if type(got) not in (int,float) or not math.isfinite(got) or abs(got-v)>tolerance:return False
        elif got!=v:return False
    return True

def target(name,a):
 number=re.fullmatch(r'(?:track|channel)\s+(\d+)',name.strip(),re.I)
 tracks=a.session()['tracks']
 matches=[t for t in tracks if (t['trackIndex']==int(number[1])-1 if number else t['name'].casefold()==name.strip().casefold())]
 if len(matches)!=1:raise ValueError('Choose one existing track by name or track number.')
 t=matches[0]
 return dict(t,devices=a.devices(t['id']))

def execute(s,adapter=None):
 validate(s);a=adapter or daw_adapter.DEFAULT
 daw_adapter.require(a,workflow_requirements.EFFECT_REMOVE if s['op']=='remove' else workflow_requirements.EFFECT_ADD,'change effects')
 with engine.PLAY_LOCK:
  if s['op']=='remove':return remove(s['transaction'],a)
  return add(s,a)['text']

def add(s,a):
 """Insert and verify one effect; returns the saved change record (its 'text' is the reply, its 'id' removes it)."""
 t=target(s['track'],a);ids=[d['id'] for d in t['devices']]
 r={'id':uuid.uuid4().hex,'spec':s,'trackId':t['id'],'beforeIds':ids,'status':'creating'};r['name']='Scrunch '+s['effect'].title()+' '+r['id'][:8];save(r)
 try:
  fresh=target(s['track'],a)
  if fresh['id']!=t['id'] or [d['id'] for d in fresh['devices']]!=ids:raise ValueError('Effect target changed before insertion.')
  # Configure before enabling so playback does not hear intermediate settings.
  created=a.create_native_effect(t['trackIndex'],s['effect'].title(),r['name'],{'Device On':'Off'})
  r.update(deviceId=created['id'],status='configuring');save(r)
  params={'Dry/Wet':s['wet']}
  if s['effect']=='delay':params.update({'Feedback':s['feedback'],'L Sync':'Off','R Sync':'Off','L Time':s['timeMs'],'R Time':s['timeMs'],'Freeze':'Off'})
  else:params.update({'Decay Time':s['decayMs'],'Freeze On':'Off'})
  d=a.read_device(created['id'])
  if d['name']!=r['name'] or d['type']!='audio-effect: '+s['effect'].title():raise ValueError('Created effect identity differs.')
  if s['effect']=='delay':
   a.set_device_parameters(d['id'],{'Link':'Off','L Sync':'Off','R Sync':'Off'})
   if any(values(a.read_device(d['id'])).get(k)!='Off' for k in ('Link','L Sync','R Sync')):raise ValueError('Delay channel unlinking not verified.')
   params['Link']='Off'
  a.set_device_parameters(d['id'],params)
  for _ in range(20):
   d=a.read_device(d['id'])
   if matches(values(d),params):break
   time.sleep(.1)
  else:raise ValueError('Effect settings did not read back.')
  fresh=target(s['track'],a)
  if [x['id'] for x in fresh['devices']]!=ids+[d['id']]:raise ValueError('Device order changed during insertion.')
  a.set_device_parameters(d['id'],{'Device On':'On'});d=a.read_device(d['id'])
  if values(d).get('Device On')!='On':raise ValueError('Effect activation was not verified.')
  r.update(status='applied',after=values(d),requested=params)
  r['text']=f"Added native {s['effect'].title()} to {t['name']} at {values(d)['Dry/Wet']}% wet, {values(d).get('L Time',values(d).get('Decay Time'))} ms {'delay' if s['effect']=='delay' else 'decay'}; settings verified. Effect change ID: {r['id']}."
  save(r);return r
 except Exception as exc:r['error']=str(exc);save(r);raise

def remove(ident,a):
 p=root()/(ident+'.json')
 if not p.exists():raise ValueError('Unknown effect change.')
 r=json.loads(p.read_text())
 if r['status']=='removed':return 'That inserted effect is already removed.'
 if not r.get('deviceId'):raise ValueError('This insertion did not record a device ID; no device can be identified for removal.')
 t=target(r['spec']['track'],a);ids=[d['id'] for d in t['devices']]
 if t['id']!=r['trackId']:raise ValueError('Effect track changed.')
 if r['deviceId'] not in ids:
  r['status']='removed';save(r);return 'The inserted effect is no longer on this track.'
 remaining=[ident for ident in ids if ident!=r['deviceId']]
 d=a.read_device(r['deviceId'])
 r['status']='removing';save(r);a.delete_device(d['id'])
 if [x['id'] for x in target(r['spec']['track'],a)['devices']]!=remaining:raise ValueError('Effect removal not verified.')
 r['status']='removed';save(r);return 'Removed the inserted effect; remaining devices verified.'
