"""Live instrument identity; optional saved records are not permission gates."""
import hashlib,json,uuid
from pathlib import Path

class ConfirmationRequired(ValueError):pass

def signature(r):
    def device(d):
        return {'id':d['id'],'path':d.get('path'),'name':d.get('name'),'type':d.get('type'),
                'parameters':[{k:p.get(k) for k in ('id','name','min','max','enabled','state','unit','options')} for p in d.get('parameters',[])]}
    # Values are intentionally excluded: legitimate sound edits aren't identity changes.
    return {'signatureVersion':2,'trackId':r['track']['id'],'trackName':r['track']['name'],
            'trackIndex':r['track'].get('trackIndex'),
            'chains':[{k:c.get(k) for k in ('id','path')} for c in r['rack'].get('chains',[])],
            'rack':device(r['rack']),'instrument':device(r['child'])}

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def location(adapter):
    scope=adapter.identity_scope()
    if not scope.get('projectPath') or not scope.get('hostEpoch'):raise ConfirmationRequired('Ableton is not reporting which project is open, so instrument changes are paused. Reconnect Ableton to continue.')
    path=Path(scope['projectPath']);return scope,path.parent/'.scrunch'/'instrument-identity.json'
def read(path):
    if not path.exists():return {'version':1,'projectId':uuid.uuid4().hex,'bindings':{}}
    try:
        d=json.loads(path.read_text())
        if d['version']!=1 or not isinstance(d['bindings'],dict) or not d['projectId']:raise ValueError()
        for b in d['bindings'].values():
            if not isinstance(b,dict) or not isinstance(b.get('id'),str) or type(b.get('revision')) is not int or b['revision']<1 or not isinstance(b.get('scope'),dict) or not isinstance(b.get('signature'),dict):raise ValueError()
        return d
    except (OSError,ValueError,KeyError,TypeError):raise ConfirmationRequired('This project\'s instrument record is unreadable, so its controls are paused until it\'s rebuilt. Say "rebuild instrument record" to start a fresh one from what\'s currently mapped.') from None

def write(path,data):
    from transaction_store import Store
    Store(path.parent).write(path,data)
def key(profile,track):return digest([profile,track.strip().casefold()])
def wording(profile,track):
    synth={'jup':'JUP','diva':'Diva','serum':'Serum'}[profile]
    return f'I need you to check {synth} on “{track}” before I change it. Is this still the instrument you intend to use? If so, say “Confirm {synth} on {track}”. I will check its controls again; your confirmation will not change the sound.'
def ensure(r,adapter,profile):
    scope=adapter.identity_scope()
    return {'projectId':digest(scope),'bindingId':digest([profile,scope,signature(r)]),'revision':1}
def confirm(r,adapter,profile):
    # Caller must freshly pass the structural/control checks before confirming.
    scope,path=location(adapter);data=read(path);k=key(profile,r['track']['name']);old=data['bindings'].get(k)
    sig=signature(r)
    if old and old['scope']==scope and old['signature']==sig:return ensure(r,adapter,profile)
    data['bindings'][k]={'id':old['id'] if old else uuid.uuid4().hex,'revision':old['revision']+1 if old else 1,'scope':scope,'signature':sig}
    write(path,data);return ensure(r,adapter,profile)
