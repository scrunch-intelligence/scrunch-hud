"""Identical, bounded stereo comparison pipeline. No host or file mutations."""
import audio_analysis
import ctypes as C, math
from pathlib import Path
import numpy as np
from scipy.signal import welch,csd,resample_poly
VERSION='reference-ab-2'
BANDS=[('sub',20,60),('bass',60,200),('low mids',200,500),('mids',500,2000),('upper mids',2000,3000),('presence',3000,4000),('presence / harshness region',4000,6000),('brightness',6000,8000),('brilliance',8000,12000),('air',12000,16000),('upper air',16000,20000)]
LIB=C.CDLL(str(Path(__file__).parent/'native/libebur128/libebur128.dylib'))
LIB.ebur128_init.argtypes=[C.c_uint,C.c_ulong,C.c_int];LIB.ebur128_init.restype=C.c_void_p
LIB.ebur128_destroy.argtypes=[C.POINTER(C.c_void_p)]
LIB.ebur128_add_frames_double.argtypes=[C.c_void_p,C.POINTER(C.c_double),C.c_size_t]
for n in ('loudness_global','loudness_momentary','loudness_shortterm','loudness_range'):
 getattr(LIB,'ebur128_'+n).argtypes=[C.c_void_p,C.POINTER(C.c_double)]
LIB.ebur128_true_peak.argtypes=[C.c_void_p,C.c_uint,C.POINTER(C.c_double)]
def finite(x):return float(x) if np.isfinite(x) else None
def db(x):return float(20*np.log10(x)) if x>0 else None
def meter_blocks(blocks,sr,channels):
 st=C.c_void_p(LIB.ebur128_init(channels,sr,63))
 if not st.value:raise ValueError('Loudness meter could not initialize.')
 def read(name):
  v=C.c_double();err=getattr(LIB,'ebur128_'+name)(st,C.byref(v))
  if err:raise ValueError('Loudness measurement failed: '+name)
  return finite(v.value)
 try:
  history=[];frames=0
  for block in blocks:
   z=np.ascontiguousarray(block,dtype=np.float64)
   if z.ndim!=2 or z.shape[1]!=channels or not np.isfinite(z).all():raise ValueError("Invalid meter block")
   if LIB.ebur128_add_frames_double(st,z.ctypes.data_as(C.POINTER(C.c_double)),len(z)):raise ValueError('Meter rejected audio.')
   frames+=len(z);t=frames/sr
   history.append({'seconds':t,'momentaryLUFS':read('loudness_momentary') if t>=.4 else None,'shortTermLUFS':read('loudness_shortterm') if t>=3 else None})
  peaks=[]
  for ch in range(channels):
   v=C.c_double()
   if LIB.ebur128_true_peak(st,ch,C.byref(v)):raise ValueError('True peak failed.')
   peaks.append(v.value)
  return {'integratedLUFS':read('loudness_global'),'truePeakDbTP':db(max(peaks)), 'loudnessRangeLU':read('loudness_range') if frames>=sr*60 else None,'loudnessHistory':history,'meter':'libebur128 1.2.6','truePeakValidation':'Synthesized EBU Tech 3341 cases 15–23 passed at 48 kHz; not full EBU certification'}
 finally:LIB.ebur128_destroy(C.byref(st))
def meter(y,sr):
 y=np.ascontiguousarray(y,dtype=np.float64);hop=max(1,round(sr*.1))
 return meter_blocks((y[i:i+hop] for i in range(0,len(y),hop)),sr,y.shape[1])
