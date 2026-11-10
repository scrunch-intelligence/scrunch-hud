"""Whole-render window profile. Local authoritative DSP; no section guessing or host writes."""
import copy,json,math,uuid
from pathlib import Path
import reference_ab,reference_dsp as dsp,ingest
import soundfile as sf

def root():return reference_ab.project()[2].parent/'arrangement-profiles'
def starts(frames,sr=48000):
 width=sr*3
 if frames<width:raise ValueError('Master render must contain at least three seconds.')
 values=list(range(0,frames-width+1,sr))
 if values[-1]!=frames-width:values.append(frames-width)
 return values

def measure(path,anchor=0):
 if type(anchor) not in (float,int) or not math.isfinite(anchor) or anchor<0:raise ValueError('Invalid arrangement start time.')
 info=sf.info(path)
 if info.samplerate!=48000 or info.channels!=2:raise ValueError('Use a 48 kHz stereo Master render; other formats are not yet verified for whole-arrangement profiling.')
 if not 3<=info.duration<=1800:raise ValueError('Use a Master render between 3 seconds and 30 minutes.')
 digest=ingest.digest(path);windows=[]
 with sf.SoundFile(path) as f:
  whole=dsp.meter_blocks(f.blocks(blocksize=4800,dtype='float64',always_2d=True),48000,2)
  for index,start in enumerate(starts(info.frames)):
   f.seek(start);pcm=f.read(144000,dtype='float64',always_2d=True);m=dsp.analyse(pcm)
   windows.append({'id':f'w{index:05d}','startSeconds':anchor+start/48000,'endSeconds':anchor+(start+len(pcm))/48000,'fileStartSeconds':start/48000,'measurement':m})
 if ingest.digest(path)!=digest:raise ValueError('Master audio changed while profiling; results discarded.')
 return {'schemaVersion':1,'dspVersion':dsp.VERSION,'audioHash':digest,'sampleRate':48000,'durationSeconds':info.duration,'startSeconds':anchor,'endSeconds':anchor+info.duration,'whole':whole,'windows':windows,'coverage':'entire supplied file; 3-second windows at 1-second hops with final tail covered','arrangementCoverageVerified':False,'provenance':'user-supplied Master render; full arrangement and post-master provenance require user verification','trackAttribution':'Master only; cannot assign a finding to kick/bass or another track','automaticSections':False,'automaticEditsAllowed':False}

def upload(stream,size,name):
 if Path(name).suffix.lower() not in ('.wav','.aif','.aiff'):raise ValueError('Choose WAV or AIFF.')
 r=root();r.mkdir(parents=True,exist_ok=True);ident=uuid.uuid4().hex;p=r/(ident+Path(name).suffix.lower())
 try:
  with p.open('xb') as f:
   remaining=size
   while remaining:
    b=stream.read(min(1024*1024,remaining))
    if not b:raise ValueError('Upload interrupted.')
    f.write(b);remaining-=len(b)
  info=sf.info(p)
  if info.samplerate!=48000 or info.channels!=2 or not 3<=info.duration<=1800:raise ValueError('Whole-profile import requires a 48 kHz stereo Master render, 3 seconds to 30 minutes.')
  ingest.write(r/(ident+'-source.json'),{'id':ident,'name':Path(name).name,'path':str(p),'hash':ingest.digest(p)})
  return {'sourceId':ident,'message':'Master render imported locally. Confirm its arrangement start and provenance before profiling.'}
 except Exception:
  p.unlink(missing_ok=True);raise

def load(ident,r=None):
 if not isinstance(ident,str) or len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):raise ValueError('Invalid profile ID')
 return json.loads(((r or root())/(ident+'.json')).read_text())

