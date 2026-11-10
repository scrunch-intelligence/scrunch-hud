"""Offline capture boundary. A future in-DAW tap implements a separate adapter."""
import json,subprocess,time,threading,hashlib,re,os
from pathlib import Path
import ingest,engine
HELPER=Path(__file__).parent/'native/capture_export'
JOURNAL=engine.DATA/'export-recovery.json'
RECOVERY_LOCK=threading.RLock()
TERMINAL={'restored','manual-verified','user-resolved-unverified'}
def durable_write(path,value):
 from transaction_store import Store
 path=Path(path);Store(path.parent).write(path,value)

def recovery_status():
 if not JOURNAL.exists():return {'pending':False,'status':'clear','settings':[]}
 raw=JOURNAL.read_bytes(); revision=hashlib.sha256(raw).hexdigest()
 try:
  j=json.loads(raw)
  if not isinstance(j,dict):raise ValueError('Invalid journal')
 except Exception:return {'pending':True,'status':'unreadable','revision':revision,'settings':[], 'error':'The restoration journal is unreadable. Restoration cannot be verified.'}
 rows=j.get('originalSettings')
 if rows is None:
  try:rows=json.loads(Path(j['snapshot']).read_text())
  except Exception:rows=[]
 settings=[]
 if not isinstance(rows,list):rows=[]
 for row in rows:
  if not isinstance(row,dict) or not isinstance(row.get('id'),str) or 'value' not in row:continue
  label=row['id'].split('.')[-1]
  if label in ('Bars','Beats','Subdivisions'):label=('Start ' if 'RenderStart' in row['id'] else 'Length ')+label
  label=re.sub(r'(?<=[a-z])(?=[A-Z])',' ',label)
  settings.append({'label':label,'value':row['value'],'id':row['id']})
 return {**j,'pending':j.get('status') not in TERMINAL,'revision':revision,'settings':settings}
def resolve_recovery(body):
 # Nonblocking: never queue an old human decision behind an active capture.
 if not RECOVERY_LOCK.acquire(blocking=False):raise ValueError('Capture or restoration is in progress. Try again after it finishes.')
 try:
  status=recovery_status()
  if not status.get('pending'):raise ValueError('There is no pending restoration to resolve.')
  if body.get('revision')!=status.get('revision'):raise ValueError('The journal changed. Review the current original settings before resolving it.')
  action=body.get('action')
  if action=='retry':OfflineExport().restore();return recovery_status()
  if action not in ('manual-verified','user-resolved-unverified'):raise ValueError('Unknown recovery action.')
  if body.get('acknowledge') is not True or not str(body.get('reason','')).strip():raise ValueError('Explicit acknowledgement and a recovery note are required.')
  if action=='manual-verified' and not status['settings']:raise ValueError('Original settings are unavailable; manual verification cannot be claimed.')
  raw=JOURNAL.read_bytes()
  archive=JOURNAL.parent/'export-recovery-history';archive.mkdir(exist_ok=True)
  saved=archive/(status['revision']+'.json')
  if not saved.exists():
   with saved.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  resolution={'status':action,'resolvedAt':time.time(),'resolvedBy':'user', 'reason':str(body['reason']).strip()[:2000], 'automaticRestorationVerified':False,'originalSettings':status['settings'] and [{'id':x['id'],'value':x['value']} for x in status['settings']], 'previousJournal':str(saved),'previousRevision':status['revision']}
  durable_write(archive/(status['revision']+'-resolution.json'),resolution)
  durable_write(JOURNAL,resolution)
  return recovery_status()
 finally:RECOVERY_LOCK.release()

class OfflineExport:
 name='live-offline-export'
 def command(self,mode,bar,bars,path):
  r=subprocess.run([str(HELPER),mode,str(bar),str(bars),str(path)],capture_output=True,text=True,timeout=45)
  if r.returncode:raise ValueError(r.stderr.strip()[:600] or 'Live export helper failed.')
 def restore(self):
  with RECOVERY_LOCK:return self._restore()
 def _restore(self):
  if not JOURNAL.exists():return
  j=json.loads(JOURNAL.read_text())
  if j['status'] in TERMINAL:return
  try:
   snapshot=Path(j['snapshot'])
   if j.get('originalSettings'):
    snapshot=JOURNAL.with_suffix('.original.json');durable_write(snapshot,j['originalSettings'])
   self.command('--restore',1,1,snapshot)
  except Exception as e:
   j.update(status='restoration-required',error=str(e));durable_write(JOURNAL,j)
   raise ValueError('Export settings restoration is pending: '+str(e)+'. Capture is blocked until restoration succeeds.') from e
  j.update(status='restored',restoredAt=time.time());durable_write(JOURNAL,j)
 def capture(self,name,bar,bars,dest):
  with RECOVERY_LOCK:return self._capture(name,bar,bars,dest)
 def _capture(self,name,bar,bars,dest):
  self.restore()
  snapshot=dest.with_suffix('.export-settings.json')
  self.command('--snapshot',1,1,snapshot)
  durable_write(JOURNAL,{'status':'restoration-required','snapshot':str(snapshot),'destination':str(dest),'createdAt':time.time(),'originalSettings':json.loads(snapshot.read_text())})
  try:
   self.command(name,bar,bars,dest)
   old=None;stable=0
   for _ in range(180):
    if dest.exists():
     size=dest.stat().st_size;stable=stable+1 if size==old else 0;old=size
     if stable>=3 and size>44:return
    time.sleep(1)
   raise ValueError('Export did not complete; no substitute audio was used.')
  finally:self.restore()