def prepare(y,sr):
 y=np.asarray(y,dtype=float)
 if y.ndim==1:y=y[:,None]
 if y.ndim!=2 or y.shape[1] not in (1,2) or not np.isfinite(y).all():raise ValueError('Use finite mono/stereo audio.')
 if not 3<=len(y)/sr<=120:raise ValueError('Select a section between 3 and 120 seconds.')
 if sr!=48000:
  g=math.gcd(sr,48000);y=resample_poly(y,48000//g,sr//g,axis=0)
 if y.shape[1]==1:y=np.repeat(y,2,axis=1)
 return np.ascontiguousarray(y)
def analyse(y):
 sr=48000;r=meter(y,sr);rms=np.sqrt(np.mean(y*y,axis=0));r['rmsDbFS']=[db(v) for v in rms];r['samplePeakDbFS']=db(np.max(np.abs(y)))
 f,p=welch(y,sr,nperseg=8192,axis=0);_,cross=csd(y[:,0],y[:,1],sr,nperseg=8192)
 df=f[1]-f[0];bands=[]
 for name,lo,hi in BANDS:
  mask=(f>=lo)&(f<hi);l,rr=p[mask].sum(axis=0)*df;c=cross[mask].real.sum()*df
  total=(l+rr)/2;mid=max(0,(l+rr+2*c)/4);side=max(0,(l+rr-2*c)/4)
  bands.append({'name':name,'lowHz':lo,'highHz':hi,'energyDbFS':db(np.sqrt(total)),'correlation':float(np.clip(c/np.sqrt(l*rr),-1,1)) if l*rr>1e-24 else None,'sideFraction':float(side/(mid+side)) if mid+side>1e-12 else None,'leftRightBalanceDb':db(np.sqrt(rr/l)) if l>1e-12 and rr>1e-12 else None})
 r['bands']=bands;r['spectrum']={'hz':f.tolist(),'powerDbPerHz':[10*math.log10(max(v,1e-20)) for v in p.mean(axis=1)]}
 r['correlation']=float(np.sum(y[:,0]*y[:,1])/np.sqrt(np.sum(y[:,0]**2)*np.sum(y[:,1]**2))) if np.prod(rms)>1e-12 else None
 r['crestFactorDb']=r['samplePeakDbFS']-db(np.sqrt(np.mean(y*y))) if np.max(np.abs(y)) else None
 r['rmsHistory']=[{'seconds':i/sr,'rmsDbFS':db(np.sqrt(np.mean(y[i:i+4800]**2)))} for i in range(0,len(y),4800)]
 # Peak envelopes preserve polarity; same sample clock for stacked/overlay views.
 step=max(1,len(y)//1200);r['waveform']={'sampleRate':sr,'stepSamples':step,'min':[float(np.min(y[i:i+step])) for i in range(0,len(y),step)],'max':[float(np.max(y[i:i+step])) for i in range(0,len(y),step)]}
 return r
def compare(a,asr,b,bsr):
 a=prepare(a,asr);b=prepare(b,bsr)
 if abs(len(a)-len(b))>1:raise ValueError('Cue sections must have equal durations.')
 n=min(len(a),len(b));a=a[:n];b=b[:n];ra=meter(a,48000);rb=meter(b,48000)
 if ra['integratedLUFS'] is None or rb['integratedLUFS'] is None:raise ValueError('Cannot level-match silence or ungated audio.')
 target=min(ra['integratedLUFS'],rb['integratedLUFS'],-18);ga=target-ra['integratedLUFS'];gb=target-rb['integratedLUFS']
 # Apply identical extra attenuation to avoid intersample clipping in preview.
 ceiling=max((ra['truePeakDbTP'] if ra['truePeakDbTP'] is not None else -120)+ga,(rb['truePeakDbTP'] if rb['truePeakDbTP'] is not None else -120)+gb);extra=min(0,-3-ceiling);ga+=extra;gb+=extra
 aa=a*10**(ga/20);bb=b*10**(gb/20);ma=analyse(aa);mb=analyse(bb)
 delta=[]
 for x,z in zip(ma['bands'],mb['bands']):
  delta.append({'comparisonReliable':all(v is not None and v>=-80 for v in (x['energyDbFS'],z['energyDbFS'])),'reportingFloorDbFS':-80,'band':x['name'],'lowHz':x['lowHz'],'highHz':x['highHz'],'energyDb':x['energyDbFS']-z['energyDbFS'] if x['energyDbFS'] is not None and z['energyDbFS'] is not None else None,'sideFraction':x['sideFraction']-z['sideFraction'] if x['sideFraction'] is not None and z['sideFraction'] is not None else None,'correlation':x['correlation']-z['correlation'] if x['correlation'] is not None and z['correlation'] is not None else None})
 r={'version':VERSION,'sampleRate':48000,'durationSeconds':n/48000,'raw':{'live':ra,'reference':rb},'gainDb':{'live':ga,'reference':gb},'matched':{'live':ma,'reference':mb},'delta':{'bands':delta,'rawLoudnessLU':ra['integratedLUFS']-rb['integratedLUFS'],'matchedLoudnessLU':ma['integratedLUFS']-mb['integratedLUFS']},'automaticEditsAllowed':False,'alignment':'manual cue starts; no stretch or automatic section detection'}
 r['delta']['shortTermHistory']=[{'seconds':x['seconds'],'loudnessLU':x['shortTermLUFS']-z['shortTermLUFS'] if x['shortTermLUFS'] is not None and z['shortTermLUFS'] is not None else None} for x,z in zip(ma['loudnessHistory'],mb['loudnessHistory'])]
 r['delta']['crestFactorDb']=ma['crestFactorDb']-mb['crestFactorDb']
 r['delta']['correlation']=ma['correlation']-mb['correlation'] if ma['correlation'] is not None and mb['correlation'] is not None else None
 return r,aa,bb