def build(body):
 if body.get('masterRenderConfirmed') is not True:raise ValueError('Confirm this is the full post-processing Master render.')
 ident=body.get('sourceId');r=root();r.mkdir(parents=True,exist_ok=True)
 if not isinstance(ident,str) or len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):raise ValueError('Invalid source ID')
 source=json.loads((r/(ident+'-source.json')).read_text());anchor=body.get('startSeconds',0)
 def work():
  if ingest.digest(source['path'])!=source['hash']:raise ValueError('Imported Master changed.')
  profile=measure(source['path'],anchor);profile.update(id=ident,source=source,masterRenderUserConfirmed=True)
  ingest.write(r/(ident+'.json'),profile)
  return {'profileId':ident,'message':f"Measured the complete supplied Master render in {len(profile['windows'])} overlapping windows. Arrangement provenance is user-confirmed, not independently verified."}
 return reference_ab.queue(work,'whole-arrangement-profile',{'sourceId':ident,'startSeconds':anchor})

def context(profile):
 # Allowlist: filenames, stored feedback, preferences and ordinary chat never enter a blind run.
 return {'profileId':profile['id'],'audioHash':profile['audioHash'],'dspVersion':profile['dspVersion'],'coverage':profile['coverage'],'provenance':profile['provenance'],'whole':{k:profile['whole'][k] for k in ('integratedLUFS','truePeakDbTP','loudnessRangeLU')},'windows':[{'id':w['id'],'startSeconds':w['startSeconds'],'endSeconds':w['endSeconds'],'integratedLUFS':w['measurement']['integratedLUFS'],'truePeakDbTP':w['measurement']['truePeakDbTP'],'crestFactorDb':w['measurement']['crestFactorDb'],'bands':w['measurement']['bands']} for w in profile['windows']],'policy':'Measurements describe this Master only. No automatic sections, causal track attribution or edits. Unsupported perceptual comments are valid outcomes.'}

def finding(profile,text,window_ids,confidence):
 if not isinstance(text,str) or not text.strip() or len(text)>4000:raise ValueError('Finding needs bounded text.')
 if confidence not in ('supported','tentative','not-supported'):raise ValueError('Choose an evidence confidence.')
 if not isinstance(window_ids,list) or len(window_ids)>100:raise ValueError('Invalid window references')
 by_id={w['id']:w for w in profile['windows']}
 if any(not isinstance(i,str) or i not in by_id for i in window_ids):raise ValueError('Finding cites an unknown measurement window')
 if confidence=='supported' and not window_ids:raise ValueError('Supported finding needs measurement evidence')
 return {'text':text,'confidence':confidence,'profileId':profile['id'],'audioHash':profile['audioHash'],'evidence':[{'windowId':i,'startSeconds':by_id[i]['startSeconds'],'endSeconds':by_id[i]['endSeconds'],'fileStartSeconds':by_id[i]['fileStartSeconds']} for i in dict.fromkeys(window_ids)],'navigation':'local render cue only; Live bar/beat mapping UNVERIFIED','automaticEditsAllowed':False}

def cue(ident,window_id):
 import io
 profile=load(ident);w=next((w for w in profile['windows'] if w['id']==window_id),None)
 if not w:raise ValueError('Unknown evidence window')
 path=profile['source']['path']
 if ingest.digest(path)!=profile['audioHash']:raise ValueError('Measured Master has changed; cue withheld')
 y,sr=sf.read(path,start=round(w['fileStartSeconds']*48000),frames=144000,dtype='float64',always_2d=True)
 if ingest.digest(path)!=profile['audioHash']:raise ValueError('Master changed during cue read; cue withheld')
 # Conservative local audition, explicitly not a change to the measured Master.
 peak=float(abs(y).max());gain=min(1,.25/peak) if peak else 1;y*=gain
 out=io.BytesIO();sf.write(out,y,sr,format='WAV',subtype='FLOAT');return out.getvalue()

def inventory():
 r=root();rows=[];errors=[]
 for path in sorted(r.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
  if len(path.stem)!=32:continue
  try:
   p=load(path.stem,r);rows.append({'id':p['id'],'name':p['source']['name'],'durationSeconds':p['durationSeconds']})
  except (ValueError,OSError,KeyError):
   errors.append("A saved profile is unreadable and has been retained without changes.");continue
  if len(rows)>=40:break
 return {'profiles':rows,'errors':errors}
