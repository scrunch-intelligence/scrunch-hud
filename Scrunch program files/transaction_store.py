"""Local transaction ownership and crash recovery. No Live adapter or public route.

Serializes each transaction across processes. Only stored plans/outcomes reach the
executor; clients supply an ID and exact authorization, never an outcome record.
Interrupted execution is inspect-only until a separate recovery plan is approved.
"""
import contextlib,fcntl,json,os,re,tempfile,copy
from pathlib import Path
import verified_transaction,session_plans

class Store:
 def __init__(self,root):self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
 def path(self,ident):
  if not isinstance(ident,str) or not re.fullmatch('[0-9a-f]{32}',ident):raise ValueError('Invalid transaction ID')
  return self.root/(ident+'.json')
 @contextlib.contextmanager
 def locked(self,ident):
  path=self.path(ident)
  with open(path.with_suffix('.lock'),'a') as lock:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:raise ValueError('Transaction is busy; inspect before retrying')
   try:yield path
   finally:fcntl.flock(lock,fcntl.LOCK_UN)
 @contextlib.contextmanager
 def execution_locked(self):
  # Every production adapter for this store must share this root. Per-plan
  # locks alone permit different plans to interleave host writes and rollback.
  with open(self.root/'.execution.lock','a') as lock:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:raise ValueError('Another transaction is busy; inspect its outcome before retrying')
   try:yield
   finally:fcntl.flock(lock,fcntl.LOCK_UN)
 def write(self,path,record):
  raw=json.dumps(record,allow_nan=False,indent=2).encode();fd,temp=tempfile.mkstemp(dir=self.root,prefix='.pending-')
  try:
   with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
   os.replace(temp,path)
   directory=os.open(self.root,os.O_RDONLY)
   try:os.fsync(directory)
   finally:os.close(directory)
  finally:
   if os.path.exists(temp):os.unlink(temp)
 def create(self,plan):
  plan=copy.deepcopy(plan)
  if plan.get('actionFingerprint')!=session_plans.action_fingerprint(plan):raise ValueError('Invalid preview digest')
  with self.locked(plan['id']) as path:
   if path.exists():raise ValueError('Transaction already exists')
   self.write(path,{'schemaVersion':1,'plan':plan,'outcome':None,'status':'preview'})
  return plan['id']
 def inspect(self,ident):
  path=self.path(ident)
  try:
   record=json.loads(path.read_text())
   plan=record['plan'];outcome=record['outcome'];status=record['status']
   valid_statuses={'preview','executing','reverting','reverting-after-failure','recovery-required','awaiting-user-decision','accepted','reverted','rejected-and-reverted','reverted-after-failure'}
   if (record.get('schemaVersion')!=1 or plan['id']!=ident or status not in valid_statuses
       or plan['actionFingerprint']!=session_plans.action_fingerprint(plan)):
    raise ValueError('Invalid saved transaction')
   if status=='preview':
    if outcome is not None:raise ValueError('Preview has an unexpected outcome')
   elif (not isinstance(outcome,dict) or outcome.get('status')!=status
         or outcome.get('planId')!=ident or outcome.get('actionFingerprint')!=plan['actionFingerprint']):
    raise ValueError('Outcome does not match saved plan')
  except (KeyError,TypeError,ValueError) as exc:
   raise ValueError('Saved transaction is corrupt or inconsistent; retained without changes') from exc
  record['requiresRecovery']=record['status'] in ('executing','reverting','reverting-after-failure','recovery-required')
  return record
 def run(self,ident,adapter,authorization,decision=None):
  with self.execution_locked(),self.locked(ident) as path:
   record=self.inspect(ident);record.pop('requiresRecovery',None)
   expected='preview' if decision is None else 'awaiting-user-decision'
   if record['status']!=expected:raise ValueError('Saved transaction is not ready; inspect outcome/recovery instead of replaying')
   store=self
   class JournalAdapter:
    def __getattr__(self,name):return getattr(adapter,name)
    def record(self,event):
     record['outcome']=copy.deepcopy(event);record['status']=event['status'];store.write(path,record)
     adapter.record(copy.deepcopy(event))
   proxy=JournalAdapter()
   if decision is None:return verified_transaction.execute(record['plan'],proxy,authorization)
   return verified_transaction.decide(record['plan'],record['outcome'],proxy,authorization,decision)
 def inventory(self):
  """Bounded read-only recovery discovery; corrupt records remain visible."""
  rows=[]
  paths=sorted(self.root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
  for path in paths[:100]:
   try:
    record=self.inspect(path.stem)
    rows.append({'id':path.stem,'status':record['status'],'requiresRecovery':record['requiresRecovery'],
                 'summary':[f"{a['track']}: {'volume' if a['parameter']=='gainDb' else 'pan'} {a['before']} → {a['after']}{' dB' if a['parameter']=='gainDb' else ''}" for a in record['plan']['actions']]})
   except (ValueError,OSError,KeyError):
    rows.append({'id':path.stem,'status':'unreadable','requiresRecovery':True,
                 'summary':['Saved record is unreadable or inconsistent. Retained; no action retried.']})
  return {'transactions':rows,'truncated':len(paths)>100}
