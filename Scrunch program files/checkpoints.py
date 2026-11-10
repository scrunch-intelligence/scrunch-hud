"""Copy-only, verified local session/state checkpoints and structured events."""
import json,gzip,hashlib,os,shutil,threading,time,uuid
from datetime import datetime,timezone
from pathlib import Path
import xml.etree.ElementTree as ET
import engine,fresh_session

ROOT=engine.DATA/'checkpoints'
STOP=threading.Event()

def digest(data):return hashlib.sha256(data).hexdigest()
def consistent(raw,state):
    root=ET.fromstring(gzip.decompress(raw));src=state.get('source')
    if not src:return True
    if not {'id','trackId','trackName','name','notes','length','hash'}<=set(src):return False
    names={e.get('Value') for e in root.findall('LiveSet/Tracks/*/Name/EffectiveName')}
    return src['trackName'] in names
def event(kind,**fields):
    engine.DATA.mkdir(parents=True,exist_ok=True)
    record={'version':1,'time':datetime.now(timezone.utc).isoformat(),'kind':kind,**fields}
    with (engine.DATA/'events.jsonl').open('a') as f:
        f.write(json.dumps(record,default=str)+'\n');f.flush();os.fsync(f.fileno())

def capture(reason,save_live=True,adopt=True):
    """Call under engine.LOCK. Failure stops the requesting action, not playback Stop.

    Session-path drift (Scrunch's last-known file vs. what's actually open)
    is auto-adopted rather than blocked: fresh_session.current_path already
    restricts what "open" can mean to the dedicated lab or a Scrunch
    rehearsal, so there's nothing here to verify a user's judgment against -
    it's bookkeeping, not content safety. adopt=False exists only for tests
    that need to see the raw drift. The real safety net for what actually
    gets written is check_source's fingerprint check in engine.py, which
    this does not touch or weaken."""
    path=fresh_session.save_current() if save_live else fresh_session.current_path()
    expected=engine.STATE.get('sessionPath')
    if expected and Path(expected).resolve()!=path.resolve():
        if not adopt:
            raise ValueError('Session path drifted (adopt=False).')
        event('session-adopted',previous=expected,current=str(path))
        engine.STATE['sessionPath']=str(path);engine.save()
    if engine.STATE.get('source'):
        # Use the same fingerprint-based recovery check_source already does
        # (auto-rebinds on an unambiguous match) instead of a bare name
        # lookup, which has no way to recover a track that moved or was
        # renamed - it can only ever say no. This runs before the local
        # consistency check below so a source that's merely moved doesn't
        # reach that check at all.
        engine.check_source(engine.STATE['source'])
    raw=path.read_bytes();root=ET.fromstring(gzip.decompress(raw))
    if not consistent(raw,engine.STATE):raise ValueError('The saved Live set no longer contains the current source track. Reload the source, or say "skip checkpoint" to continue without one this time.')
    if shutil.disk_usage(engine.DATA).free < max(20_000_000,len(raw)*4):raise ValueError('Not enough disk space for a verified checkpoint.')
    ident=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:10]
    ROOT.mkdir(parents=True,exist_ok=True);temp=ROOT/('.pending-'+ident);temp.mkdir()
    state=json.dumps(engine.STATE,indent=2).encode()
    assets=[]
    for value in sorted({e.get('Value','') for e in root.findall('.//FileRef/Path')}):
        p=Path(value)
        if not value:continue
        assets.append({'path':value,'exists':p.is_file(),'size':p.stat().st_size if p.is_file() else None,'copyPolicy':'Referenced only; audio/assets are not copied per action.'})
    manifest={'version':1,'id':ident,'created':datetime.now(timezone.utc).isoformat(),'reason':reason,
              'sessionId':engine.STATE.get('sessionId','lab'),'sourcePath':str(path),'sessionSha256':digest(raw),
              'stateSha256':digest(state),'assets':assets,'verified':False}
    for name,data in [('session.als',raw),('state.json',state)]:
        with (temp/name).open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    if digest((temp/'session.als').read_bytes())!=manifest['sessionSha256'] or digest((temp/'state.json').read_bytes())!=manifest['stateSha256']:
        raise ValueError('Checkpoint copy verification failed.')
    manifest['verified']=True;(temp/'manifest.json').write_text(json.dumps(manifest,indent=2))
    temp.rename(ROOT/ident)
    event('checkpoint',id=ident,reason=reason,sessionId=manifest['sessionId'])
    return manifest

def inventory():
    records=[]
    if ROOT.exists():
        for folder in ROOT.iterdir():
            if folder.name.startswith('.'):continue
            try:
                m=json.loads((folder/'manifest.json').read_text())
                m['verified']=digest((folder/'session.als').read_bytes())==m['sessionSha256'] and digest((folder/'state.json').read_bytes())==m['stateSha256']
                saved=json.loads((folder/'state.json').read_text());src=saved.get('source')
                if src and not {'id','trackId','trackName','name','notes','length','hash'}<=set(src):m['verified']=False
                if m['verified'] and not consistent((folder/'session.als').read_bytes(),saved):m['verified']=False
                records.append(m)
            except (OSError,ValueError,KeyError,TypeError,ET.ParseError):continue
    return sorted(records,key=lambda x:x['created'],reverse=True)

