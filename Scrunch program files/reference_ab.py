"""Project-local post-fader capture and manual reference comparison transactions."""
import json,uuid,subprocess,time,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import engine,ambient_analysis,ingest,reference_dsp as dsp
import soundfile as sf
import capture_adapter
LOCK=threading.RLock();POOL=ThreadPoolExecutor(max_workers=1);JOB={'status':'idle'}
JOBFILE=engine.DATA/'reference-job.json'
if JOBFILE.exists():
 JOB=json.loads(JOBFILE.read_text())
 if JOB.get('status')=='running':
  JOB.update(status='interrupted',error='Server restarted during this job. No action was retried; inspect capture recovery before continuing.');ingest.write(JOBFILE,JOB)
def persist_job():ingest.write(JOBFILE,dict(JOB))
def project():
 live=engine.host().connect();s=engine.host().read_live_set('tracks')
 path,folder=ambient_analysis.project_path(s['name']);root=ambient_analysis.storage(folder)/'reference-ab';root.mkdir(parents=True,exist_ok=True)
 return s,path,root
def manifest(root):
 p=root/'library.json';return json.loads(p.read_text()) if p.exists() else {'captures':[],'references':[],'reports':[]}
def state():
 try:
  s,p,root=project();r=manifest(root)
  return {**r,'project':str(p),'targets':[{'id':'master','name':'Master (post-fader)'}]+[{'id':x['id'],'name':x['name']} for x in s.get('tracks',[]) if x.get('type') in ('audio','midi','group')], 'job':dict(JOB)}
 except Exception as e:return {'error':str(e),'job':dict(JOB),'captures':[],'references':[],'reports':[],'targets':[]}
def save_asset(root,path,kind,meta):
 info=sf.info(path)
 if info.channels not in (1,2) or info.frames<1:raise ValueError('Expected nonempty mono/stereo audio.')
 row={'id':uuid.uuid4().hex,'name':Path(path).stem,'path':str(path),'hash':ingest.digest(path),'duration':info.duration,'sampleRate':info.samplerate,**meta}
 r=manifest(root);r[kind].append(row)
 if kind=='references':r[kind]=r[kind][-2:]
 ingest.write(root/'library.json',r);return row

def queue(fn,kind,parameters):
 with LOCK:
  if JOB['status']=='running':raise ValueError('A capture or comparison is already running.')
  JOB.clear();JOB.update(status='running',id=uuid.uuid4().hex,kind=kind,parameters=parameters,startedAt=time.time());persist_job()
 def run():
  try:
   r=fn()
   with LOCK:JOB.update(status='complete',result=r,finishedAt=time.time());persist_job()
  except Exception as e:
   with LOCK:JOB.update(status='failed',error=str(e)[:600],finishedAt=time.time());persist_job()
 POOL.submit(run);return {'message':'Working in the background.','status':'running'}
def capture(body):
 def work():
  s,p,root=project();ident=str(body.get('target','master'));bar=int(body.get('bar',1));bars=int(body.get('bars',4))
  if not 1<=bar<=100000 or not 1<=bars<=64:raise ValueError('Choose valid start and length in bars.')
  tr=next((t for t in s.get('tracks',[]) if t['id']==ident),None)
  if ident!='master' and not tr:raise ValueError('Selected track is no longer available.')
  name='Main' if ident=='master' else tr['name']
  if tr and sum(t['name']==name for t in s['tracks'])!=1:raise ValueError('Track names must be unique for verified export selection.')
  engine.stop();s2,p2,_=project()
  if p2!=p:raise ValueError('Live Set changed before capture.')
  if tr:engine.host().read_track_detail(ident,'devices','mixer')
  dest=root/(uuid.uuid4().hex+'.wav')
  capture_adapter.OfflineExport().capture(name,bar,bars,dest)
  if project()[1]!=p:raise ValueError('Project changed during capture; output not accepted.')
  info=sf.info(dest)
  if info.samplerate!=48000 or info.channels!=2 or info.subtype!='FLOAT':raise ValueError('Capture format readback failed; expected 48 kHz stereo 32-bit float.')
  row=save_asset(root,dest,'captures',{'origin':'live-post-fader-export','trackId':ident,'trackName':name,'startBar':bar,'lengthBars':bars,'normalize':False,'mono':False,'includesReturnAndMasterEffects':ident=='master','verification':'export-settings-readback-and-file-decode','tempo':s.get('tempo')})
  engine.stop();return {'message':'Captured '+name+' after processing. Choose the reference cue.','capture':row}
 return queue(work,'capture',{k:body.get(k) for k in ('target','bar','bars')})
