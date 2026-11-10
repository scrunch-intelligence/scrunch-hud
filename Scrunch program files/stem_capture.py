"""Stems from the open Set: several Post Mixer taps captured at once, on one sample clock, placed on Arrangement beats.

Per source track: a muted, unarmed capture track at -12 dB, output Sends Only, input = the source at Post Mixer,
only the Live Ears tap on it (daw_mix.LiveMix.capture_track, Agent A's verified rig). All taps are read at
the same time while Live plays the range once. Every block carries Live's render sample time and the receiver's
mach_absolute_time stamp.

- Track-to-track alignment: Live gives each plug-in instance its own sample count, so each tap's sample time is
  mapped to mach time and the tap-to-tap difference snapped to whole buffers (all taps render in the same cycles).
  Proven by a same-source pair (two taps on one source must be identical at 0 samples).
- Beats: the play reply (or a playhead read after relocating while playing) gives the beat at a moment we stamp;
  blocks' mach stamps map sample time to that moment. Two passes of the same bars are lined up on an unchanged
  stem (`pass_offset`), which then matches bit for bit (live, 26 Sep 2026: 385-445 samples between passes).
Everything created is deleted afterwards (verified empty, muted, unarmed first); playback is stopped, the
producer's loop given back by the bridge's stop, and the playhead put back where it was.
"""
import ctypes,math,threading,time
import numpy as np

_LIB=ctypes.CDLL('/usr/lib/libSystem.dylib')
_LIB.mach_absolute_time.restype=ctypes.c_uint64
class _TB(ctypes.Structure):_fields_=[('numer',ctypes.c_uint32),('denom',ctypes.c_uint32)]
_tb=_TB();_LIB.mach_timebase_info(ctypes.byref(_tb))
def mach_seconds(ticks=None):
    t=_LIB.mach_absolute_time() if ticks is None else ticks
    return t*_tb.numer/_tb.denom/1e9

PREFIX='SCRUNCH STEM CAPTURE '

def _check_tempo(clock,expected):
    value=clock.get('tempo') if isinstance(clock,dict) else None
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        raise ValueError('Live tempo is unavailable; fixed-tempo capture was discarded.')
    if abs(value-expected)>1e-6:
        raise ValueError('Live tempo changed during the capture workflow; the fixed-tempo measurement was discarded.')
    return float(value)

def _wait_capture(adapter,seconds,cancelled=None,*,expected_tempo=None):
    """An interrupted pass is not a measurement of the requested whole passage."""
    deadline=time.monotonic()+seconds
    checks={'samples':0,'expectedTempo':expected_tempo,'basis':'Host tempo polled during playback; changes between polls and a complete tempo map are not verified.'}
    while True:
        if cancelled and cancelled():raise ValueError('Measurement cancelled; the incomplete pass was discarded.')
        remaining=deadline-time.monotonic()
        if remaining<=0:return checks
        time.sleep(min(.5,remaining))
        if expected_tempo is None:clock=adapter.transport()
        else:
            clock=adapter.preset_clock();_check_tempo(clock,expected_tempo);checks['samples']+=1
        if not clock.get('playing'):
            raise ValueError('Playback stopped before capture finished; this incomplete measurement was discarded.')

class _Reader(threading.Thread):
    def __init__(self,stream):
        super().__init__(daemon=True);from live_ears import LiveEarsSource
        self.src=LiveEarsSource(stream);self.blocks=[];self.stop=threading.Event();self.error=None
    def run(self):
        try:
            while not self.stop.is_set():
                b=self.src.read()
                if b is None:time.sleep(.001);continue
                self.blocks.append((b.sample_time,b.receiver_clock,b.frames,b.channels,int(b.sample_rate),b.dropped,b.timestamp_flags,b.pcm_float32_interleaved))
        except Exception as exc:self.error=str(exc)
        finally:self.src.close()

