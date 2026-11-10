"""Bounded local rolling-window analysis using the same DSP as Deep Analysis.

No cloud calls. Unbound device-position streams cannot authorize track edits.
The consumer drains PCM separately from analysis so DSP cannot stall the ring.
"""
import threading,time,collections,copy,math
import numpy as np
import reference_dsp as dsp
from live_ears import LiveEarsSource
import live_ears_binding
import host_clock

class RollingWindow:
 def __init__(self,seconds=3):self.seconds=seconds;self.reset()
 def reset(self):self.blocks=collections.deque();self.frames=0;self.format=None;self.last=None;self.last_drop=None;self.last_sample=None;self.last_frames=0;self.timestamp_flags=0;self.host_time=0;self.discontinuities=0
 def push(self,b):
  if type(b.sample_rate) not in (int,float) or not math.isfinite(b.sample_rate) or b.sample_rate!=int(b.sample_rate):raise ValueError('Invalid exact stream sample rate')
  if b.timestamp_flags&1 and not math.isfinite(b.sample_time):raise ValueError('Invalid verified sample timestamp')
  fmt=(int(b.sample_rate),b.channels)
  if fmt[0] not in (32000,44100,48000,88200,96000,176400,192000) or fmt[1] not in (1,2):raise ValueError('Unsupported stream format')
  discontinuity=self.format is not None and (fmt!=self.format or bool(b.timestamp_flags&1)!=bool(self.timestamp_flags&1) or b.sequence!=self.last+1 or b.dropped!=self.last_drop or (self.last_sample is not None and (self.last_sample!=0 or b.sample_time!=0) and b.sample_time!=self.last_sample+self.last_frames))
  if discontinuity:self.blocks.clear();self.frames=0;self.discontinuities+=1
  self.format=fmt;self.last=b.sequence;self.last_drop=b.dropped;self.last_sample=b.sample_time;self.last_frames=b.frames;self.timestamp_flags=b.timestamp_flags;self.host_time=b.host_time
  a=np.frombuffer(b.pcm_float32_interleaved,dtype=np.float32).reshape(b.frames,b.channels)
  if not np.isfinite(a).all():self.blocks.clear();self.frames=0;raise ValueError('Non-finite audio buffer')
  self.blocks.append(a);self.frames+=len(a);limit=self.seconds*fmt[0]
  while self.frames>limit:
   excess=self.frames-limit;first=self.blocks[0]
   if len(first)<=excess:self.frames-=len(self.blocks.popleft())
   else:self.blocks[0]=first[excess:];self.frames-=excess
 def snapshot(self):
  if not self.format or self.frames<self.seconds*self.format[0]:return None
  return np.concatenate(tuple(self.blocks)),self.format[0]

