import json, os, threading, uuid, math
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
from music import validate, fingerprint, compare, notation, midi_bytes, position_beats, position_text
from providers import generate
from keychain import operation
import daw_adapter
import workflow_requirements as needs

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('SCRUNCH_DATA',str(ROOT/'data')))
DATA.mkdir(parents=True,exist_ok=True)
LOCK=threading.RLock()
PLAY_LOCK=threading.RLock()
DEFAULT={'version':1,'providers':[],'source':None,'attempts':[],
 'memory':''}
STATE=json.loads((DATA/'state.json').read_text()) if (DATA/'state.json').exists() else DEFAULT
AUDITION=None

def save(*,write_handoff=True):
    from transaction_store import Store
    store=Store(DATA)
    store.write(DATA/'state.json',STATE)
    if not write_handoff:return
    src=STATE['source']
    handoff={'updated':datetime.now(timezone.utc).isoformat(),'source':{k:v for k,v in (src or {}).items() if k!='notes'},
      'memory':STATE['memory'],'recentAttempts':[{k:a.get(k) for k in ('id','model','status','reply','feedback','live')} for a in STATE['attempts'][-8:]],
      'next':'Review latest variation; audition one at a time. Use dashboard or localhost API. Never overwrite source.'}
    store.write(DATA/'HANDOFF.json',handoff)

def host():
    """The active DAW backend. Core code uses only its Scrunch contract."""
    return daw_adapter.DEFAULT

def require_lab(adapter=None):
    """Legacy entrypoint: connect to whichever Set the producer has open."""
    return (adapter or host()).connect()

def lab_session(adapter=None):
    """Contract-shaped current session, without a project allowlist."""
    require_lab(adapter); return (adapter or host()).session()

def live_set():
    """Residual raw-shape session for device/analysis layers (see DECOUPLING-PHASE1.md)."""
    require_lab(); return host().read_live_set('tracks')

def list_clips():
    ls=lab_session(); clips=[]
    for tr in ls['tracks']:
        if tr.get('arrangementClipCount') and tr['kind']=='midi':
            t=host().track(tr['id'],clips=True)
            for c in t.get('clips',[]):
                clips.append({'id':c['id'],'name':c['name'],'trackId':t['id'],'trackName':t['name'],
                  'trackIndex':t['trackIndex'],'instrument':t.get('instrument','No instrument')})
    return {'name':ls['name'],'tempo':ls['tempo'],'clips':clips}

def clip_notes(clip,length):
    if type(length) not in (int,float) or not math.isfinite(length) or length<=0:
        raise ValueError('The host returned an invalid clip duration.')
    offset=clip.get('offset') or 0
    if not clip.get('looping') and not offset:
        return validate(clip['notes'],length)
    segments=[]
    if clip.get('looping'):
        left,right=clip.get('loopStart'),clip.get('loopEnd')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (left,right,offset)) or not right>left:
            raise ValueError('The host did not return the clip loop markers; reconnect the current bridge.')
        cursor=offset;elapsed=0
        if cursor>=right:cursor=left+(cursor-left)%(right-left)
        while elapsed<length:
            span=min(right-cursor,length-elapsed)
            segments.append((cursor,cursor+span,elapsed));elapsed+=span;cursor=left
    else:segments=[(offset,offset+length,0)]
    notes=[]
    for left,right,at in segments:
        for n in clip['notes']:
            start=max(left,n['t']);end=min(right,n['t']+n['d'])
            if end>start:notes.append({**n,'t':at+start-left,'d':end-start})
    return validate(notes,length)

