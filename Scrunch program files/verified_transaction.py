"""Adapter-neutral transaction executor. Live adapter remains behind routing proof.

The adapter must provide observe(), checkpoint(), read(track_id), write(action),
and record(event). Authorisation requires planId, observationFingerprint, actionFingerprint and allow=True.
The action fingerprint covers full actions including rollback; it is an integrity
check against the separately retained approval, not a cryptographic signature.
"""
import session_plans,copy,math

def require_expected_context(plan,observation,attempted,*,uncertain=False):
 """Normalize only our verified/uncertain writes; preserve every other field."""
 if observation.get('routingAndLatencyVerified') is not True:raise ValueError('Routing and latency became UNVERIFIED')
 expected=copy.deepcopy(observation)
 for row in attempted:
  tracks=[t for t in expected.get('tracks',[]) if t.get('id')==row['target']]
  if len(tracks)!=1:raise ValueError('Attempted target identity changed')
  value=tracks[0].get(row['parameter']);allowed=(row['before'],row['after']) if uncertain else (row['after'],)
  if type(value) not in (int,float) or not math.isfinite(value) or not any(abs(value-v)<.001 for v in allowed):raise ValueError('Intervening edit; transaction context changed')
  tracks[0][row['parameter']]=row['before']
 session_plans.require_fresh(plan,expected)

def execute(plan,adapter,authorization):
 plan=copy.deepcopy(plan)
 digest=session_plans.action_fingerprint(plan)
 current=adapter.observe();session_plans.require_fresh(plan,current)
 checkpoint=adapter.checkpoint();applied=[];record={'planId':plan['id'],'checkpoint':checkpoint,'actionFingerprint':digest,'status':'executing','changes':applied};adapter.record(record)
 def matches(actual,expected):return type(actual) in (float,int) and abs(actual-expected)<.001
 try:
  for row in plan['actions']:
   require_expected_context(plan,adapter.observe(),applied)
   before=adapter.read(row['target'])
   if before.get('id')!=row['target'] or not matches(before.get(row['parameter']),row['before']):raise ValueError('Target changed before execution')
   # Record intent before a call that may time out after completing a write.
   entry={**row,'status':'write-started'};applied.append(entry);adapter.record(record)
   adapter.write({'trackId':row['target'],'parameter':row['parameter'],'value':row['after']})
   after=adapter.read(row['target'])
   if after.get('id')!=row['target'] or not matches(after.get(row['parameter']),row['after']):raise ValueError('Mutation readback differs')
   entry['status']='verified';adapter.record(record)
  require_expected_context(plan,adapter.observe(),applied)
  record['status']='awaiting-user-decision';adapter.record(record);return record
 except Exception as error:
  record.update(status='reverting-after-failure',error=str(error),rollbackProgress=[]);adapter.record(record);conflicts=[]
  for row in reversed(applied):
   try:
    require_expected_context(plan,adapter.observe(),applied,uncertain=True)
    now=adapter.read(row['target'])
    if now.get('id')!=row['target']:raise ValueError('Target identity changed')
    value=now.get(row['parameter'])
    if matches(value,row['before']):row['status']='unchanged';continue
    if not matches(value,row['after']):raise ValueError('Concurrent edit; automatic rollback refused')
    progress={'target':row['target'],'parameter':row['parameter'],'expectedCurrent':row['after'],'restoreValue':row['before'],'status':'rollback-started'}
    record['rollbackProgress'].append(progress);adapter.record(record)
    adapter.write(row['rollback']);after=adapter.read(row['target'])
    if after.get('id')!=row['target'] or not matches(after.get(row['parameter']),row['before']):raise ValueError('Rollback readback differs')
    row['status']='reverted';progress['status']='rollback-verified';adapter.record(record)
   except Exception as rollback:conflicts.append({'target':row['target'],'error':str(rollback)})
  if not conflicts:
   try:require_expected_context(plan,adapter.observe(),[])
   except Exception as rollback:conflicts.append({'scope':'final-session-verification','error':str(rollback)})
  record['status']='recovery-required' if conflicts else 'reverted-after-failure';record['recoveryConflicts']=conflicts;adapter.record(record);return record


def decide(plan,transaction,adapter,authorization,decision):
 """Resolve a completed transaction. Adapter must persist records durably.

 Caller must serialize decisions and supply the latest durably stored transaction,
 never a client-submitted or cached record. This foundation has no Live endpoint. Recovery conflicts require
 a new observation/preview; repeated calls never silently retry a partial undo.
 """
 plan=copy.deepcopy(plan);record=copy.deepcopy(transaction)
 if decision not in ('accept','reject','revert'):raise ValueError('Choose accept, reject or revert')
 digest=session_plans.action_fingerprint(plan)
 if (plan.get('actionFingerprint')!=digest or record.get('actionFingerprint')!=digest
     or record.get('planId')!=plan['id']):raise ValueError('Transaction record does not match this plan')
 if record.get('status')!='awaiting-user-decision':raise ValueError('Transaction is not awaiting a decision; inspect its saved outcome')
 current=adapter.observe()
 # Verify the complete expected post-action session, not only numeric targets.
 expected=copy.deepcopy(current)
 for row in plan['actions']:
  tracks=[t for t in expected.get('tracks',[]) if t.get('id')==row['target']]
  if len(tracks)!=1:raise ValueError('Decision target is missing or ambiguous')
  value=tracks[0].get(row['parameter'])
  if type(value) not in (float,int) or not math.isfinite(value) or abs(value-row['after'])>=.001:raise ValueError('Intervening edit; decision refused without changes')
  tracks[0][row['parameter']]=row['before']
 session_plans.require_fresh(plan,expected)
 if decision=='accept':
  record.update(status='accepted',decision=decision);adapter.record(record);return record
 record.update(status='reverting',decision=decision,undoProgress=[]);adapter.record(record)
 conflicts=[];remaining=list(plan['actions'])
 for row in reversed(plan['actions']):
  try:
   require_expected_context(plan,adapter.observe(),remaining)
   now=adapter.read(row['target']);value=now.get(row['parameter'])
   if now.get('id')!=row['target'] or type(value) not in (int,float) or not math.isfinite(value) or abs(value-row['after'])>=.001:raise ValueError('Intervening edit; undo refused')
   progress={'target':row['target'],'parameter':row['parameter'],'expectedCurrent':row['after'],'restoreValue':row['before'],'status':'undo-started'}
   record['undoProgress'].append(progress);adapter.record(record)
   adapter.write(row['rollback'])
   after=adapter.read(row['target']);value=after.get(row['parameter'])
   if after.get('id')!=row['target'] or type(value) not in (int,float) or not math.isfinite(value) or abs(value-row['before'])>=.001:raise ValueError('Undo readback differs')
   remaining.remove(row)
   progress['status']='undo-verified';adapter.record(record)
  except Exception as error:
   conflicts.append({'target':row['target'],'parameter':row['parameter'],'error':str(error)})
   break
 if not conflicts:
  try:require_expected_context(plan,adapter.observe(),[])
  except Exception as error:conflicts.append({'scope':'final-session-verification','error':str(error)})
 record.update(status='recovery-required' if conflicts else 'rejected-and-reverted' if decision=='reject' else 'reverted',recoveryConflicts=conflicts)
 adapter.record(record);return record