class LiveAnalysis:
 def __init__(self):
  self.lock=threading.RLock();self.stop_event=threading.Event();self.window=RollingWindow();self.worker=None;self.reader=None;self.binder=None;self.result=None;self.error=None;self.stream=None;self.last_audio=0;self.binding=None;self.binding_target=None;self.binding_chain=None;self.binding_error=None;self.reference=None;self.history=collections.deque(maxlen=20)
 def start(self,stream):
  self.stop()
  # Open synchronously: invalid/leased producers fail before claiming connected.
  try:source=LiveEarsSource(stream)
  except RuntimeError as exc:raise ValueError(str(exc)) from exc
  with self.lock:self.stream=stream;self.window=RollingWindow();self.result=None;self.error=None;self.last_audio=0;self.binding=None;self.binding_target=None;self.binding_chain=None;self.binding_error=None;self.history.clear();self.stop_event=threading.Event()
  stop=self.stop_event
  def read():
   try:
    while not stop.is_set():
     block=source.read()
     if block is None:stop.wait(.001);continue
     with self.lock:
      self._accept_block(block)
   except Exception as e:
    with self.lock:self.error=str(e);self.result=None
    stop.set()
   finally:source.close()
  def watch_binding():
   while not stop.is_set():
    try:
     binding=live_ears_binding.observe_binding(stream)
     with self.lock:
      if self.binding_target and self.binding_target!=(binding['trackId'],binding['deviceId']):
       self.binding=None;self.result=None;self.error='Live Ears device moved. Select its current source and reconnect before using measurements.';stop.set();return
      if self.binding_chain!=binding.get('deviceChain'):
       self.window.blocks.clear();self.window.frames=0;self.window.discontinuities+=1;self.result=None;self.history.clear()
      self.binding_chain=copy.deepcopy(binding.get('deviceChain'))
      self.binding=binding;self.binding_target=(binding['trackId'],binding['deviceId']);self.binding_error=None
    except Exception as e:
     with self.lock:self.binding=None;self.binding_error=str(e)
    stop.wait(5)
  def analyse():
   while not stop.wait(.5):
    with self.lock:
     snapshot=self.window.snapshot();generation=self.window.discontinuities;last=self.last_audio
     source_binding=copy.deepcopy(self.binding) if self.binding and time.time()-self.binding['observedAt']<7 else None
     metadata={'timestampFlags':self.window.timestamp_flags,'hostTime':self.window.host_time,'endSample':self.window.last_sample+self.window.last_frames if self.window.last_sample is not None else None,'dropped':self.window.last_drop}
    if snapshot is None or time.monotonic()-last>1:continue
    start=time.monotonic()
    try:
     pcm,sr=snapshot;measurement=dsp.analyse(dsp.prepare(pcm,sr))
     with self.lock:reference=self.reference
     comparison=None
     if reference:
      try:
       report,_,_=dsp.compare(pcm,sr,reference['pcm'],reference['sampleRate']);comparison={'delta':report['delta'],'reference':reference['provenance'],'alignment':'manual fixed reference cue versus current rolling 3 seconds; no automatic alignment','levelMatched':True}
      except ValueError as exc:comparison={'available':False,'reason':str(exc),'reference':reference['provenance']}
     with self.lock:
      if stop.is_set() or generation!=self.window.discontinuities or reference is not self.reference:continue
      self.result={'windowEndedMonotonic':last,'windowEndedAt':time.time()-(time.monotonic()-last),'droppedBuffersTotal':metadata['dropped'],'measurement':measurement,'measuredAt':time.time(),'sourceSampleRate':sr,'sourceChannels':pcm.shape[1],'sourceBindingAtMeasurement':source_binding or 'UNVERIFIED','hostTimestampFlags':metadata['timestampFlags'],'hostBufferStartTicks':metadata['hostTime'],'sampleClockVerified':bool(metadata['timestampFlags']&1),'windowEndSample':metadata['endSample'] if metadata['timestampFlags']&1 else None,'analysisSampleRate':48000,'windowSeconds':3,'dspVersion':dsp.VERSION,'analysisMilliseconds':(time.monotonic()-start)*1000,'discontinuities':generation,'referenceComparison':comparison}
      self.result['hostBufferStartToPublishedMs']=host_clock.age_ms(metadata['hostTime'],metadata['timestampFlags'])
      previous=self.history[-1] if self.history and self.history[-1]['discontinuities']==generation else None
      self.result['recentDelta']=measurement_delta(measurement,previous['measurement']) if previous else None
      self.history.append({'discontinuities':generation,'measurement':{k:measurement[k] for k in ('integratedLUFS','truePeakDbTP','correlation','crestFactorDb','bands')}})
    except Exception as e:
     with self.lock:self.error=str(e);self.result=None
     stop.set()
  self.reader=threading.Thread(target=read,daemon=True);self.worker=threading.Thread(target=analyse,daemon=True);self.binder=threading.Thread(target=watch_binding,daemon=True);self.reader.start();self.worker.start();self.binder.start()
  return self.status()
 def _accept_block(self,block):
  """Called under lock; invalidate published evidence at the discontinuity itself."""
  generation=self.window.discontinuities
  if self.last_audio and time.monotonic()-self.last_audio>1:
   self.window.blocks.clear();self.window.frames=0;self.window.discontinuities+=1
  try:self.window.push(block)
  except Exception:
   self.result=None;self.history.clear()
   raise
  if generation!=self.window.discontinuities:
   self.result=None;self.history.clear()
  self.last_audio=time.monotonic()
 def stop(self):
  self.stop_event.set()
  for t in (self.reader,self.worker,self.binder):
   if t:t.join(5)
  if any(t and t.is_alive() for t in (self.reader,self.worker,self.binder)):raise RuntimeError('Previous live analysis is still stopping; new source refused')
  self.reader=self.worker=self.binder=None
  with self.lock:self.result=None;self.stream=None;self.last_audio=0
  return self.status()
 def set_reference(self,reference):
  with self.lock:
   self.reference=reference
   # Keep valid live measurements, but never publish the previous cue's deltas.
   if self.result is not None:self.result['referenceComparison']=None
  return {'reference':copy.deepcopy(reference['provenance']) if reference else None}
 def status(self,compact=False):
  with self.lock:
   stale=not self.last_audio or time.monotonic()-self.last_audio>1 or self.stop_event.is_set() or (self.result is not None and time.monotonic()-self.result['windowEndedMonotonic']>2)
   r={'mode':'LIVE_EARS','streamId':self.stream,'state':'error' if self.error else 'stale' if stale else 'measuring' if self.result is None else 'live','error':self.error,'tapPoint':'device-position; post-fader NOT verified','trackBinding':self.binding if self.binding and time.time()-self.binding['observedAt']<7 and not stale else 'UNVERIFIED — stream identity only','bindingLimitation':self.binding_error,'automaticEditsAllowed':False,'measurementScope':'rolling 3 seconds, not whole-song integrated loudness','selectedReference':copy.deepcopy(self.reference['provenance']) if self.reference else None,'result':None if stale else copy.deepcopy(self.result)}
  if r['result']:
   result=r['result'];result['hostBufferStartToStatusMs']=host_clock.age_ms(result.get('hostBufferStartTicks',0),result.get('hostTimestampFlags',0))
   result['latencyBasis']='host-provided newest buffer start to local publication/status; excludes hardware latency; null means clock unavailable'
   result['arrangementAlignment']={'available':False,'reason':'AU render sample timestamps are not verified Arrangement bar/beat coordinates or a shared clock epoch across taps. Do not attach these measurements to a named song section.'}
  if compact and r['result']:
   m=r['result']['measurement'];r['result']['measurement']={k:m[k] for k in ('integratedLUFS','truePeakDbTP','rmsDbFS','correlation','crestFactorDb','bands')}
   recent=r['result'].get('recentDelta')
   if recent:recent['bands']=[b for b in recent['bands'] if b.get('comparisonReliable') is not False]
   comparison=r['result'].get('referenceComparison')
   if comparison and comparison.get('delta'):
    comparison['delta']['bands']=[b for b in comparison['delta']['bands'] if b.get('comparisonReliable') is not False]
  return r