def load_source(clip_id):
    ls=lab_session()
    candidates=list_clips()['clips']; c=next((c for c in candidates if c['id']==clip_id),None)
    if not c: raise ValueError('Clip no longer exists. Refresh the list.')
    raw=host().clip(clip_id)
    if raw.get('start') is None:raise ValueError('The host did not return this clip\'s Arrangement position.')
    length=raw.get('span') or raw.get('length')
    notes=clip_notes(raw,length)
    STATE['source']={**c,'sessionId':STATE.get('sessionId','lab'),'notes':notes,'length':length,'tempo':ls['tempo'],
      'arrangementStart':position_text(raw['start']),'timeSignature':raw.get('timeSignature','4/4'),'hash':fingerprint(notes)}
    (DATA/('original-'+STATE['source']['hash'][:12]+'.mid')).write_bytes(midi_bytes(notes,length,ls['tempo']))
    save(); return STATE['source']

def provider_save(body):
    name=str(body.get('name','')).strip()[:80]; kind=body.get('kind'); model=str(body.get('model','')).strip()[:160]
    endpoints={'openai':'https://api.openai.com/v1','anthropic':'https://api.anthropic.com/v1',
               'gemini':'https://generativelanguage.googleapis.com/v1beta'}
    endpoint=endpoints.get(kind,str(body.get('endpoint','')).strip().rstrip('/'))
    u=urlparse(endpoint)
    if kind not in (*endpoints,'compatible') or not name or not model: raise ValueError('Name, adapter and model ID are required.')
    if u.username or u.password or u.query or u.fragment or not u.hostname or (u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('localhost','127.0.0.1'))):
        raise ValueError('Use HTTPS, or a localhost HTTP endpoint, without embedded credentials.')
    existing=next((p for p in STATE['providers'] if p['id']==body.get('id')),None)
    if existing and (existing['endpoint']!=endpoint or existing['kind']!=kind):
        raise ValueError('Create a new profile to change the endpoint. Keys are tied to their original provider.')
    config={'id':existing['id'] if existing else uuid.uuid4().hex,'name':name,'kind':kind,'endpoint':endpoint,'model':model,
            'hasKey':existing.get('hasKey',False) if existing else False}
    key=str(body.get('key','')).strip()
    if key: operation(config['id'],key); config['hasKey']=True
    if existing: STATE['providers'][STATE['providers'].index(existing)]=config
    else: STATE['providers'].append(config)
    save(); return config

def attempt(body):
    src=STATE['source']
    if not src: raise ValueError('Choose a source clip first.')
    prompt=str(body.get('prompt','')).strip()
    if not prompt or len(prompt)>6000: raise ValueError('Enter an instruction of 1–6000 characters.')
    profile=next((p for p in STATE['providers'] if p['id']==body.get('provider')),None)
    previous=[a for a in STATE['attempts'] if a['source']['hash']==src['hash'] and a['source'].get('sessionId','lab')==src.get('sessionId','lab')]
    context={'source':src,'preferences':STATE['memory'],'instruction':prompt,
       'recentFeedback':[{k:a.get(k) for k in ('model','prompt','reply','status','feedback')} for a in previous[-6:]],
       'previousVariation':previous[-1]['notes'] if previous else None}
    usage={}
    if body.get('mode')=='rehearsal':
        # Deliberately labelled deterministic test; never presented as model output.
        notes=[dict(n) for n in src['notes']]
        for i in range(11,len(notes),12): notes[i]['v']=max(1,notes[i]['v']-12)
        response={'reply':'Local rehearsal only: softened phrase endings. No model called; pitch and groove retained.','notes':notes}
        model='Local rehearsal'; provider='local'
    elif body.get('mode')=='agent':
        response=body['result']; model=str(body.get('model','External agent'))[:160]; provider='agent-import'
    else:
        if not profile: raise ValueError('Add and select a provider in Settings.')
        response,usage=generate(profile,context); model=profile['model']; provider=profile['id']
    notes=validate(response['notes'],src['length'])
    row={'id':uuid.uuid4().hex,'created':datetime.now(timezone.utc).isoformat(),'source':json.loads(json.dumps(src)),
      'prompt':prompt,'reply':str(response['reply'])[:4000],'provider':provider,'model':model,'notes':notes,
      'metrics':compare(src['notes'],notes),'usage':usage,'status':'candidate','feedback':'','live':None}
    STATE['attempts'].append(row); save()
    (DATA/(row['id']+'.mid')).write_bytes(midi_bytes(notes,src['length'],src['tempo']))
    return row

