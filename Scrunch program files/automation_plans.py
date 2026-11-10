"""Stage 4 explicit automation previews, and their execution through arrangement_automation.

Caller supplies a verified source observation and existing envelope snapshot. A preview is a dry
run; `execute` records it into the Arrangement (Live 12.3.5 exposes no envelope writer, so the
automation is recorded on Live's clock, read back by locating the playhead and taken back with
Live's undo - see arrangement_automation.py). Existing automation outside the recorded region is
left alone; inside it, the recording replaces it.
"""
import math,copy,hashlib,json
import re
import session_plans,arrangement_automation

def preview(observation,track_id,parameter,points,existing,*,interpolation='linear'):
 fingerprint=session_plans.fingerprint(observation)
 tracks=[t for t in observation['tracks'] if t.get('id')==track_id]
 if len(tracks)!=1:raise ValueError('Automation target is missing or ambiguous')
 limits={'gainDb':(-70,0),'pan':(-1,1)}
 if parameter not in limits:raise ValueError('Only bounded mixer automation can be previewed')
 if interpolation not in ('linear','step'):raise ValueError('Explicit supported interpolation required')
 if not isinstance(existing,dict) or existing.get('verified') is not True or existing.get('trackId')!=track_id or existing.get('parameter')!=parameter:raise ValueError('Verified original envelope snapshot required')
 def validate(rows):
  if not isinstance(rows,list) or len(rows)>4096:raise ValueError('Envelope exceeds bounded preview size')
  last=-1
  for p in rows:
   if not isinstance(p,dict) or set(p)!={'seconds','value'}:raise ValueError('Explicit seconds/value points required')
   t,v=p['seconds'],p['value'];lo,hi=limits[parameter]
   if type(t) not in (int,float) or not math.isfinite(t) or t<0 or t<=last or t>86400:raise ValueError('Envelope times must increase within one day')
   if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:raise ValueError('Envelope value outside conservative limits')
   last=t
 validate(points);validate(existing.get('points'))
 if len(points)<2:raise ValueError('At least two explicit points required')
 if existing.get('interpolation') not in ('linear','step','sampled'):raise ValueError('Original interpolation is unverified')
 result={'mode':'dry-run','target':track_id,'parameter':parameter,'timeUnit':'seconds','observationFingerprint':fingerprint,'points':copy.deepcopy(points),'interpolation':interpolation,'originalEnvelope':copy.deepcopy(existing),'replaceScope':'complete envelope; original preserved in preview','executed':False,'actAvailable':interpolation=='linear','gate':'execute() records this into the Arrangement on Live\'s clock (timing within one display tick), reads it back by locating the playhead and can undo it with Live\'s undo; breakpoints themselves are not readable'}
 result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()
 return result

def fade_tail(observation,track_id,start_seconds,end_seconds,end_gain_db,existing):
 """Append a linear-dB tail fade; reject rather than erase later automation.

 The final value holds after end_seconds. This is an explicit tail operation,
 not a bounded-region replacement that could silently reshape neighboring data.
 """
 if (type(start_seconds) not in (int,float) or type(end_seconds) not in (int,float)
     or not math.isfinite(start_seconds) or not math.isfinite(end_seconds)
     or not 0<=start_seconds<end_seconds<=86400):raise ValueError('Explicit increasing fade times required')
 if not isinstance(existing,dict) or existing.get('interpolation')!='linear':raise ValueError('Tail fade requires verified linear original interpolation')
 rows=copy.deepcopy(existing.get('points'))
 # Reuse all original snapshot/point checks before deriving any values.
 preview(observation,track_id,'gainDb',[{'seconds':start_seconds,'value':-12},{'seconds':end_seconds,'value':end_gain_db}],existing)
 if any(p['seconds']>start_seconds for p in rows):raise ValueError('Later automation exists; tail fade would overwrite it')
 target=next(t for t in observation['tracks'] if t.get('id')==track_id)
 initial=rows[-1]['value'] if rows else target.get('gainDb')
 if type(initial) not in (float,int) or not math.isfinite(initial) or not -70<=initial<=0:raise ValueError('Initial fade gain is unavailable')
 if not rows or rows[-1]['seconds']<start_seconds:rows.append({'seconds':start_seconds,'value':initial})
 rows.append({'seconds':end_seconds,'value':end_gain_db})
 result=preview(observation,track_id,'gainDb',rows,existing)
 # It extends an envelope that already exists: Live exposes no exact restore of existing automation, so it is not actable.
 result.update(actAvailable=False,gate='Appends to existing automation; exact revert not guaranteed (Live exposes no way to put an existing envelope back exactly)',
               replaceScope='append tail fade only; all earlier points retained; final gain holds afterward',fade={'startSeconds':start_seconds,'endSeconds':end_seconds,'startGainDb':initial,'endGainDb':end_gain_db,'curve':'linear in decibels, not amplitude'})
 result.pop('fingerprint');result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()
 return result