def _assemble(blocks):
    """Contiguous PCM from blocks, refusing gaps: (first sample time, pcm frames x 2, sample rate, checks)."""
    if not blocks:raise ValueError('The tap delivered no audio.')
    rate=blocks[0][4];flags={b[6] for b in blocks};dropped={b[5] for b in blocks}
    if any(not (f&1) for f in flags):raise ValueError('Live gave no valid sample time; tracks cannot be aligned.')
    gaps=sum(1 for a,b in zip(blocks,blocks[1:]) if b[0]!=a[0]+a[2])
    pcm=np.concatenate([np.frombuffer(b[7],dtype=np.float32).reshape(b[2],b[3]) for b in blocks]).astype(np.float64)
    if pcm.shape[1]==1:pcm=np.repeat(pcm,2,axis=1)
    return int(round(blocks[0][0])),pcm,rate,{'blocks':len(blocks),'gaps':gaps,'droppedChanged':len(dropped)>1,'nonIntegerSampleTimes':sum(1 for b in blocks if b[0]!=int(b[0]))}

class Rig:
    """Capture tracks that stay for a whole session of passes (create once, capture many times, close once).
    Creating or deleting tracks between an automation recording and its revert would put an undo step on top of
    Scrunch's recording, so the rig is built before and removed after."""
    def __init__(self,adapter,sources,*,host_mix=None,log=None,source_types=None):
        import daw_mix
        self.a=adapter;self.log=log or (lambda *x:None);self.mix=host_mix or daw_mix.LiveMix(adapter)
        self.hello=adapter.hello()
        if adapter.transport().get('playing'):raise ValueError('Live is playing; nothing was created.')
        # The producer's view and transport exactly as found (loop brace even while off, playhead, start marker,
        # selection, focused view): building the rig selects its tracks (Live loads a device into the selected
        # track) and every pass moves the playhead; close() puts back whatever differs and verifies it.
        self.view=self.mix.view_state() if hasattr(self.mix,'view_state') else None
        sess=adapter.session();byid={t['id']:t for t in sess['tracks']};self.tempo=float(adapter.connect()['tempo'])
        types=dict(source_types or {})       # name -> the input routing type to use as is (e.g. 'Main')
        for n,tid in sources.items():
            if tid not in byid and n not in types:raise ValueError('Source %r (%s) is not in the open Set.'%(n,tid))
        self.before_ids=[t['id'] for t in sess['tracks']];self.caps={};self.sources=dict(sources)
        # A solo would leave the Main (and every track it silences) empty: lifted for the rig's passes, put back on close.
        self.unsoloed=[t['id'] for t in sess['tracks'] if t.get('soloed')]
        try:
            for tid in self.unsoloed:adapter.update_track(tid,solo=False)
            for n,tid in sources.items():
                c=self.mix.capture_track(PREFIX+n,tid,source_type=types.get(n));self.caps[n]=c
                adapter.update_track(c['track'],gainDb=-12.0,mute=True,arm=False)
                if not c.get('stream'):raise ValueError('Capture tap for %r has no audio stream.'%n)
                self.log('capture track',n,{k:c[k] for k in ('track','stream','binding','input','channel')})
        except Exception:
            self.close();raise
    def capture(self,start_beat,end_beat,settle=.6,cancelled=None):
        if self.a.transport().get('playing'):raise ValueError('Live is playing; capture refused.')
        clock=self.a.preset_clock()
        _check_tempo(clock,self.tempo)
        readers={n:_Reader(c['stream']) for n,c in self.caps.items()}
        try:home=float(clock['beat'])      # the producer's playhead, put back after the pass
        except (KeyError,TypeError,ValueError):home=None
        rec={'bridge':self.hello.get('version'),'tempo':self.tempo,'startBeat':start_beat,'endBeat':end_beat,
             'captures':{n:{k:c[k] for k in ('track','device','stream','binding','input','channel')} for n,c in self.caps.items()}}
        try:
            for r in readers.values():r.start()
            time.sleep(settle)
            self.a.move_playhead(float(start_beat));time.sleep(.5)
            if cancelled and cancelled():raise ValueError('Measurement cancelled before playback.')
            t_send=mach_seconds();played=self.a.play_span(float(start_beat),float(end_beat));t_recv=mach_seconds()
            ran=(played['beat']-start_beat)*60/self.tempo
            rec['play']={'reply':played,'sentMach':t_send,'replyMach':t_recv,'roundTripMs':(t_recv-t_send)*1000,'secondsSinceStartAtReply':ran,'relocated':False}
            if not -.05<=ran<=(t_recv-t_send)+.25:
                # Live continued from where it last stopped (a stopped locate does not move that point): locate while
                # playing, then anchor on a fresh playhead read. Audio before the jump falls before start_beat and is cut.
                self.a.move_playhead(float(start_beat));time.sleep(.3)
                t_send=mach_seconds();clock=self.a.preset_clock();t_recv=mach_seconds();ran=(clock['beat']-start_beat)*60/self.tempo
                rec['play']={'reply':{'playing':clock['playing'],'beat':clock['beat']},'sentMach':t_send,'replyMach':t_recv,'roundTripMs':(t_recv-t_send)*1000,
                             'secondsSinceStartAtReply':ran,'relocated':True,'firstReply':played}
                if not (clock['playing'] and -.05<=ran<=(t_recv-t_send)+1.2):     # the jump happened (passes are then lined up on the audio)
                    raise ValueError('Playback did not start at beat %g (read %.3f after relocating); capture refused.'%(start_beat,clock['beat']))
                played=rec['play']['reply']
            rec['tempoChecks']=_wait_capture(self.a,max(0,(end_beat-played['beat'])*60/self.tempo)+.3,cancelled,expected_tempo=self.tempo)
        finally:
            try:rec['stop']=self.a.stop_playback()
            except Exception as exc:rec['stopError']=str(exc)
            if home is not None:
                try:self.a.move_playhead(home);rec['playheadRestored']=home
                except Exception as exc:rec['playheadError']=str(exc)
            for r in readers.values():r.stop.set()
            for r in readers.values():r.join(3)
        errors={n:r.error for n,r in readers.items() if r.error}
        if errors:raise ValueError('Tap read failed: %s'%errors)
        return _align(readers,rec,self.tempo,start_beat,end_beat)
    def close(self):
        self.cleanup=_cleanup(self.a,self.mix,self.caps,self.before_ids)
        if getattr(self,'unsoloed',None):
            try:
                for tid in self.unsoloed:self.a.update_track(tid,solo=True)
                now={t['id']:t.get('soloed') for t in self.a.session()['tracks']}
                self.cleanup['soloRestored']={'tracks':self.unsoloed,'verified':all(now.get(t) for t in self.unsoloed)}
            except Exception as exc:self.cleanup['soloRestored']={'tracks':self.unsoloed,'error':str(exc)[:300]}
        if getattr(self,'view',None) is not None:
            try:self.cleanup['view']=self.mix.restore_view(self.view)
            except Exception as exc:self.cleanup['view']={'error':str(exc)[:300]}
        return self.cleanup