def find_attempt(id):
    row=next((a for a in STATE['attempts'] if a['id']==id),None)
    if not row: raise ValueError('Unknown variation.')
    return row

def find_by_fingerprint(src):
    """Scan the open set for arrangement clips whose notes match src's hash.

    The stored hash is over note content only (music.fingerprint), so a clip
    that was moved or renamed still matches, and a duplicated source track
    yields more than one match - which is why the caller must treat an
    ambiguous result as a question, not a rebind."""
    matches=[]
    for c in list_clips()['clips']:
        try:
            raw=host().clip(c['id'])
            if raw['start'] is not None and fingerprint(clip_notes(raw,src['length']))==src['hash']:
                matches.append({**c,'arrangementStart':position_text(raw['start'])})
        except ValueError:
            continue  # unreadable or wrong-shaped clip is simply not a candidate
    return matches


def rebind_source(src,match):
    """Point src at a clip proven identical by fingerprint.

    arrangementStart is updated too: it is not only an identity field, it is
    the bar position imports are written to (see import_attempt)."""
    import checkpoints
    before='%s / %s'%(src.get('trackName'),src.get('name'))
    src.update(id=match['id'],name=match['name'],trackId=match['trackId'],
               trackName=match['trackName'],arrangementStart=match['arrangementStart'])
    if STATE.get('source') and STATE['source'].get('hash')==src.get('hash'):
        STATE['source'].update(id=match['id'],name=match['name'],trackId=match['trackId'],
                               trackName=match['trackName'],arrangementStart=match['arrangementStart'])
    save()
    checkpoints.event('source-rebound',before=before,
                      after='%s / %s'%(match['trackName'],match['name']))
    return 'Source moved; rebound to %s / %s. Proceeding.'%(match['trackName'],match['name'])


def same_position(beats,stored):
    expected=position_beats(stored)
    return beats is not None and expected is not None and abs(beats-expected)<.0001

