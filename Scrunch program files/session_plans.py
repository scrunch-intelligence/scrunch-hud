"""Stage 3/4 read-only planning boundary. No DAW mutations in this module."""
import hashlib,json,math,uuid,copy

def fingerprint(observation):
 if not observation.get('available'):raise ValueError('Complete current session observation required')
 try:raw=json.dumps({'session':observation.get('session'),'tracks':observation['tracks'],'markers':observation.get('markers')},sort_keys=True,separators=(',',':'),allow_nan=False)
 except (ValueError,TypeError):raise ValueError('Session observation contains unavailable or nonfinite values') from None
 return hashlib.sha256(raw.encode()).hexdigest()

def preview(observation,actions):
 before=fingerprint(observation)
 if not isinstance(actions,list) or not 1<=len(actions)<=16:raise ValueError('Preview requires 1–16 actions')
 rows=[];targets=set()
 for action in actions:
  if not isinstance(action,dict) or set(action)!={'trackId','parameter','value'}:raise ValueError('Only explicit mixer previews are supported')
  matches=[t for t in observation['tracks'] if t.get('id')==action['trackId']]
  if len(matches)!=1:raise ValueError('Track identity is missing or ambiguous')
  track=matches[0];key=action['parameter'];value=action['value']
  if key=='gainDb':
   if type(value) not in (int,float) or not math.isfinite(value) or not -70<=value<=0:raise ValueError('Preview gain must be -70 to 0 dB')
  elif key=='pan':
   if type(value) not in (int,float) or not math.isfinite(value) or not -1<=value<=1:raise ValueError('Preview pan must be -1 to 1')
  else:raise ValueError('Unsupported mixer preview parameter')
  if key not in track or type(track[key]) not in (int,float) or not math.isfinite(track[key]):raise ValueError('Original mixer value is unavailable')
  identity=(track['id'],key)
  if identity in targets:raise ValueError('Repeated target parameter requires one final preview value')
  targets.add(identity)
  rows.append({'target':track['id'],'track':track.get('name'),'parameter':key,'before':track[key],'after':value,'description':f"Set {track.get('name')} {key} from {track[key]} to {value}",'routing':{k:track.get(k) for k in ('inputRoutingType','outputRoutingType','sends')},'rollback':{'trackId':track['id'],'parameter':key,'value':track[key]}})
 plan={'id':uuid.uuid4().hex,'mode':'dry-run','observationFingerprint':before,'actions':rows,'executed':False,'actAvailable':False,'gate':'UNVERIFIED: preview execution/rollback and complete routing/latency awareness require lab proof. Existing bounded controls are unchanged.'}

 plan['actionFingerprint']=action_fingerprint(plan)
 return plan

def action_fingerprint(plan):
 return hashlib.sha256(json.dumps({'id':plan['id'],'observationFingerprint':plan['observationFingerprint'],'actions':plan['actions']},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def require_fresh(plan,observation):
 if plan['observationFingerprint']!=fingerprint(observation):raise ValueError('Session changed since preview; rebuild the plan')
 return True

def architecture_preview(nodes,edges):
 """Validate a proposed topology before an adapter translates symbolic IDs."""
 if not isinstance(nodes,list) or not 1<=len(nodes)<=64 or not isinstance(edges,list) or len(edges)>256:raise ValueError('Architecture size is outside prototype limits')
 if any(not isinstance(n,dict) for n in nodes) or any(not isinstance(e,dict) for e in edges):raise ValueError('Track and routing records must be objects')
 identifiers=[n.get('id') for n in nodes]
 if any(not isinstance(i,str) or not i for i in identifiers) or len(set(identifiers))!=len(identifiers):raise ValueError('Unique symbolic node identities required')
 if any(n.get('kind') not in ('audio','midi','group','return','master','analysis-tap') for n in nodes):raise ValueError('Unknown track kind')
 if sum(n['kind']=='master' for n in nodes)>1:raise ValueError('Only one master can be represented')
 kinds={n['id']:n['kind'] for n in nodes};connections=set();outputs=set()
 graph={i:[] for i in identifiers}
 for edge in edges:
  if edge.get('from') not in graph or edge.get('to') not in graph:raise ValueError('Routing target is missing')
  if edge.get('kind') not in ('output','send','sidechain'):raise ValueError('Unknown connection type')
  source,target,kind=edge['from'],edge['to'],edge['kind']
  identity=(source,target,kind)
  if identity in connections:raise ValueError('Duplicate routing connection')
  connections.add(identity)
  if kinds[source]=='analysis-tap':raise ValueError('Analysis tap must not have an audible route')
  if kinds[source]=='master' and kinds[target]!='analysis-tap':raise ValueError('Master routing back into the mix is unsupported')
  if kind=='send' and kinds[target]!='return':raise ValueError('Send destination must be a return track')
  if kinds[target]=='analysis-tap' and kind!='output':raise ValueError('Passive tap requires an explicit observation output')
  if kind=='output' and kinds[target]!='analysis-tap':
   if source in outputs:raise ValueError('Multiple main outputs require unsupported routing semantics')
   outputs.add(source)
  if edge['kind']=='send' and (type(edge.get('gainDb')) not in (float,int) or not math.isfinite(edge['gainDb']) or not -70<=edge['gainDb']<=0):raise ValueError('Explicit conservative send gain required')
  graph[edge['from']].append(edge['to'])
 visiting=set();done=set()
 def visit(node):
  if node in visiting:raise ValueError('Potential feedback cycle: architecture refused')
  if node in done:return
  visiting.add(node)
  for target in graph[node]:visit(target)
  visiting.remove(node);done.add(node)
 for node in graph:visit(node)
 for node in nodes:
  if node['kind']=='analysis-tap' and graph[node['id']]:raise ValueError('Analysis tap must not have an audible route')
 result={'mode':'dry-run','nodes':copy.deepcopy(nodes),'connections':copy.deepcopy(edges),'executed':False,'actAvailable':False,'limitations':['Symbolic topology only; no tracks created.','Routing IDs, sends, sidechain support, plugin availability, PDC and rollback need host readback and lab acceptance.','Existing compressors/EQ/limiters are used by future adapter plans; no new processing DSP.']}
 result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()
 return result