def readcue(row,start,duration):
 path=Path(row['path'])
 if not path.is_file() or ingest.digest(path)!=row['hash']:raise ValueError('Audio is missing or changed; capture/load it again.')
 info=sf.info(path);first=round(start*info.samplerate);count=round(duration*info.samplerate)
 if start<0 or first+count>info.frames:raise ValueError('The selected cue extends outside its audio file.')
 y,sr=sf.read(path,start=first,frames=count,always_2d=True,dtype='float64')
 if ingest.digest(path)!=row['hash']:raise ValueError('Audio changed during measurement; comparison discarded.')
 return y,sr

def compare(body):
 def work():
  s,p,root=project();r=manifest(root)
  a=next((x for x in r['captures'] if x['id']==body.get('capture')),None);b=next((x for x in r['references'] if x['id']==body.get('reference')),None)
  if not a or not b:raise ValueError('Choose a Live capture and external reference first.')
  starta=float(body.get('liveCue',0));startb=float(body.get('referenceCue',0));duration=float(body.get('duration',8))
  if not 3<=duration<=120:raise ValueError('Choose 3–120 seconds for comparison.')
  ay,ar=readcue(a,starta,duration);by,br=readcue(b,startb,duration);report,aa,bb=dsp.compare(ay,ar,by,br)
  if project()[1]!=p:raise ValueError('Live Set changed during comparison; result discarded.')
  ident=uuid.uuid4().hex;report.update(id=ident,liveSource=a,referenceSource=b,manualCues={'liveSeconds':starta,'referenceSeconds':startb,'durationSeconds':duration},project=str(p))
  sf.write(root/(ident+'-A.wav'),aa,48000,subtype='FLOAT');sf.write(root/(ident+'-B.wav'),bb,48000,subtype='FLOAT')
  ingest.write(root/(ident+'.json'),report);r=manifest(root);r['reports'].append({'id':ident,'name':a['trackName']+' vs '+b['name']});ingest.write(root/'library.json',r)
  return {'message':english(report),'reportId':ident}
 return queue(work,'comparison',{k:body.get(k) for k in ('capture','reference','liveCue','referenceCue','duration')})
def report(ident=None):
 _,_,root=project();r=manifest(root)
 if not r['reports']:raise ValueError('No reference comparison yet. Capture, choose a reference and set your cues first.')
 ident=ident or r['reports'][-1]['id']
 if not any(x['id']==ident for x in r['reports']):raise ValueError('Unknown report.')
 result=json.loads((root/(ident+'.json')).read_text());result['summary']=english(result);return result
def audio(ident,side):
 if side not in ('A','B'):raise ValueError('Unknown audition side.')
 report(ident);_,_,root=project();return (root/(ident+'-'+side+'.wav')).read_bytes()
def english(r):
 d=r['delta'];lines=['Reference comparison: '+r['liveSource']['trackName']+' vs '+r['referenceSource']['name'],'Manual cues: Live %.2f s; reference %.2f s; duration %.2f s.'%(r['manualCues']['liveSeconds'],r['manualCues']['referenceSeconds'],r['durationSeconds']),'Original loudness difference: %+.2f LU. Level-matched difference: %+.2f LU.'%(d['rawLoudnessLU'],d['matchedLoudnessLU'])]
 for band in d['bands']:
  if band.get('comparisonReliable') is False:continue
  if band['energyDb'] is not None:lines.append('%s: %+.2f dB vs reference after level matching.'%(band['band'],band['energyDb']))
  if band['sideFraction'] is not None and abs(band['sideFraction'])>.02:lines.append('%s: %s stereo image (side-energy share difference %+.3f).'%(band['band'],'wider' if band['sideFraction']>0 else 'narrower',band['sideFraction']))
 short=[x['loudnessLU'] for x in d.get('shortTermHistory',[]) if x['loudnessLU'] is not None]
 if short:lines.append('Mean short-term loudness difference after matching: %+.2f LU.'%(sum(short)/len(short)))
 lines.append('True peak uses libebur128; validation scope is recorded in technical details. No automatic edits.')
 return '\n'.join(lines)

def upload(stream,size,name):
 s,p,root=project();name=Path(name).name
 if Path(name).suffix.lower() not in ('.wav','.aif','.aiff'):raise ValueError('Choose WAV or AIFF audio.')
 dest=root/(uuid.uuid4().hex+Path(name).suffix.lower())
 try:
  with dest.open('xb') as f:
   remaining=size
   while remaining:
    chunk=stream.read(min(1048576,remaining))
    if not chunk:raise ValueError('Reference upload was interrupted.')
    f.write(chunk);remaining-=len(chunk)
  if project()[1]!=p:raise ValueError('Project changed while loading reference.')
  with LOCK:row=save_asset(root,dest,'references',{'origin':'external-reference','name':Path(name).stem})
  return {'message':'Reference loaded: '+Path(name).stem,'reference':row}
 except Exception:
  dest.unlink(missing_ok=True);raise

def recover_capture_settings():
 def recover():
  try:capture_adapter.OfflineExport().restore()
  except Exception as e:
   with LOCK:JOB.update(status='failed',error=str(e));persist_job()
 POOL.submit(recover)