def check_source(src,rebind=True,allow_session_override=False,track_hint=None):
    """Verify src still points at the real original.

    rebind=False is mandatory for the post-import verification call: there the
    point is to prove the original did not change while we were writing, and
    silently re-pointing at a different clip would hide exactly the corruption
    that check is for."""
    if not src:
        # Nothing is selected to work from. Everything downstream reads fields off the original,
        # so this is said plainly here rather than surfacing as an attribute error on None.
        raise ValueError('No original is selected, so there is nothing to work from. Choose the clip '
                         'you want to build on in Scrunch first, then ask again.')
    if src.get('sessionId','lab')!=STATE.get('sessionId','lab'):
        import checkpoints
        checkpoints.event('session-gate-overridden',take=src.get('sessionId','lab'),current=STATE.get('sessionId','lab'))
    ls=lab_session(); tr=next((t for t in ls['tracks'] if t['id']==src['trackId'] and t['name']==src['trackName']),None)
    clip_ok=False
    if tr:
        track=host().track(tr['id'],clips=True,devices=True)
        clip_ok=any(c['id']==src['id'] and c['name']==src['name'] for c in track.get('clips',[]))
    if tr and clip_ok:
        raw=host().clip(src['id'])
        if fingerprint(clip_notes(raw,src['length']))==src['hash'] and same_position(raw['start'],src['arrangementStart']):
            return track
        if not rebind:
            raise ValueError('Original has changed since generation. Reload it before importing.')
    elif not rebind:
        raise ValueError('Source identity changed. Reload the source before importing.')
    # Identity drifted. The notes themselves are the durable identity, so look
    # for them rather than refusing outright.
    matches=find_by_fingerprint(src)
    if track_hint is None:
        track_hint=STATE.pop('sourceTrackHint',None)  # set by "use track N" in engineer.chat
    if len(matches)>1 and track_hint is not None:
        picked=[m for m in matches if m.get('trackIndex') is not None and m['trackIndex']+1==track_hint]
        if len(picked)==1:
            matches=picked  # falls into the single-match path below
        else:
            where='; '.join('track %d: %s / %s'%(m['trackIndex']+1,m['trackName'],m['name']) for m in matches[:5] if m.get('trackIndex') is not None)
            raise ValueError('Track %d isn\'t one of the matches (%s). Say the track number exactly as shown.'%(track_hint,where))
    if len(matches)==1:
        import checkpoints
        note=rebind_source(src,matches[0])
        fresh=lab_session(); tr=next((t for t in fresh['tracks'] if t['id']==src['trackId']),None)
        if not tr:
            raise ValueError('Rebound clip vanished mid-check. Reload the source.')
        checkpoints.event('source-rebind-note',message=note)
        return host().track(tr['id'],clips=True,devices=True)
    if len(matches)>1:
        where='; '.join('track %d: %s / %s'%(m['trackIndex']+1,m['trackName'],m['name']) for m in matches[:5] if m.get('trackIndex') is not None)
        raise ValueError('The original phrase is on %d tracks (%s). Say "use track" and the number - '
            'e.g. "use track %d" - or reload the source you want.'%(len(matches),where,matches[0].get('trackIndex',0)+1))
    raise ValueError('The original phrase is not in this set - it was on "%s / %s", and no clip here '
        'has matching notes. Reload a source clip, or say "use track" and its number to point at one directly.'
        %(src.get('trackName'),src.get('name')))

def import_attempt(id, recover=False):
    a=find_attempt(id)
    daw_adapter.require(host(),needs.VERIFY_IMPORTED if (a['live'] or {}).get('verified') else needs.IMPORT,'place this take')
    if a['live']:
        if a['live'].get('verified'):
            check_source(a['source'])
            ls=lab_session()
            dt=next((t for t in ls['tracks'] if t['id']==a['live']['trackId'] and t['name']==a['live']['name']),None)
            if not dt: raise ValueError('Previously imported track is missing or changed. Inspect the lab before retrying.')
            track=host().track(dt['id'],clips=True)
            if not any(c['id']==a['live']['clipId'] for c in track.get('clips',[])):
                raise ValueError('Previously imported clip is missing. Inspect the lab before retrying.')
            raw=host().clip(a['live']['clipId'])
            if not notes_equal(clip_notes(raw,a['source']['length']),a['notes']):
                daw_adapter.require(host(),{'replace_clip_notes'},'reimport this take')
                host().replace_clip_notes(a['live']['clipId'],a['notes'])
                if not notes_equal(clip_notes(host().clip(a['live']['clipId']),a['source']['length']),a['notes']):
                    raise ValueError('Reimported notes did not read back correctly.')
            return a
        if not recover or a['live'].get('stage')!='track-created':
            raise ValueError('A previous import needs inspection. No automatic retry or duplicate track.')
    tr=check_source(a['source'])
    label='Scrunch '+a['id'][:6]+' - '+a['model'][:30]
    # Write intent first: a timeout after duplication must not create more tracks on retry.
    if not a['live']:
        a['live']={'stage':'creating','name':label}; save()
        dest=host().duplicate_track(tr['id'],label,clips=False,devices=True)
        a['live'].update(trackId=dest['id'],stage='track-created'); save()
    else:
        dest={'id':a['live']['trackId']}
    fresh=lab_session(); dt=next(t for t in fresh['tracks'] if t['id']==dest['id'])
    if dt['name']!=label: raise ValueError('Destination identity changed. Recovery stopped.')
    start=position_beats(a['source']['arrangementStart'])
    if start is None: raise ValueError('The saved original position is unreadable. Reload the source before importing.')
    host().update_track(dest['id'],gainDb=-12,arm=False,solo=False,mute=True,monitoring='off')
    check=host().track(dt['id'],clips=True)
    if check.get('clips'): raise ValueError('Destination is occupied. Import stopped.')
    clip=host().create_arrangement_clip(dt['trackIndex'],a['source']['length'],a['notes'],start=start,name=label)
    a['live'].update(clipId=clip['id'],stage='clip-created'); save()
    read=host().clip(clip['id'])
    got=clip_notes(read,a['source']['length'])
    equal=len(got)==len(a['notes']) and all(n['p']==m['p'] and n['v']==m['v'] and abs(n['t']-m['t'])<.0001 and abs(n['d']-m['d'])<.0001 for n,m in zip(got,a['notes']))
    if not equal: raise ValueError('Readback mismatch. Track is retained muted for inspection.')
    check_source(a['source'],rebind=False)
    a['live'].update(verified=True,stage='verified',trackIndex=dt['trackIndex']); save(); return a

