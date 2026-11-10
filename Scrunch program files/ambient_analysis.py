"""Read-only Live audio inventory and project-scoped background analysis."""
import json,threading,time,uuid
from pathlib import Path
from urllib.parse import urlparse,unquote
import engine,ingest,jup_bridge
LOCK=threading.RLock();STOP=threading.Event();STATE={'available':False,'tracks':[],'error':'Waiting for Live.'};SEEN={}

def project_path(name):
    value=jup_bridge.script('''tell application "System Events" to tell process "Live"
repeat with w in windows
try
set d to value of attribute "AXDocument" of w
if d starts with "file:" then return d
end try
end repeat
error "Cannot read the saved Live Set location. Bring Live forward and close any open dialogs."
end tell''')
    p=Path(unquote(urlparse(value).path)).resolve()
    if p.suffix!='.als' or p.stem!=name or not p.is_file():raise ValueError('Cannot verify the saved project path for the open Live Set.')
    for folder in p.parents:
        if (folder/'Ableton Project Info').is_dir():return p,folder
    return p,p.parent

def storage(folder):
    cfg=folder/'.scrunch-analysis.json';name=json.loads(cfg.read_text()).get('sidecarDirectory','.scrunch/analysis') if cfg.exists() else '.scrunch/analysis'
    p=Path(name).expanduser();return p.resolve() if p.is_absolute() else (folder/p).resolve()

def scan():
    global STATE
    try:
        engine.host().connect();live=engine.host().read_live_set('tracks')
        saved,folder=project_path(live['name']);root=storage(folder);tracks=[]
        for tr in live.get('tracks',[]):
            if tr.get('type')!='audio':continue
            detail=engine.host().read_track_detail(tr['id'],'clips','session-clips','sample','timing')
            if detail.get('id')!=tr['id']:raise ValueError('Live track changed during scan.')
            clips=[]
            for c in detail.get('arrangementClips',[])+detail.get('sessionClips',[]):
                path=c.get('sampleFile');entry={'clipId':c.get('id'),'name':c.get('name','Audio clip'),'length':c.get('arrangementLength',c.get('length')),'sourcePath':path,'status':'waiting'}
                try:
                    if not path:raise ValueError('Live did not report an audio file for this clip.')
                    p=Path(path)
                    if not p.is_file():raise ValueError('Audio file is missing or its drive is disconnected: '+str(p))
                    stat=p.stat();key=(str(saved),str(root),str(p));signature=(stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
                    cached=SEEN.get(key)
                    if not cached or cached['signature']!=signature:
                        cached={'signature':signature,'job':ingest.submit(str(p),root)};SEEN[key]=cached
                    job=next((j for j in ingest.jobs() if j['id']==cached['job']['id']),cached['job'])
                    entry.update(status=job['status'],jobId=job['id'])
                    if job.get('error'):entry['error']=job['error']
                    if job.get('fileHash'):
                        entry['fileHash']=job['fileHash'];entry['analysis']=ingest.read(job['fileHash'],root)
                except Exception as exc:entry.update(status='failed',error=str(exc)[:500])
                clips.append(entry)
            tracks.append({'id':tr['id'],'name':tr['name'],'clips':clips})
        # A changed window must not label old results as belonging to the new set.
        if project_path(live['name'])[0]!=saved:raise ValueError('Live Set changed during inventory; results discarded.')
        result={'available':True,'project':live['name'],'projectFile':str(saved),'projectFolder':str(folder),'sidecarDirectory':str(root),'tracks':tracks,'observedAt':time.time()}
    except Exception as exc:result={'available':False,'tracks':[],'error':str(exc)[:500]}
    with LOCK:STATE=result
    return result

def snapshot():
    with LOCK:return json.loads(json.dumps(STATE))

def configure(directory):
    s=snapshot()
    if not s.get('available'):raise ValueError('Connect a saved Live Set first.')
    saved,folder=project_path(s['project'])
    if str(saved)!=s['projectFile']:raise ValueError('Project changed; wait for the next scan.')
    value=str(directory).strip() or '.scrunch/analysis'
    target=Path(value).expanduser();target=target if target.is_absolute() else folder/target
    target.mkdir(parents=True,exist_ok=True)
    # Fail at configuration time if this directory is not writable. No alternate storage.
    probe=target/('.scrunch-write-check-'+uuid.uuid4().hex);probe.write_text('');probe.unlink()
    ingest.write(folder/'.scrunch-analysis.json',{'sidecarDirectory':value})
    SEEN.clear();return {'saved':True,'message':'Sidecar folder updated. Existing copies are retained.'}

def start():
    def work():
        while not STOP.is_set():
            try:scan()
            except Exception as exc:
                with LOCK:STATE.update(available=False,error=str(exc)[:500])
            STOP.wait(10)
    threading.Thread(target=work,daemon=True,name='live-audio-observer').start()

def english(s,track_ids=None):
    if not s.get('available'):return 'Audio analysis unavailable: '+s.get('error','Live has not been read.')
    lines=['Project: '+s['project']]
    for tr in s['tracks']:
        if track_ids and tr['id'] not in track_ids:continue
        if not tr['clips']:lines.append(tr['name']+': no audio clips in this track.')
        for c in tr['clips']:
            lines.append(tr['name']+' — '+c['name']+' (source file measurements; not the processed track)')
            if c.get('error'):lines.append('Analysis failed: '+c['error']);continue
            a=c.get('analysis',{});f=a.get('fast')
            if not f:lines.append('Analysis '+c['status']+'.');continue
            lines.append('Duration: %.1f seconds; %s channels; %s Hz.'%(f['durationSeconds'],f['channels'],f['sampleRate']))
            tempo=f['tempo']['candidatesBPM'];lines.append('Tempo: '+(', '.join('%.1f BPM'%x for x in tempo)+' candidates; confidence not calibrated.' if tempo else 'not detected.'))
            keys=f.get('sections',[])
            for section in keys:
                names=[x['tonic']+' '+x['mode'] for x in section['keyCandidates']]
                lines.append('Key %.0f–%.0f seconds: %s; uncertain.'%(section['startSeconds'],section['endSeconds'],', '.join(names) or 'not detected'))
            for key,label,unit in [('integratedLUFS','Loudness','LUFS'),('samplePeakDbFS','Sample peak','dBFS'),('oversampledPeakDbFS','Approximate oversampled peak (not verified true peak)','dBFS')]:
                v=f.get(key);lines.append(label+': '+('%.1f %s'%(v,unit) if v is not None else 'unavailable / silence'))
            lines.append('Downbeat and material type: '+('silence' if f['silence'] else 'unverified')+'. Automatic edits blocked.')
            if a.get('deep',{}).get('rmsByChannel') is not None:
                lines.append('RMS by channel (linear amplitude): '+', '.join('%.6f'%v for v in a['deep']['rmsByChannel']))
            if a.get('deepError'):lines.append('Background measurement failed: '+a['deepError'])
    return '\n'.join(lines) or 'No audio clips found in this Live Set. MIDI instruments need rendered audio before these measurements apply.'