def measurement_delta(current,previous):
 def difference(a,b):return a-b if a is not None and b is not None else None
 return {'basis':'previous contiguous rolling window; overlapping windows are correlated','loudnessLU':difference(current['integratedLUFS'],previous['integratedLUFS']),'correlation':difference(current['correlation'],previous['correlation']),'crestDb':difference(current['crestFactorDb'],previous['crestFactorDb']),'bands':[{'comparisonReliable':all(v is not None and v>=-80 for v in (a['energyDbFS'],b['energyDbFS'])),'name':a['name'],'lowHz':a['lowHz'],'highHz':a['highHz'],'energyDb':difference(a['energyDbFS'],b['energyDbFS']) if all(v is not None and v>=-80 for v in (a['energyDbFS'],b['energyDbFS'])) else None,'reportingFloorDbFS':-80,'sideFraction':difference(a['sideFraction'],b['sideFraction'])} for a,b in zip(current['bands'],previous['bands'])]}
SERVICE=LiveAnalysis()

def select_reference(body):
 if body.get('clear') is True:
  return SERVICE.set_reference(None)
 import reference_ab
 _,_,root=reference_ab.project();rows=reference_ab.manifest(root)['references']
 matches=[r for r in rows if r['id']==body.get('reference')]
 if len(matches)!=1:raise ValueError('Choose one existing external reference')
 cue=float(body.get('cueSeconds',0));pcm,sr=reference_ab.readcue(matches[0],cue,3)
 # Validate finite data and loudness before making it active.
 prepared=dsp.prepare(pcm,sr)
 if dsp.meter(prepared,48000)['integratedLUFS'] is None:raise ValueError('Reference cue has no measurable loudness')
 provenance={'id':matches[0]['id'],'hash':matches[0]['hash'],'name':matches[0]['name'],'cueSeconds':cue,'durationSeconds':3,'sourceSampleRate':sr}
 return SERVICE.set_reference({'pcm':pcm,'sampleRate':sr,'provenance':provenance})