def _db(display):
 m=re.match(r'^\s*(-?inf|[+-]?\d+(?:\.\d+)?)\s*dB\s*$',display or '')
 if not m:raise ValueError('Volume display is not in dB: %r'%display)
 return -70.0 if 'inf' in m.group(1) else max(-70.0,min(0.0,float(m.group(1))))

def snapshot_from_samples(adapter,track_id,parameter,seconds):
 """Verified 'sampled' snapshot of the current envelope: the parameter's value with the playhead at each time.

 Not a breakpoint list (Live 12.3.5 exposes none); gain is read from Live's own dB display, -inf as -70.
 """
 if parameter not in ('gainDb','pan'):raise ValueError('Only bounded mixer automation can be sampled here')
 tempo=float(adapter.connect()['tempo'])
 target={'mixer':'volume' if parameter=='gainDb' else 'pan'}
 rows=arrangement_automation.read(adapter,track_id,[target],[s*tempo/60.0 for s in seconds])[0]['samples']
 points=[{'seconds':s,'value':_db(r['display']) if parameter=='gainDb' else r['value']} for s,r in zip(seconds,rows)]
 return {'trackId':track_id,'parameter':parameter,'verified':True,'interpolation':'sampled','points':points,
         'automation':sorted({r['automation'] for r in rows}),'tempo':tempo}

def execute(result,adapter,observation):
 """Record a linear preview into the Arrangement and verify it; returns the undo record."""
 body={k:v for k,v in result.items() if k!='fingerprint'}
 if hashlib.sha256(json.dumps(body,sort_keys=True,allow_nan=False).encode()).hexdigest()!=result.get('fingerprint'):raise ValueError('Preview was altered after it was made')
 if result.get('interpolation')!='linear':raise ValueError('Only linear previews can be recorded')
 if session_plans.fingerprint(observation)!=result['observationFingerprint']:raise ValueError('Session changed since the preview; preview again')
 track=result['target']
 if not any(t.get('id')==track for t in adapter.session()['tracks']):raise ValueError('Automation target track is no longer in the Set')
 tempo=float(adapter.connect()['tempo'])
 beats=[round(p['seconds']*tempo/60.0,9) for p in result['points']];values=[p['value'] for p in result['points']]
 if result['parameter']=='gainDb':
  target={'mixer':'volume'};internal=arrangement_automation.internal_values(adapter,track,target,[[v,'dB'] for v in values])
 else:
  target={'mixer':'pan'};internal=list(values)
 name=adapter.automation_target(track,target)['name']
 record=arrangement_automation.write(adapter,track,[arrangement_automation.lane(target,name,zip(beats,internal))])
 return {'executed':True,'verified':record['verified'],'preview':result['fingerprint'],'tempo':tempo,
         'beats':[[b,v] for b,v in zip(beats,values)],'record':record}

def undo(outcome,adapter):
 """Take an executed preview back out; `restored` is True only if every sample reads exactly as before."""
 return arrangement_automation.undo(adapter,outcome['record'])