def notes_equal(left,right):
    return len(left)==len(right) and all(
        x['p']==y['p'] and x['v']==y['v'] and abs(x['t']-y['t'])<.0001 and abs(x['d']-y['d'])<.0001
        for x,y in zip(left,right))

def recover_replacement(id):
    daw_adapter.require(host(),needs.RECOVER_REPLACEMENT,'recover this replacement')
    a=find_attempt(id); saved=a.get('live') or {}
    if saved.get('stage')!='replacing': raise ValueError('No interrupted replacement to recover.')
    check_source(a['source'])
    old=find_attempt(saved['replaces'])
    if old['status']!='rejected': raise ValueError('Replaced take is no longer rejected.')
    ls=lab_session();tr=next(t for t in ls['tracks'] if t['id']==saved['trackId'])
    if tr['name'] not in (old['live']['name'],saved['name']): raise ValueError('Track identity changed.')
    track=host().track(tr['id'],clips=True)
    if len(track.get('clips',[]))!=1 or track['clips'][0]['id']!=saved['clipId']:
        raise ValueError('Track contents changed.')
    raw=host().clip(saved['clipId'])
    if raw['name']!=saved['name'] or not notes_equal(clip_notes(raw,a['source']['length']),a['notes']):
        raise ValueError('Replacement is incomplete or edited. Preserving it for inspection.')
    host().update_track(tr['id'],name=saved['name'])
    old['archivedLive']=old['live'];old['live']=None;old['replacedBy']=id
    saved.update(verified=True,stage='verified',trackIndex=tr['trackIndex'])
    a.pop('workflowError',None);save();return a


def place_latest(id):
    """Reuse only the immediately preceding, explicitly rejected, verified take."""
    a=find_attempt(id)
    if a.get('live'): return import_attempt(id)
    previous=[x for x in STATE['attempts'][:STATE['attempts'].index(a)]
              if x['source']['hash']==a['source']['hash'] and x['source'].get('sessionId','lab')==a['source'].get('sessionId','lab')]
    old=previous[-1] if previous else None
    if not old or old['status']!='rejected' or not (old.get('live') or {}).get('verified'):
        return import_attempt(id)
    daw_adapter.require(host(),needs.placement(True),'replace the rejected take')
    source=check_source(a['source'])
    ls=lab_session()
    target=next((t for t in ls['tracks'] if t['id']==old['live']['trackId']),None)
    if target is None:return import_attempt(id)
    track=host().track(target['id'],clips=True,devices=True)
    if len(track.get('clips',[]))!=1 or track['clips'][0]['id']!=old['live']['clipId']:
        return import_attempt(id)
    stop()
    label='Scrunch '+a['id'][:6]+' - '+a['model'][:30]
    a['live']={**old['live'],'verified':False,'stage':'replacing','name':label,'replaces':old['id']}
    save() # Persist intent before any write; never blindly retry a partial replacement.
    host().update_track(target['id'],mute=True,arm=False,gainDb=-12)
    host().clip(old['live']['clipId'])
    host().replace_clip_notes(old['live']['clipId'],a['notes'],name=label)
    raw=host().clip(old['live']['clipId'])
    if not notes_equal(clip_notes(raw,a['source']['length']),a['notes']):
        raise ValueError('Replacement readback failed. Track retained muted for inspection.')
    host().track(target['id'],clips=True)
    host().update_track(target['id'],name=label)
    old['archivedLive']=old['live']; old['live']=None; old['replacedBy']=a['id']
    a['live'].update(verified=True,stage='verified')
    save()
    return a