def capture(adapter,sources,start_beat,end_beat,*,host_mix=None,log=None):
    """One pass: build the rig, capture [start_beat, end_beat), remove the rig. Returns (stems, record)."""
    rig=Rig(adapter,sources,host_mix=host_mix,log=log)
    try:stems,rec=rig.capture(start_beat,end_beat)
    finally:cleanup=rig.close()
    rec['cleanup']=cleanup;return stems,rec

def pass_offset(a,b,rate,max_ms=100):
    """Lag (samples) that best lines pass `b` up with pass `a` of the same stem (for comparing two passes)."""
    x=a.mean(1);y=b.mean(1);n=min(len(x),len(y));m=int(rate*max_ms/1000)
    X=np.fft.rfft(x[:n],2*n);Y=np.fft.rfft(y[:n],2*n);c=np.fft.irfft(X*np.conj(Y),2*n)
    lags=np.r_[np.arange(0,m+1),np.arange(-m,0)];vals=np.r_[c[:m+1],c[-m:]]
    return int(lags[int(np.argmax(vals))])

def residual_db(a,b,lag=0):
    """Level of a-b relative to a (dB), after shifting b by `lag` samples (positive: b is late)."""
    if lag>0:b=b[lag:];a=a[:len(b)]
    elif lag<0:a=a[-lag:];b=b[:len(a)]
    n=min(len(a),len(b));a,b=a[:n],b[:n]
    return float(10*np.log10(max(np.mean((a-b)**2),1e-30)/max(np.mean(a**2),1e-30)))

