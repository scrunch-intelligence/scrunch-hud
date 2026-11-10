"""Local watchdogs, independent of request/UI/audio threads. No audio or secrets sent."""
import json, os, socket, ssl, threading, time, urllib.request
INTERVAL=3.0
MAX_AGE=7.0

class Monitor:
 def __init__(self,probes,clock=time.monotonic):
  self.probes=probes;self.clock=clock;self.lock=threading.Lock();self.records={};self.started=False
 def check(self,name):
  try: data=self.probes[name]();record={'ok':True,'data':data}
  except Exception as e:record={'ok':False,'error':type(e).__name__}
  with self.lock:self.records[name]={**record,'checked':self.clock()}
 def start(self):
  if self.started:return
  self.started=True
  def loop(name):
   while True:
    self.check(name);time.sleep(INTERVAL)
  for name in self.probes:threading.Thread(target=loop,args=(name,),daemon=True,name='health-'+name).start()
 def snapshot(self):
  now=self.clock()
  with self.lock: rows={k:dict(v) for k,v in self.records.items()}
  checks={}
  for name in self.probes:
   r=rows.get(name);fresh=r is not None and 0<=now-r['checked']<=MAX_AGE
   checks[name]={'ok':bool(fresh and r['ok']),'fresh':fresh,'ageSeconds':now-r['checked'] if r else None,'data':r.get('data') if fresh and r['ok'] else None}
  faults=[]
  for name,label in [('ableton','Ableton disconnected'),('tap','Audio listening stopped'),('internet','Internet lost')]:
   r=checks.get(name,{'ok':False,'fresh':False})
   if name=='tap' and r.get('ok') and (r.get('data') or {}).get('captureFaults'):
    faults.append({'name':name,'label':'Live Ears unavailable — memory could not be pinned','severity':'amber'});continue
   if not r['ok']:faults.append({'name':name,'label':label if r['fresh'] else label+' — check missed','severity':'amber' if name=='internet' else 'red'})
  return {'status':'red' if any(f['severity']=='red' for f in faults) else 'amber' if faults else 'green','label':'; '.join(f['label'] for f in faults) if faults else 'Ableton Live / Connected','checks':checks,'faults':faults,'maxAgeSeconds':MAX_AGE,'checkIntervalSeconds':INTERVAL}

def ableton():
 import daw_adapter
 return {**daw_adapter.DEFAULT.heartbeat(),'backend':daw_adapter.DEFAULT.name}

def internet():
 # A TLS reachability check only: no account, API call, credentials or audio.
 with socket.create_connection(('1.1.1.1',443),timeout=2) as sock:
  with ssl.create_default_context().wrap_socket(sock,server_hostname='one.one.one.one'):pass
 return {'scope':'Internet TLS reachability; not provider/model availability'}

def tap():
 import live_ears_dsp
 from live_ears import capture_faults
 faults=capture_faults()
 if faults:return {'state':'capture-unavailable','captureFaults':faults}
 s=live_ears_dsp.SERVICE.status(compact=True)
 if not s.get('streamId') or s['state'] not in ('live','measuring'):raise ValueError('Audio tap not delivering fresh buffers')
 return {'state':s['state']}

_previous_cpu=None
_machine=None

def cpu_percent(previous,current):
 if previous is None:return None
 deltas=[(b-a)&0xffffffff for a,b in zip(previous,current)]
 return 100*(sum(deltas)-deltas[2])/sum(deltas) if sum(deltas) else None

def resources():
 import ctypes as C,re,subprocess
 global _previous_cpu,_machine
 if _machine is None:
  lib=C.CDLL('/usr/lib/libSystem.B.dylib');lib.mach_host_self.restype=C.c_uint
  lib.host_statistics.argtypes=[C.c_uint,C.c_int,C.c_void_p,C.POINTER(C.c_uint)]
  _machine=(lib,lib.mach_host_self())
 lib,host=_machine;values=(C.c_uint*4)();count=C.c_uint(4)
 if lib.host_statistics(host,3,values,C.byref(count)) or count.value!=4:raise RuntimeError('CPU counters unavailable')
 current=list(values);cpu=cpu_percent(_previous_cpu,current);_previous_cpu=current
 raw=subprocess.check_output(['/usr/bin/vm_stat'],text=True,timeout=2)
 size=int(re.search(r'page size of (\d+) bytes',raw)[1]);pages={k.strip():int(v) for k,v in re.findall(r'^([^:]+):\s+(\d+)\.',raw,re.M)}
 total=int(subprocess.check_output(['/usr/sbin/sysctl','-n','hw.memsize'],text=True,timeout=2))
 return {'cpuPercent':cpu,'loadAverage1Minute':os.getloadavg()[0],'logicalCpus':os.cpu_count(),'physicalRamBytes':total,'freeRamBytes':pages.get('Pages free',0)*size,'inactiveRamBytes':pages.get('Pages inactive',0)*size,'compressedRamBytes':pages.get('Pages occupied by compressor',0)*size,'scope':'macOS system totals; inactive RAM may be reclaimable; free RAM alone is not memory pressure; no per-track attribution'}
MONITOR=Monitor({'ableton':ableton,'internet':internet,'tap':tap,'resources':resources})