def next_take_replaces(src=None):
    """Would a new take for this source replace the previous rejected, verified one?"""
    src=src or STATE.get('source')
    if not src:return False
    previous=[x for x in STATE['attempts'] if x['source'].get('hash')==src.get('hash')
              and x['source'].get('sessionId','lab')==src.get('sessionId','lab')]
    return bool(previous) and previous[-1]['status']=='rejected' and bool((previous[-1].get('live') or {}).get('verified'))

def generate_and_audition(body):
    # Refuse unsupported workflows, then unverifiable originals, before spending API credit.
    daw_adapter.require(host(),needs.placement(next_take_replaces())|needs.AUDITION,'generate, place and audition a take')
    check_source(STATE['source'])
    stop()
    a=attempt(body)
    try:
        place_latest(a['id'])
        audition(a['id'])
    except Exception as e:
        a['workflowError']=str(e);save()
        raise ValueError('Take saved, but automatic placement/playback stopped: '+str(e)) from None
    a.pop('workflowError',None);save()
    return a


def stop():
    import preset_navigation
    preset_navigation.NAVIGATOR.stop()
    with PLAY_LOCK: return _stop()

def _stop():
    global AUDITION
    daw_adapter.require(host(),needs.STOP,'stop playback')
    old=AUDITION; AUDITION=None
    host().stop_playback()
    if old:
        old['timer'].cancel()
        for tr in old['tracks']:
            host().update_track(tr['id'],solo=tr['solo'],mute=tr['mute'])
    return {'playing':False}

def audition(id=None,loop=False):
    with PLAY_LOCK: return _audition(id,loop)

def _audition(id=None,loop=False):
    global AUDITION
    daw_adapter.require(host(),needs.AUDITION,'audition')
    if AUDITION: stop()
    a=find_attempt(id) if id else None; src=a['source'] if a else STATE['source']
    if not src: raise ValueError('Choose a source first.')
    check_source(src)
    if a and not (a['live'] or {}).get('verified'): raise ValueError('Place this variation in Ableton first.')
    target=a['live']['trackId'] if a else src['trackId']
    start=position_beats(src['arrangementStart'])
    if start is None:raise ValueError('The saved source position is unreadable. Reload the source.')
    ls=lab_session(); tracks=[{'id':t['id'],'solo':t['soloed'],'mute':t['muted']} for t in ls['tracks']]
    timer=threading.Timer(min(60,src['length']*60/ls['tempo']+.4),safe_stop); timer.daemon=True
    AUDITION={'tracks':tracks,'timer':timer}
    try:
        host().stop_session_clips()
        for t in tracks: host().update_track(t['id'],solo=t['id']==target)
        host().update_track(target,mute=False,arm=False)
        region=None
        if loop:
            region=(start,start+src['length'])
        after=host().play_arrangement(start,loop=region)
        if loop and (not after.get('playing') or not after.get('loop')):raise ValueError('Ableton did not confirm looping playback.')
        if not loop:timer.start()
    except Exception:
        safe_stop(); raise
    return {'playing':True,'seconds':0 if loop else src['length']*60/ls['tempo']}

def safe_stop():
    try: stop()
    except Exception: pass
