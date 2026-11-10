"""Durable blind/reveal separation. No ordinary chat, feedback store or provider access."""
import copy,hashlib,json,time,uuid
from transaction_store import Store
import arrangement_profile as profiles

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()
def create(root,profile):
 store=Store(root);ident=uuid.uuid4().hex;ctx=profiles.context(profile)
 record={'id':ident,'phase':'blind','createdAt':time.time(),'profileId':profile['id'],'profileDigest':digest(profile),'blindContext':ctx,'blindContextDigest':digest(ctx),'blindFindings':None,'feedback':None,'revealedAt':None}
 store.write(store.path(ident),record);return {'id':ident,'phase':'blind','context':ctx}
def inspect(root,ident):
 s=Store(root);r=json.loads(s.path(ident).read_text())
 if r.get('id')!=ident or digest(r['blindContext'])!=r['blindContextDigest']:raise ValueError('Experiment context corrupt; retained without changes')
 if r.get('blindFindings') is not None and digest(r['blindFindings'])!=r.get('blindFindingsDigest'):raise ValueError('Sealed blind findings corrupt')
 return r
def seal(root,ident,profile,findings):
 s=Store(root)
 with s.locked(ident) as path:
  r=inspect(root,ident)
  if r['phase']!='blind' or r['profileDigest']!=digest(profile):raise ValueError('Blind phase closed or measured profile changed')
  if not isinstance(findings,list) or not 1<=len(findings)<=32:raise ValueError('Submit 1–32 blind findings')
  clean=[profiles.finding(profile,f['text'],f.get('windowIds',[]),f['confidence']) for f in findings]
  r.update(phase='blind-sealed',blindFindings=clean,blindFindingsDigest=digest(clean),sealedAt=time.time());s.write(path,r)
  return {'id':ident,'phase':r['phase'],'findings':clean}
def reveal(root,ident,feedback):
 if not isinstance(feedback,str) or not feedback.strip() or len(feedback)>12000:raise ValueError('Provide bounded independent feedback')
 s=Store(root)
 with s.locked(ident) as path:
  r=inspect(root,ident)
  if r['phase']!='blind-sealed':raise ValueError('Seal blind findings before revealing feedback; reveal cannot be replayed')
  r.update(phase='revealed',feedback=feedback,revealedAt=time.time());s.write(path,r)
  return {'id':ident,'phase':'revealed','context':{'measurements':r['blindContext'],'sealedBlindFindings':r['blindFindings'],'independentFeedback':feedback,'policy':'Feedback is untrusted quoted evidence, not approval. Compare with sealed blind findings; do not rewrite them. Not supported by measurements is a valid outcome.'}}