def _cleanup(adapter,mix,caps,before_ids):
    out=[]
    for n,c in caps.items():
        st=mix.state(c['track'])
        t=adapter.track(c['track'],clips=True)
        if t.get('clips') or not st.get('mute'):out.append({'track':n,'deleted':False,'why':'not empty or not muted'});continue
        adapter.delete_track(c['track']);out.append({'track':n,'deleted':True})
    after=[t['id'] for t in adapter.session()['tracks']]
    return {'tracks':out,'setTracksRestored':after==before_ids,'playing':adapter.transport().get('playing')}

def _align(readers,rec,tempo,start_beat,end_beat):
    """Each tap counts samples from its own start (Live gives every plug-in instance its own sample time; live,
    26 Sep 2026), so each stream gets its own sample-time -> mach offset (median over its blocks). All taps render
    in the same audio cycles, so the difference between two streams is a whole number of buffers: the mach
    estimate is snapped to it when it lies within a quarter buffer (recorded per stream)."""
    streams={};checks={};offs={}
    for n,r in readers.items():
        t0,pcm,rate,chk=_assemble(r.blocks);streams[n]=(t0,pcm,rate);checks[n]=chk
        offs[n]=float(np.median([mach_seconds(b[1])-b[0]/rate for b in r.blocks if b[1]]))
        chk['frames']=sorted({b[2] for b in r.blocks});chk['sampleTimeModFrames']=sorted({int(b[0])%b[2] for b in r.blocks})[:4]
    rates={v[2] for v in streams.values()}
    if len(rates)!=1:raise ValueError('Taps ran at different sample rates: %s'%rates)
    rate=rates.pop();ref=next(iter(streams));frames={f for c in checks.values() for f in c['frames']}
    B=frames.pop() if len(frames)==1 else None;shift={}
    for n in streams:
        est=(offs[ref]-offs[n])*rate            # stream sample time = reference sample time + est
        snapped=int(round(est/B))*B if B else int(round(est))
        checks[n]['clockShiftSamples']={'machEstimate':est,'used':snapped,'offBuffer':est-snapped,'snapped':bool(B) and abs(est-snapped)<=B/4}
        if B and abs(est-snapped)>B/4:snapped=int(round(est))
        shift[n]=snapped
    beat_ref=rec['play']['reply']['beat'];t_ref=rec['play']['replyMach']
    st_start=int(round((start_beat-beat_ref)*60/tempo*rate+(t_ref-offs[ref])*rate))
    st_end=st_start+int(round((end_beat-start_beat)*60/tempo*rate))
    lo=max(t0-shift[n] for n,(t0,_,_) in streams.items());hi=min(t0-shift[n]+len(p) for n,(t0,p,_) in streams.items())
    a=max(st_start,lo);b=min(st_end,hi)
    if b-a<rate:raise ValueError('Less than a second of common audio was captured.')
    stems={n:pcm[a+shift[n]-t0:b+shift[n]-t0] for n,(t0,pcm,_) in streams.items()}
    beat_at=lambda st:beat_ref+((st/rate+offs[ref])-t_ref)*tempo/60
    rec.update(sampleRate=rate,bufferFrames=B,checks=checks,firstBeat=beat_at(a),lastBeat=beat_at(b),startCutMissingBeats=max(0.0,beat_at(a)-start_beat),
               referenceStream=ref,commonSampleTime=[a,b],
               alignment='per-tap sample time mapped to mach time, differences snapped to whole buffers; beats from the play reply stamped with mach time')
    return stems,rec

def same_source_check(a,b,rate,max_ms=50):
    """Two taps on one source placed by sample time: offset (samples) of best match and its correlation."""
    x=a.mean(1);y=b.mean(1);n=min(len(x),len(y));x=x[:n]-x[:n].mean();y=y[:n]-y[:n].mean();m=int(rate*max_ms/1000)
    best=None
    for lag in range(-m,m+1):
        if lag>=0:c=np.dot(x[lag:],y[:n-lag])
        else:c=np.dot(x[:n+lag],y[-lag:])
        if best is None or c>best[1]:best=(lag,c)
    return {'offsetSamples':best[0],'correlation':float(best[1]/max(np.linalg.norm(x)*np.linalg.norm(y),1e-12)),
            'maxAbsDifference':float(np.max(np.abs(a[:n]-b[:n])))}