def start_timer(interval=300):
    def work():
        while not STOP.wait(interval):
            if not engine.LOCK.acquire(blocking=False):continue
            try:
                if not engine.AUDITION:capture('timer')
            except Exception as exc:
                event('checkpoint-error',reason='timer',error=str(exc)[:500])
            finally:engine.LOCK.release()
    thread=threading.Thread(target=work,name='scrunch-checkpoints',daemon=True);thread.start();return thread

def rebind(state):
    """Resolve IDs after reopening; never trust IDs serialized in a Live snapshot."""
    clips=engine.list_clips()['clips'];session=state.get('sessionId','lab')
    def source(src):
        matches=[c for c in clips if c['name']==src['name'] and c['trackName']==src['trackName']]
        if len(matches)!=1:raise ValueError('Restored original cannot be uniquely resolved.')
        c=matches[0];raw=engine.host().clip(c['id'])
        if not engine.notes_equal(engine.clip_notes(raw,src['length']),src['notes']):raise ValueError('Restored original notes differ.')
        src.update({k:c[k] for k in ('id','trackId','trackName','trackIndex')})
    if state.get('source'):source(state['source'])
    for a in state.get('attempts',[]):
        if a['source'].get('sessionId','lab')!=session:continue
        source(a['source'])
        if a.get('live'):
            matches=[c for c in clips if c['name']==a['live']['name'] and c['trackName']==a['live']['name']]
            if len(matches)!=1:raise ValueError('Restored take cannot be uniquely resolved.')
            c=matches[0];raw=engine.host().clip(c['id'])
            if not engine.notes_equal(engine.clip_notes(raw,a['source']['length']),a['notes']):raise ValueError('Restored take notes differ.')
            a['live'].update(trackId=c['trackId'],clipId=c['id'],trackIndex=c['trackIndex'])

def restore(ident):
    matches=[m for m in inventory() if m['id']==ident and m['verified']]
    if len(matches)!=1:raise ValueError('Choose an intact verified checkpoint.')
    before=capture('before-revert');engine.stop();m=matches[0];folder=ROOT/ident
    state=json.loads((folder/'state.json').read_text())
    dest=fresh_session.REHEARSALS/('restore-'+uuid.uuid4().hex);dest.mkdir(parents=True)
    (dest/'Ableton Project Info').mkdir()
    livefile=dest/(Path(state.get('sessionPath') or fresh_session.LEGACY_REHEARSAL_NAME).stem+'.als');shutil.copyfile(folder/'session.als',livefile)
    (dest/'session.json').write_text(json.dumps({'path':str(livefile),'sessionId':state.get('sessionId'),'restoredFrom':ident},indent=2))
    event('restore-start',checkpoint=ident,recoveryCheckpoint=before['id'],destination=str(livefile))
    try:
        open_verified(livefile,state)
    except Exception as exc:
        event('restore-error',checkpoint=ident,recoveryCheckpoint=before['id'],error=str(exc)[:500])
        recovery=json.loads((ROOT/before['id']/'state.json').read_text())
        try:
            open_verified(Path(before['sourcePath']),recovery)
            engine.STATE.clear();engine.STATE.update(recovery);engine.save()
            event('restore-rolled-back',checkpoint=ident,recoveryCheckpoint=before['id'])
        except Exception as rollback:
            event('restore-rollback-error',recoveryCheckpoint=before['id'],error=str(rollback)[:500])
            raise ValueError('Restore and automatic recovery did not verify. Stop editing and reopen the before-revert session: '+before['sourcePath']) from exc
        raise ValueError('Restore did not verify; your before-revert session was reopened and verified.') from exc
    # Keep account configuration and the current conversation across Revert.
    for key in ('providers','engineerProvider','midiProvider','engineerChat'):
        if key in engine.STATE:state[key]=engine.STATE[key]
    restored={a['id']:a for a in state.get('attempts',[])}
    for attempt in engine.STATE.get('attempts',[]):
        previous=restored.get(attempt['id'])
        if not previous or previous.get('status')!=attempt.get('status'):
            event('later-outcome',take=attempt['id'],outcome='session-reverted',previousStatus=attempt.get('status'),restoredStatus=previous.get('status') if previous else 'not-in-restored-version',checkpoint=ident)
    state['sessionPath']=str(livefile)
    engine.STATE.clear();engine.STATE.update(state);engine.save()
    event('restore-complete',checkpoint=ident,recoveryCheckpoint=before['id'])
    return {'restored':ident,'recoveryCheckpoint':before['id'],'verified':True}

def open_verified(livefile,state):
    import subprocess
    subprocess.run(['/usr/bin/open','-a','Ableton Live 12 Suite',str(livefile)],check=True,timeout=10)
    for _ in range(40):
        try:
            if fresh_session.current_path()==livefile.resolve():
                engine.lab_session();rebind(state);return
        except ValueError:pass
        time.sleep(.5)
    raise ValueError('Opened session could not be verified.')
