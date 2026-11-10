"""Content-addressed sidecars and separate bounded fast/deep worker queues."""
import json,hashlib,threading,uuid,os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import engine,audio_analysis
ROOT=engine.DATA/'analysis';LOCK=threading.RLock()
FAST=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ingest-fast')
DEEP=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ingest-deep')
JOBS={};PENDING=threading.BoundedSemaphore(8)

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:json.dump(value,f,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    tmp.replace(path)

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def location(key,root=None):
    if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise ValueError('Invalid audio hash.')
    return (Path(root) if root is not None else ROOT)/(key+'-'+audio_analysis.VERSION+'.json')

def read(key,root=None):
    with LOCK:return json.loads(location(key,root).read_text())

def update(key,root=None,**fields):
    with LOCK:
        p=location(key,root);r=json.loads(p.read_text());r.update(fields);write(p,r);return r

def run_deep(key,path,root=None):
    try:
        update(key,root=root,deepStatus='running');result=audio_analysis.deep(path)
        if digest(path)!=key:raise ValueError('Audio changed during background analysis.')
        update(key,root=root,deepStatus='complete',deep=result)
    except Exception as e:update(key,root=root,deepStatus='failed',deepError=str(e)[:400])

def ingest(path,root=None):
    path=Path(path).expanduser().resolve()
    if path.suffix.lower() not in ('.wav','.aif','.aiff') or not path.is_file():raise ValueError('Choose an existing WAV or AIFF file.')
    if path.stat().st_size>250_000_000:raise ValueError('Audio exceeds 250 MB limit.')
    key=digest(path);dest=location(key,root)
    with LOCK:
        if dest.exists():
            r=json.loads(dest.read_text());r['knownPaths']=sorted(set(r['knownPaths']+[str(path)]));write(dest,r)
            if r.get('fastStatus')=='complete':
                if r.get('deepStatus') in ('interrupted','not-started'):
                    r['deepStatus']='queued';write(dest,r);DEEP.submit(run_deep,key,str(path),root)
                return r
        write(dest,{'schemaVersion':1,'fileHash':key,'analysisVersion':audio_analysis.VERSION,'knownPaths':[str(path)],'fastStatus':'running','deepStatus':'not-started'})
    try:
        result=audio_analysis.fast(path)
        if digest(path)!=key:raise ValueError('Audio changed during analysis.')
        r=update(key,root=root,fastStatus='complete',fast=result,deepStatus='queued');DEEP.submit(run_deep,key,str(path),root);return r
    except Exception as e:
        update(key,root=root,fastStatus='failed',fastError=str(e)[:400]);raise

def submit(path,root=None):
    if not PENDING.acquire(blocking=False):raise ValueError('Audio queue is full; wait for a job to finish.')
    ident=uuid.uuid4().hex
    with LOCK:
        JOBS[ident]={'id':ident,'status':'queued','sourcePath':str(path),'sidecarRoot':str(root or ROOT)};write(ROOT/'jobs.json',JOBS)
    def work():
        try:
            with LOCK:
                JOBS[ident]['status']='running';write(ROOT/'jobs.json',JOBS)
            r=ingest(path,root)
            with LOCK:
                JOBS[ident].update(status='complete',fileHash=r['fileHash']);write(ROOT/'jobs.json',JOBS)
        except Exception as e:
            with LOCK:
                JOBS[ident].update(status='failed',error=str(e)[:400]);write(ROOT/'jobs.json',JOBS)
        finally:PENDING.release()
    FAST.submit(work);return {'id':ident,'status':'queued'}

def jobs():
    with LOCK:return list(JOBS.values())


def recover():
    """Mark interrupted jobs explicitly; never retry audio work silently."""
    with LOCK:
        if (ROOT/'jobs.json').exists():
            JOBS.update(json.loads((ROOT/'jobs.json').read_text()))
            for job in JOBS.values():
                if job['status'] in ('queued','running'):job.update(status='interrupted',error='Server stopped before completion; submit again explicitly.')
            write(ROOT/'jobs.json',JOBS)
        roots={ROOT}|{Path(j['sidecarRoot']) for j in JOBS.values() if j.get('sidecarRoot')}
        for root in roots:
            for p in root.glob('*-'+audio_analysis.VERSION+'.json'):
                try:
                    r=json.loads(p.read_text())
                    for k in ('fastStatus','deepStatus'):
                        if r.get(k) in ('running','queued'):r[k]='interrupted'
                    write(p,r)
                except (ValueError,OSError):continue
