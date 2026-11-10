"""Mix automation: arrangement-aware, track-versus-track analysis that proposes real automation curves.

Scrunch-owned and DAW-neutral. Input is per-track audio of one arrangement (stems) on the Set's own
time base (tempo, bar length, the beat where the stems start). Every finding and every curve point is
placed in bars/beats.

1. Measure every track and the summed mix in 0.25 s hops (0.5 s windows, reference_dsp bands, a
   1/3-octave grid for locating frequencies, libebur128 momentary loudness).
2. Detect, track against track: masking (1/3-octave cells where two tracks are within 6 dB of each other,
   both matter to their own sound and together make up most of the mix there), low-end clashes (30-150 Hz,
   bin by bin, frame by frame, measured - never guessed from names), balance drift inside a section,
   stereo/phase (low-end anti-phase or side, width collapse, mono loss) per track and summed, and
   sections from novelty in the mix's spectral shape and track activity (clip edges help when given).
   Importance is measured: power-mean loudness over the span plus how much of it the track is active.
3. Plan: for each finding, candidate moves on the less important track (EQ bell dip, high-pass, gain,
   pan, send, sidechain compressor threshold, Utility width / bass mono, or a hosted-plugin parameter
   whose measured response is supplied) are sized from the measured numbers, shaped to the finding's
   own time envelope, placed on beats (bar lines where the problem starts within a beat of one),
   applied offline to the stem with a matching filter/dynamics model, and measured again. The move that
   removes most of the problem for the least collateral loudness change is proposed; moves that do not
   measurably improve it are dropped with their numbers. No word rules: nothing reads track names.
4. Workflow (binding, John 26 Sep): proposal -> audition (offline render of processed stems, A/B WAVs of
   the affected bars) -> Keep / Revert / A / B / Neither chosen by the producer. Measured improvement is
   evidence, not permission. Auto-keep only when the persisted autonomous mode is on (off by default).
   Before any write every target's automation state is read; a target that already carries automation
   Scrunch did not record is refused ("exact revert not guaranteed"). Writes go through
   automation_actions when it exists, else arrangement_automation, and are read back and verified.
5. Report: Mix Doctor style HTML with sections, per-pair clash markers and the planned curves overlaid.
"""
import audio_analysis  # Shared local analysis dependency environment.
import argparse,copy,datetime,hashlib,html,json,math,os,sys,time,uuid
from pathlib import Path
import numpy as np,soundfile as sf
from scipy.signal import resample_poly,sosfilt,sosfiltfilt,butter
from scipy import ndimage
import reference_dsp as dsp

VERSION='mix-automation-1'
ROOT=Path(__file__).resolve().parent
SR=48000;HOP=.25;HS=int(SR*HOP);NFFT=8192;STEP=1500;FPH=HS//STEP      # 8 frames per hop
MAX_SECONDS=900;MAX_TRACKS=24
CENTERS=1000*2**(np.arange(-16,13)/3.0)                                  # 1/3-octave grid, 24.8 Hz .. 16 kHz
EDGES=np.stack([CENTERS*2**(-1/6),CENTERS*2**(1/6)],axis=1)
FREQ=np.fft.rfftfreq(NFFT,1/SR)
LF=(30.0,150.0);LFBINS=np.flatnonzero((FREQ>=LF[0])&(FREQ<LF[1]))
MASK_MIN_HZ=150.0;KMIN=int(np.searchsorted(CENTERS,MASK_MIN_HZ))
BAND_NAMES=[b[0] for b in dsp.BANDS]
# Numeric criteria. Each is a measurement threshold, documented in the report's method section.
CLOSE=6.0        # two tracks within this many dB in the same cell compete
SIG=18.0         # a cell matters to a track when within this many dB of that track's strongest cell at that time
CONTRIB=10.0     # ... and when within this many dB of the mix in that cell
PAIRSHARE=.5     # ... and the two tracks together carry at least half of the mix's power there
BUILDUP_DB=3.0   # a track whose clash-band level rose this much above its own level elsewhere (and more than its other bands) built the clash up
MARGIN=7.0       # a planned dip aims to put the less important track this far below the dominant one (1 dB past the clash line)
MIN_MOVE=1.0     # dB: smaller moves are not proposed
MAX_CUT=12.0     # dB: largest cut a single move may make
BAR_LABEL_EPS=1e-4  # quarter-note beats: tolerate sample/host rounding in labels only
ACTIVE_BELOW_MIX=35.0
DRIFT=3.0;DRIFT_OWN=2.0
LOW_CORR=0.0;LOW_SIDE=.2
IMPROVE=.25;IMPROVE_DB=1.0
COLLATERAL=.05   # benefit lost per dB of collateral change (the track outside the problem: other bands, frames without the clash)

def db(p):return 10*np.log10(np.maximum(np.asarray(p,float),1e-20))
def r(x,d=2):
    if x is None:return None
    x=float(x);return round(x,d) if math.isfinite(x) else None
def clock(s):return f'{int(max(0,s)//60)}:{max(0,s)%60:04.1f}'

# ---------------------------------------------------------------- time base
class TimeBase:
    """Seconds of stem audio <-> Arrangement beats. Constant tempo (Live's API gives no tempo map)."""
    def __init__(self,tempo,beats_per_bar=4,start_beat=0.0,clip_edges=(),source='given'):
        if not isinstance(tempo,(int,float)) or not 20<=tempo<=999:raise ValueError('Tempo must be 20-999 BPM.')
        if type(beats_per_bar) not in (int,float) or not math.isfinite(beats_per_bar) or beats_per_bar<=0:
            raise ValueError('Bar length must be a positive number of quarter-note beats.')
        self.tempo=float(tempo);self.bpb=float(beats_per_bar);self.start_beat=float(start_beat)
        self.clip_edges=sorted({float(b) for b in clip_edges});self.source=source
    def beat(self,t):return self.start_beat+t*self.tempo/60.0
    def seconds(self,beat):return (beat-self.start_beat)*60.0/self.tempo
    def bar(self,t):return self.beat(t)/self.bpb+1.0
    def label(self,t):
        b=self.beat(t);bar=int(math.floor((b+BAR_LABEL_EPS)/self.bpb))+1;beat=b-(bar-1)*self.bpb+1
        return f'{bar}' if abs(beat-1)<BAR_LABEL_EPS else f'{bar}.{beat:g}'
    def span(self,t0,t1):
        """Bars as a producer counts them: the bar t0 starts in to the bar t1 ends in."""
        a=int(math.floor((self.beat(t0)+BAR_LABEL_EPS)/self.bpb))+1;b=int((self.beat(t1)-BAR_LABEL_EPS)//self.bpb)+1
        return f'bar {a}' if a==b else f'bars {a}-{b}'
    def snap(self,t):
        """Nearest beat; the bar line instead when it is within one beat."""
        b=self.beat(t);bar=round(b/self.bpb)*self.bpb
        return self.seconds(bar if abs(b-bar)<=1.0 else round(b))
    def public(self):return {'tempo':self.tempo,'beatsPerBar':self.bpb,'startBeat':self.start_beat,'clipEdgesBeats':self.clip_edges,'source':self.source}

def timebase_from_live(adapter,start_beat=0.0):
    """Tempo, bar length and clip edges read from the open Set (read-only)."""
    c=adapter.connect();ts=str(c.get('timeSignature') or '4/4')
    try:num,den=(int(x) for x in ts.split('/'))
    except ValueError:num,den=4,4
    edges=[]
    for t in adapter.session().get('tracks',[]):
        try:clips=adapter.track(t['id'],clips=True).get('clips') or []
        except Exception:continue
        for cl in clips:
            if isinstance(cl.get('start'),(int,float)) and isinstance(cl.get('span'),(int,float)):edges+=[cl['start'],cl['start']+cl['span']]
    return TimeBase(c['tempo'],num*4.0/den,start_beat,edges,source='live: tempo %s, %s, %d clip edges'%(c['tempo'],ts,len(edges)))

# ---------------------------------------------------------------- stems
def load_stems(folder):
    """Every WAV/AIFF in `folder` as a stem (file stem = track name) plus optional arrangement.json:
    {"tempo":..,"beatsPerBar":4,"startBeat":0,"trackIds":{name:id},"returns":{name:{"feeds":{track:sendDb}}},
     "responses":{name:[measured parameter responses]}}"""
    folder=Path(folder);stems={}
    files=sorted(p for p in folder.iterdir() if p.suffix.lower() in ('.wav','.aif','.aiff'))
    if not 2<=len(files)<=MAX_TRACKS:raise ValueError('Give 2 to %d stems.'%MAX_TRACKS)
    for p in files:
        info=sf.info(str(p))
        if info.duration>MAX_SECONDS or info.channels not in (1,2):raise ValueError('%s: mono/stereo up to %d s.'%(p.name,MAX_SECONDS))
        y,sr=sf.read(str(p),always_2d=True,dtype='float64')
        if not np.isfinite(y).all():raise ValueError('%s has nonfinite samples.'%p.name)
        if sr!=SR:g=math.gcd(sr,SR);y=resample_poly(y,SR//g,sr//g,axis=0)
        stems[p.stem]=np.repeat(y,2,axis=1) if y.shape[1]==1 else y
    meta=json.loads((folder/'arrangement.json').read_text()) if (folder/'arrangement.json').exists() else {}
    return stems,meta

def align(stems):
    n=max(len(y) for y in stems.values())
    return {k:np.vstack([y,np.zeros((n-len(y),2))]) if len(y)<n else y for k,y in stems.items()}

# ---------------------------------------------------------------- features
_W=np.hanning(NFFT);_SC=2.0/(NFFT*np.sum(_W**2))
def _assign(edges):
    m=np.zeros((len(FREQ),len(edges)))
    for k,(lo,hi) in enumerate(edges):m[(FREQ>=lo)&(FREQ<hi),k]=1
    return m
_MF=_assign(EDGES);_MB=_assign([(lo,hi) for _,lo,hi in dsp.BANDS])

def features(y):
    """Per-frame and per-hop measurements of one stereo signal. Frame f is centred on sample f*STEP."""
    y=np.asarray(y,float);n=len(y);H=max(1,math.ceil(n/HS));F=H*FPH+1
    pad=np.zeros((F*STEP+NFFT,2));pad[NFFT//2:NFFT//2+n]=y
    fl=np.zeros((F,len(CENTERS)));fr=np.zeros_like(fl);bl=np.zeros((F,len(dsp.BANDS)));br=np.zeros_like(bl);bc=np.zeros_like(bl)
    lf=np.zeros((F,len(LFBINS)),np.complex64)
    views=[np.lib.stride_tricks.sliding_window_view(pad[:,c],NFFT)[::STEP][:F] for c in (0,1)]
    for a in range(0,F,256):
        XL=np.fft.rfft(views[0][a:a+256]*_W,axis=1);XR=np.fft.rfft(views[1][a:a+256]*_W,axis=1)
        PL=(XL.real**2+XL.imag**2)*_SC;PR=(XR.real**2+XR.imag**2)*_SC;C=(XL*XR.conj()).real*_SC
        fl[a:a+256]=PL@_MF;fr[a:a+256]=PR@_MF;bl[a:a+256]=PL@_MB;br[a:a+256]=PR@_MB;bc[a:a+256]=C@_MB
        lf[a:a+256]=((XL[:,LFBINS]+XR[:,LFBINS])/2*math.sqrt(_SC)).astype(np.complex64)
    def hop(x):
        cs=np.concatenate([np.zeros((1,)+x.shape[1:]),np.cumsum(x,axis=0)]);h=np.arange(H)
        lo=np.clip(h*FPH-FPH//2,0,F);hi=np.clip(h*FPH+FPH+FPH//2,0,F)
        return (cs[hi]-cs[lo])/np.maximum(hi-lo,1).reshape((-1,)+(1,)*(x.ndim-1))
    L,R,Cc=hop(bl),hop(br),hop(bc);FL,FR=hop(fl),hop(fr)
    tl,tr,tc=L.sum(1),R.sum(1),Cc.sum(1)
    with np.errstate(invalid='ignore',divide='ignore'):
        corr=np.where(tl*tr>1e-20,tc/np.sqrt(tl*tr),np.nan)
        side=np.maximum(0,(L+R-2*Cc)/4);mid=np.maximum(0,(L+R+2*Cc)/4)
        width=side[:,2:].sum(1)/np.maximum(side[:,2:].sum(1)+mid[:,2:].sum(1),1e-20)
        lc=Cc[:,:2].sum(1);ll=L[:,:2].sum(1);lr=R[:,:2].sum(1)
        low_corr=np.where(ll*lr>1e-20,lc/np.sqrt(ll*lr),np.nan)
        low_side=side[:,:2].sum(1)/np.maximum(side[:,:2].sum(1)+mid[:,:2].sum(1),1e-20)
        mono=db(mid.sum(1))-db((tl+tr)/2)
    blocks=(np.ascontiguousarray(y[i:i+HS]) for i in range(0,max(n,1),HS))
    hist=dsp.meter_blocks(blocks,SR,2)['loudnessHistory'] if n>=HS else []
    lufs=np.array([np.nan if x['momentaryLUFS'] is None else x['momentaryLUFS'] for x in hist]+[np.nan]*H)[:H]
    return {'hops':H,'fineL':FL,'fineR':FR,'fine':(FL+FR)/2,'bandL':L,'bandR':R,'bandC':Cc,'total':(tl+tr)/2,'lufs':lufs,
            'corr':corr,'width':width,'lowCorr':low_corr,'lowSide':low_side,'monoLossDb':mono,'lf':lf,'lfPower':hop((np.abs(lf)**2).sum(1)),
            'level':db((tl+tr)/2),'frameTotal':(fl.sum(1)+fr.sum(1))/2}

class Context:
    """All stems, the summed mix and their features, on one time base."""
    def __init__(self,stems,timebase,returns=None,track_ids=None,responses=None,rest=True,live_names=None):
        if not 2<=len(stems)<=MAX_TRACKS:raise ValueError('Give 2 to %d stems.'%MAX_TRACKS)
        self.stems=align({k:np.asarray(v,float) for k,v in stems.items()});self.tb=timebase
        self.names=list(self.stems);self.mix=sum(self.stems.values());self.n=len(self.mix)
        self.returns=dict(returns or {});self.track_ids=dict(track_ids or {});self.responses=dict(responses or {});self.live_names=dict(live_names or {})
        self.f={k:features(y) for k,y in self.stems.items()};self.fm=features(self.mix);self.H=self.fm['hops']
        self.rest=set(self.names) if rest else set()     # tracks whose balance against the rest is measured
        self._rest_power={}
        ml=self.fm['lufs']
        self.active={k:np.isfinite(f['lufs'])&(f['lufs']>=-70)&(f['lufs']>=np.nan_to_num(ml,nan=-200)-ACTIVE_BELOW_MIX) for k,f in self.f.items()}
    def t(self,h):return h*HOP
    def rest_power(self,name):
        """Meter the actual sum of other stems, including their cross terms.

        Power(mix) - power(stem) is not power(rest) for correlated audio.
        Cache only the small hop-power array, not another complete feature set.
        """
        if name not in self._rest_power:
            self._rest_power[name]=features(self.mix-self.stems[name])['total']
        return self._rest_power[name]
    def importance(self,name,hops):
        hops=np.asarray(hops,int);l=self.f[name]['lufs'][hops];a=self.active[name][hops]
        if not a.any():return -200.0
        return float(db(np.mean(np.where(a,10**(np.nan_to_num(l,nan=-200)/10),0)))+db(a.mean()))

def features_lufs(y):
    n=len(y);H=max(1,math.ceil(n/HS))
    hist=dsp.meter_blocks((np.ascontiguousarray(y[i:i+HS]) for i in range(0,n,HS)),SR,2)['loudnessHistory']
    return np.array([np.nan if x['momentaryLUFS'] is None else x['momentaryLUFS'] for x in hist]+[np.nan]*H)[:H]

# ---------------------------------------------------------------- detectors
def segments(flag,minimum=1,gap=1):
    import mix_doctor
    return mix_doctor.segments(np.asarray(flag,bool),minimum,gap)

def _bars(ctx,t0,t1):return {'startSeconds':r(t0),'endSeconds':r(t1),'startBeat':r(ctx.tb.beat(t0),3),'endBeat':r(ctx.tb.beat(t1),3),'bars':ctx.tb.span(t0,t1)}

def masking_cells(fa,fb,fm,act_a,act_b,channel='fine'):
    La,Lb,Lm=db(fa[channel]),db(fb[channel]),db(fm[channel])
    top_a=La.max(1,keepdims=True);top_b=Lb.max(1,keepdims=True)
    sig_a=(La>=top_a-SIG)&(La>=Lm-CONTRIB)&act_a[:,None];sig_b=(Lb>=top_b-SIG)&(Lb>=Lm-CONTRIB)&act_b[:,None]
    share=(fa[channel]+fb[channel])>=PAIRSHARE*fm[channel]
    c=sig_a&sig_b&(np.abs(La-Lb)<=CLOSE)&share;c[:,:KMIN]=False
    return c,La,Lb

def detect_masking(ctx,a,b):
    fa,fb,fm=ctx.f[a],ctx.f[b],ctx.fm
    c,La,Lb=masking_cells(fa,fb,fm,ctx.active[a],ctx.active[b])
    if not c.any():return []
    closed=ndimage.binary_closing(c,structure=np.ones((5,1)))|c
    lab,n=ndimage.label(closed,structure=[[0,1,0],[1,1,1],[0,1,0]]);out=[]
    for i in range(1,n+1):
        comp=(lab==i)&c
        per_band=comp.sum(0)
        if per_band.max()<4:continue
        core=np.flatnonzero(per_band>=.5*per_band.max())
        hops=np.flatnonzero(comp[:,core].any(1))
        h0,h1=int(hops.min()),int(hops.max())
        if h1-h0+1<4:continue
        span=np.arange(h0,h1+1)
        ia,ib=ctx.importance(a,span),ctx.importance(b,span)
        d,m=(a,b) if ia>=ib else (b,a);Ld,Lmm=(La,Lb) if d==a else (Lb,La)
        k0,k1=int(core.min()),int(core.max());ks=np.arange(k0,k1+1)
        cells=comp[np.ix_(span,ks)]
        ratio=float(np.mean((Lmm-Ld)[np.ix_(span,ks)][cells]))
        # where the clash sits: the band carrying most of the shared (smaller) power
        shared=np.minimum(ctx.f[a]['fine'],ctx.f[b]['fine'])[np.ix_(span,ks)].sum(0)
        kc=ks[int(np.argmax(shared))]
        own=ctx.f[m]['fine'][span];share_db=float(db(own[:,ks].sum())-db(own.sum()))
        found={'kind':'masking','tracks':[a,b],'dominant':d,'maskee':m,**_bars(ctx,h0*HOP,(h1+1)*HOP),'hops':[h0,h1],'bands':[k0,k1],
               'lowHz':r(EDGES[k0,0],0),'highHz':r(EDGES[k1,1],0),'centerHz':r(CENTERS[kc],0),
               'numbers':{'maskeeToDominantDb':r(ratio),'cellsWithin6dB':int(cells.sum()),'cellsInRegion':int(cells.size),
                          'coverage':r(cells.mean(),3),'importanceDb':{a:r(ia),b:r(ib)},'importanceMarginDb':r(abs(ia-ib)),
                          'maskeeBandShareDb':r(share_db),'score':r(masking_score(Lmm,Ld,span,ks))},
               'severity':float(cells.sum()*HOP*(CLOSE+min(ratio,CLOSE)))}
        found['label']=('%s: %s and %s overlap %s-%s Hz within %.0f dB (%s %+.1f dB against %s there); %s is the more important track here (%.1f vs %.1f, loudness + activity)'
                        %(found['bars'],a,b,_hz(found['lowHz']),_hz(found['highHz']),CLOSE,m,ratio,d,d,max(ia,ib),min(ia,ib)))
        out.append(found)
    return sorted(out,key=lambda x:-x['severity'])[:20]

def build_up(ctx,f):
    """Which track built a masking clash up, measured: each track's level in the clash bands over the clash, against
    its own median level in those bands where it plays outside the clash (from 1 s either side). A builder rose by
    >= BUILDUP_DB there, and by BUILDUP_DB more than in its other bands (a band build-up, not a level change), while
    the other track rose by less than half that. None when that is not clear."""
    h0,h1=f['hops'];ks=np.arange(f['bands'][0],f['bands'][1]+1);span=np.arange(h0,min(h1+1,ctx.H));oth=np.setdiff1d(np.arange(KMIN,len(CENTERS)),ks)
    rows={}
    for t in (f['dominant'],f['maskee']):
        act=ctx.active[t].copy();act[max(0,h0-4):h1+5]=False;ref=np.flatnonzero(act)
        if len(ref)<8:return None
        L=db(ctx.f[t]['fine']);ref_in=np.median(L[np.ix_(ref,ks)],axis=0)
        per=np.mean(L[np.ix_(span,ks)]-ref_in,axis=1)
        out_rise=float(np.median(db(ctx.f[t]['fine'][np.ix_(span,oth)].sum(1)))-np.median(db(ctx.f[t]['fine'][np.ix_(ref,oth)].sum(1))))
        rows[t]={'inBandRiseDb':float(np.median(per)),'outOfBandRiseDb':out_rise,'perHop':per}
    builders=[t for t,v in rows.items() if v['inBandRiseDb']>=BUILDUP_DB and v['inBandRiseDb']-v['outOfBandRiseDb']>=BUILDUP_DB]
    if len(builders)!=1:return None
    b=builders[0];o=next(t for t in rows if t!=b)
    if rows[o]['inBandRiseDb']>=BUILDUP_DB/2:return None
    ref_bars=ctx.tb.span(0,ctx.n/SR)
    return {'track':b,'other':o,'riseDb':r(rows[b]['inBandRiseDb']),'outOfBandRiseDb':r(rows[b]['outOfBandRiseDb']),'otherRiseDb':r(rows[o]['inBandRiseDb']),
            'perHop':[r(float(x)) for x in rows[b]['perHop']],
            'basis':'median level per 1/3 octave in %s-%s Hz where each track plays outside %s (within %s), against the clash span'%(_hz(EDGES[ks[0],0]),_hz(EDGES[ks[-1],1]),f['bars'],ref_bars)}

def masking_score(Lm,Ld,span,ks):
    return float(np.sum(np.maximum(0,(Lm-Ld)[np.ix_(span,ks)]+CLOSE)))

def _hz(x):return f'{x/1000:.1f}k' if x>=1000 else f'{x:.0f}'

def lf_frames(ctx_f,h0,h1):
    F=ctx_f['lf'].shape[0];return np.arange(max(0,h0*FPH-FPH//2),min(F,(h1+1)*FPH+FPH//2))

def lf_ratio(lfm,lfd,frames):
    """Maskee-to-dominant LF ratio where and when the dominant sounds: frames within 12 dB of its loudest
    frame in the span, bins within 12 dB of its strongest bin in that frame. Returns (dB, score, frames used)."""
    A=np.abs(lfm[frames])**2;B=np.abs(lfd[frames])**2
    bt=B.sum(1)
    if bt.max()<=1e-20:return None,0.0,0
    on=bt>=bt.max()*10**(-1.2)
    M=B>=B.max(1,keepdims=True)*10**(-1.2)
    num=(A*M).sum(1)[on];den=(B*M).sum(1)[on]
    rf=db(num)-db(den)
    return float(db(num.sum())-db(den.sum())),float(np.sum(np.maximum(0,rf+CLOSE))),int(on.sum())

def detect_low_end(ctx,a,b):
    fa,fb,fm=ctx.f[a],ctx.f[b],ctx.fm
    la,lb,lm=db(fa['lfPower']),db(fb['lfPower']),db(fm['lfPower'])
    sig=(la>=lm-12)&(lb>=lm-12)&(lm>=db(fm['total'])-25)&ctx.active[a]&ctx.active[b]
    clash=np.zeros(ctx.H,bool);rab=np.full(ctx.H,np.nan);rba=np.full(ctx.H,np.nan)
    for h in np.flatnonzero(sig):
        fr=lf_frames(fa,h,h)
        x,_,_=lf_ratio(fa['lf'],fb['lf'],fr);y,_,_=lf_ratio(fb['lf'],fa['lf'],fr)
        rab[h]=np.nan if x is None else x;rba[h]=np.nan if y is None else y
        clash[h]=np.nanmax([rab[h],rba[h],-99])>=-CLOSE
    out=[]
    for h0,h1 in segments(clash,4,2):
        span=np.arange(h0,h1+1);ia,ib=ctx.importance(a,span),ctx.importance(b,span)
        fr=lf_frames(fa,h0,h1);xab,_,_=lf_ratio(fa['lf'],fb['lf'],fr);xba,_,_=lf_ratio(fb['lf'],fa['lf'],fr)
        if xab is None or xba is None:continue
        # direction from the shared band itself: the track that encroaches on the other's moments (higher ratio
        # against it) is moved; section importance decides only when the two are within 1 dB
        if abs(xab-xba)>=1:d,m=(b,a) if xab>xba else (a,b)
        else:d,m=(a,b) if ia>=ib else (b,a)
        ratio,score,nf=lf_ratio(ctx.f[m]['lf'],ctx.f[d]['lf'],fr)
        A=np.abs(ctx.f[m]['lf'][fr])**2;B=np.abs(ctx.f[d]['lf'][fr])**2
        peak=float(FREQ[LFBINS][int(np.argmax(np.minimum(A,B).sum(0)))])
        both=ctx.f[a]['lf'][fr]+ctx.f[b]['lf'][fr]
        inter=float(db((np.abs(both)**2).sum())-db((np.abs(ctx.f[a]['lf'][fr])**2+np.abs(ctx.f[b]['lf'][fr])**2).sum()))
        def crest(x):
            p=(np.abs(x[fr])**2).sum(1);return float(db(p.max())-db(p.mean()))
        mf=ctx.f[m];share=float(db(mf['lfPower'][span].sum())-db(mf['total'][span].sum()))
        top=float(FREQ[LFBINS][np.flatnonzero((np.minimum(A,B).sum(0))>=np.minimum(A,B).sum(0).max()*.1).max()])+FREQ[1]
        found={'kind':'low-end-clash','tracks':[a,b],'dominant':d,'maskee':m,**_bars(ctx,h0*HOP,(h1+1)*HOP),'hops':[h0,h1],
               'lowHz':LF[0],'highHz':LF[1],'centerHz':r(peak,1),'topHz':r(min(LF[1],top),0),
               'numbers':{'maskeeToDominantDb':r(ratio),'score':r(score),'dominantFrames':nf,'interferenceDb':r(inter),
                          'dominantLfCrestDb':r(crest(ctx.f[d]['lf'])),'maskeeLfCrestDb':r(crest(ctx.f[m]['lf'])),
                          'maskeeLfShareDb':r(share),'importanceDb':{a:r(ia),b:r(ib)},'importanceMarginDb':r(abs(ia-ib)),
                          'inBandDb':{'%s against %s'%(a,b):r(xab),'%s against %s'%(b,a):r(xba)}},
               'severity':float((h1-h0+1)*HOP*(CLOSE+min(ratio,CLOSE)))}
        found['label']=('%s: %s and %s share 30-150 Hz (strongest %.0f Hz): %s sits %+.1f dB against %s while %s sounds; summing them changes the low end by %+.1f dB'
                        %(found['bars'],a,b,peak,m,ratio,d,d,inter))
        out.append(found)
    return out

def _section_of(sections,h):
    for s in sections:
        if s['hops'][0]<=h<=s['hops'][1]:return s
    return sections[-1]

def smooth_level(p,size=4):
    """Power-mean over `size` hops, centred (0.5 s windows already centred on each hop): no metering lag."""
    return db(ndimage.uniform_filter1d(np.asarray(p,float),size,mode='nearest'))

def detect_drift(ctx,name,sections):
    if name not in ctx.rest:return []
    f=ctx.f[name];rest_p=ctx.rest_power(name)
    own=smooth_level(f['total']);rest=smooth_level(rest_p);bal=own-rest
    ok=ctx.active[name]&(rest>=db(ctx.fm['total']).max()-60)
    dev=np.full(ctx.H,np.nan);odev=np.full(ctx.H,np.nan);ref={}
    for s in sections:
        h=np.arange(s['hops'][0],s['hops'][1]+1);k=h[ok[h]]
        if len(k)<8:continue
        mb=float(np.median(bal[k]));mo=float(np.median(own[k]));ref[s['index']]=(mb,mo)
        dev[k]=bal[k]-mb;odev[k]=own[k]-mo
    out=[]
    # Each section has its own reference level. Neither duration thresholds nor
    # reported deviations can join across a change of that reference.
    for s in sections:
        first,last=s['hops']
        for sign in (1,-1):
            flag=np.nan_to_num(sign*dev[first:last+1],nan=-99)>=DRIFT
            for lo,hi in segments(flag,8,1):
                h0,h1=first+lo,first+hi
                span=np.arange(h0,h1+1)
                d=float(np.nanmean(dev[span]));o=float(np.nanmean(odev[span]))
                if abs(d)<DRIFT:continue
                own_moved=np.sign(o)==np.sign(d) and abs(o)>=DRIFT_OWN
                found={'kind':'level-drift' if own_moved else 'balance-change','tracks':[name],'maskee':name,**_bars(ctx,h0*HOP,(h1+1)*HOP),'hops':[h0,h1],
                       'section':s['index'],'numbers':{'balanceDeviationDb':r(d),'ownLevelDeviationDb':r(o),'sectionBalanceDb':r(ref[s['index']][0]),
                       'peakDeviationDb':r(float(np.nanmax(sign*dev[span]))*sign)},'severity':float((h1-h0+1)*HOP*abs(d)),
                       'deviation':[r(x) for x in dev[span]]}
                found['label']=('%s: %s is %+.1f dB against the rest of the mix compared with its own median in section %d; its own level moved %+.1f dB'
                                %(found['bars'],name,d,s['index'],o))+('' if own_moved else ' - the rest changed, not the track, so no move is planned')
                out.append(found)
    return out

def detect_stereo(ctx,name):
    f=ctx.fm if name=='__mix__' else ctx.f[name];act=ctx.fm['lufs']>-70 if name=='__mix__' else ctx.active[name]
    out=[];lowshare=db(f['bandL'][:,:2].sum(1)+f['bandR'][:,:2].sum(1))-db(2*f['total'])
    lowon=act&(lowshare>=np.nanmedian(lowshare[act])-6 if act.any() else False)
    if name!='__mix__':
        mlow=ctx.fm['bandL'][:,:2].sum(1)+ctx.fm['bandR'][:,:2].sum(1);lowon&=db(f['bandL'][:,:2].sum(1)+f['bandR'][:,:2].sum(1))>=db(mlow)-CONTRIB
    # A side/total ratio of 0.5 also occurs in numerical leakage from sounds
    # containing no bass. Correlation is undefined there, not evidence of a
    # bass phase fault. It must be measurable before making this diagnosis.
    lowon&=np.isfinite(f['lowCorr'])
    lc=np.nan_to_num(f['lowCorr'],nan=1);ls=f['lowSide']
    flag=lowon&((lc<LOW_CORR)|(ls>=LOW_SIDE))
    for h0,h1 in segments(flag,4,1):
        span=np.arange(h0,h1+1);bands=[]
        for b in (0,1):
            corr=_band_corr(f,b)[span];valid=np.isfinite(corr)
            if valid.any() and (np.mean(corr[valid])<LOW_CORR or np.mean(_band_side(f,b)[span][valid])>=LOW_SIDE):bands.append(b)
        top=dsp.BANDS[max(bands or [1])][2]
        found={'kind':'low-end-phase','tracks':[name],'maskee':name,**_bars(ctx,h0*HOP,(h1+1)*HOP),'hops':[h0,h1],'highHz':top,
               'numbers':{'lowCorrelation':r(np.nanmean(f['lowCorr'][span])),'lowSideShare':r(np.nanmean(ls[span])),'monoLossDb':r(np.nanmean(f['monoLossDb'][span]))},
               'severity':float((h1-h0+1)*HOP*(1-np.nanmean(lc[span])))}
        found['label']='%s: %s low end (20-%d Hz) correlation %.2f, side share %.2f - it cancels or thins in mono'%(found['bars'],'the mix' if name=='__mix__' else name,top,found['numbers']['lowCorrelation'],found['numbers']['lowSideShare'])
        out.append(found)
    w=f['width'];good=act&np.isfinite(w)
    if good.sum()>=8:
        med=float(np.median(w[good]));mad=max(1.4826*float(np.median(np.abs(w[good]-med))),.02)
        if med>=.05:
            flag=good&((w-med)/mad<=-4)&(w<=.5*med)
            for h0,h1 in segments(flag,4,1):
                span=np.arange(h0,h1+1)
                found={'kind':'width-collapse','tracks':[name],'maskee':name,**_bars(ctx,h0*HOP,(h1+1)*HOP),'hops':[h0,h1],
                       'numbers':{'width':r(np.mean(w[span]),3),'medianWidth':r(med,3),'z':r((np.mean(w[span])-med)/mad,1)},'severity':float((h1-h0+1)*HOP*(med-np.mean(w[span]))*10)}
                found['label']='%s: %s stereo width (side share above 200 Hz) falls to %.2f from its median %.2f'%(found['bars'],'the mix' if name=='__mix__' else name,found['numbers']['width'],med)
                out.append(found)
    return out

def _band_corr(f,b):
    with np.errstate(invalid='ignore',divide='ignore'):return np.where(f['bandL'][:,b]*f['bandR'][:,b]>1e-20,f['bandC'][:,b]/np.sqrt(f['bandL'][:,b]*f['bandR'][:,b]),np.nan)
def _band_side(f,b):
    L,R,C=f['bandL'][:,b],f['bandR'][:,b],f['bandC'][:,b];return np.maximum(0,L+R-2*C)/np.maximum(2*(L+R),1e-20)

def attribute(ctx,finding):
    """Which track's removal most improves a summed-mix finding over its span (leave one out, measured)."""
    h0,h1=finding['hops'];a,b=h0*HS,(h1+1)*HS;key={'low-end-phase':'lowCorr','width-collapse':'width'}[finding['kind']]
    base=np.nanmean(features(ctx.mix[a:b])[key]);rows=[]
    for k,y in ctx.stems.items():
        v=np.nanmean(features(ctx.mix[a:b]-y[a:b])[key]);rows.append({'track':k,'without':r(v,3),'change':r(v-base,3)})
    rows.sort(key=lambda x:-(x['change'] or -9));return rows

def detect_sections(ctx):
    """Boundaries where the mix's spectral shape and the set of active tracks change, on bar lines."""
    fm=ctx.fm;H=ctx.H
    # spectral shape in units of 10 dB (a whole-band 10 dB change counts like one track entering), activity x3
    shape=np.clip(db(fm['fine'])-db(fm['total'])[:,None],-60,0)
    act=np.array([ctx.active[k].astype(float) for k in ctx.names]).T*3
    # With no active track anywhere in the capture, tiny spectral leakage is
    # not evidence of musical sections. Do not gate individual hops here:
    # momentary metering warm-up would invent an entrance at the start.
    if not np.any(act):shape[:]=0
    shape=ndimage.uniform_filter1d(shape,4,axis=0)/10.0
    X=np.hstack([shape/np.sqrt(shape.shape[1]),ndimage.uniform_filter1d(act,4,axis=0)])
    K=max(4,int(round(ctx.tb.bpb*60/ctx.tb.tempo/HOP)))               # one bar each side
    nov=np.zeros(H)
    for h in range(K,H-K+1):nov[h]=np.linalg.norm(X[h:h+K].mean(0)-X[h-K:h].mean(0))
    med=float(np.median(nov[K:H-K+1])) if H>2*K else 0;mad=max(1.4826*float(np.median(np.abs(nov[K:H-K+1]-med))) if H>2*K else 0,1e-3)
    thr=max(med+4*mad,.3*nov.max())
    cands=[h for h in range(1,H-1) if nov[h]>=thr and nov[h]==nov[max(0,h-K):h+K+1].max()]
    edges_t=[ctx.tb.seconds(b) for b in ctx.tb.clip_edges]
    bounds=[]
    for h in cands:
        t=h*HOP;near=[e for e in edges_t if abs(e-t)<=ctx.tb.bpb*60/ctx.tb.tempo]
        t=min(near,key=lambda e:abs(e-t)) if near else ctx.tb.snap(t)
        if 0<t<ctx.n/SR and all(abs(t-b)>=2*ctx.tb.bpb*60/ctx.tb.tempo-1e-6 for b in bounds):bounds.append(t)
    # Assign each hop by its centre, exactly once. Rounding the start and
    # ceiling the end used to count a boundary hop in both adjacent sections.
    centres=np.minimum((np.arange(H)+.5)*HOP,(ctx.n-.5)/SR)
    bounds=sorted(bounds);kept=[];last=0
    for t in bounds:
        split=int(np.searchsorted(centres,t))
        if last<split<H:kept.append(t);last=split
    edges=[0.0]+kept+[ctx.n/SR];out=[]
    for i,(t0,t1) in enumerate(zip(edges,edges[1:])):
        h0=int(np.searchsorted(centres,t0));h1=int(np.searchsorted(centres,t1))-1
        out.append({'index':i+1,**_bars(ctx,t0,t1),'hops':[h0,h1],'novelty':r(nov[min(H-1,int(round(t0/HOP)))],3) if i else None})
    return out,nov

def section_balance(ctx,sections):
    rows=[];changes=[]
    for s in sections:
        h=np.arange(s['hops'][0],s['hops'][1]+1)
        levels=ctx.fm['lufs'][h];valid=np.isfinite(levels)
        ml=float(db(np.mean(np.where(valid,10**(np.nan_to_num(levels,nan=-200)/10),0)))) if valid.any() else None
        rows.append({'section':s['index'],'bars':s['bars'],'mixLUFS':r(ml),
                     'loudnessBasis':'Power mean of momentary readings across assigned hops, not integrated LUFS; unavailable readings contribute zero power.',
                     'measuredHopFraction':float(valid.mean()),
                     'tracks':{k:(r(ctx.importance(k,h)-ml) if ml is not None and ctx.active[k][h].any() else None) for k in ctx.names}})
    for a,b in zip(rows,rows[1:]):
        for k in ctx.names:
            x,y=a['tracks'][k],b['tracks'][k]
            if x is not None and y is not None and abs(y-x)>=DRIFT:
                changes.append({'kind':'section-change','tracks':[k],'fromSection':a['section'],'toSection':b['section'],'changeDb':r(y-x),
                                'label':'%s moves %+.1f dB against the mix from section %d (%s) to section %d (%s)'%(k,y-x,a['section'],a['bars'],b['section'],b['bars'])})
    return rows,changes

def analyse(ctx):
    sections,nov=detect_sections(ctx);findings=[]
    pairs=[(a,b) for i,a in enumerate(ctx.names) for b in ctx.names[i+1:] if ctx.active[a].any() and ctx.active[b].any()]
    for a,b in pairs:findings+=detect_masking(ctx,a,b)+detect_low_end(ctx,a,b)
    for k in ctx.names:findings+=detect_drift(ctx,k,sections)+detect_stereo(ctx,k)
    for fnd in detect_stereo(ctx,'__mix__'):fnd['attribution']=attribute(ctx,fnd);fnd['tracks']=['__mix__'];findings.append(fnd)
    balance,changes=section_balance(ctx,sections)
    findings.sort(key=lambda x:x['startSeconds'])
    for i,x in enumerate(findings):x['id']='F%d'%(i+1)
    return {'sections':sections,'novelty':[r(x,3) for x in nov],'findings':findings,'sectionBalance':balance,'sectionChanges':changes}

# ---------------------------------------------------------------- offline models of moves (time-varying)
def envelope(points,n,t0,log=False):
    """Per-sample curve value for samples t0 .. t0+n (seconds), linear between points (log-frequency when log)."""
    ts=np.array([p['seconds'] for p in points]);vs=np.array([p['value'] for p in points],float)
    t=t0+np.arange(n)/SR
    return np.exp(np.interp(t,ts,np.log(vs))) if log else np.interp(t,ts,vs)

def _tv_filter(y,env,make,block=480,quant=.05):
    """Block-wise time-varying filter with carried state (how a DAW applies automation to a filter)."""
    out=np.empty_like(y);zi=None;last=None;sos=None
    for a in range(0,len(y),block):
        v=round(float(np.mean(env[a:a+block]))/quant)*quant
        if v!=last:
            new=make(v)
            if zi is None or new.shape!=sos.shape:zi=np.zeros((new.shape[0],2,y.shape[1]))
            sos=new;last=v
        out[a:a+block],zi=sosfilt(sos,y[a:a+block],axis=0,zi=zi)
    return out

def eq_sos(shape,hz,gain_db=0.0,q=.71,slope=48):
    """Standard RBJ bell / Butterworth high-pass: a model of the move, not the DAW's exact curve.
    Uses mix_actions.eq_sos (Agent A) when present so both modules predict with the same filters."""
    try:
        import mix_actions;return mix_actions.eq_sos(shape,hz,gain_db,q,slope,SR)
    except (ImportError,AttributeError):
        w=2*math.pi*min(hz,SR*.49)/SR;cs,sn=math.cos(w),math.sin(w);al=sn/(2*q);A=10**(gain_db/40)
        if shape=='bell':b,a=[1+al*A,-2*cs,1-al*A],[1+al/A,-2*cs,1-al/A];return np.array([[b[0]/a[0],b[1]/a[0],b[2]/a[0],1,a[1]/a[0],a[2]/a[0]]])
        return butter(slope//6,min(hz,SR*.49),'highpass',fs=SR,output='sos')

def _compress(y,key,thr_env,ratio,attack_ms,release_ms,knee=6.0):
    """Feed-forward peak compressor keyed by `key`, threshold per sample, gain smoothed at 1 ms steps."""
    B=48;n=len(y);m=math.ceil(n/B);k=np.abs(np.asarray(key)).max(axis=1)
    k=np.pad(k,(0,m*B-len(k)))[:m*B].reshape(m,B).max(1);lvl=20*np.log10(np.maximum(k,1e-9))
    T=np.pad(thr_env,(0,m*B-n),mode='edge')[:m*B].reshape(m,B).mean(1);over=lvl-T
    gr=np.where(over<=-knee/2,0.0,np.where(over>=knee/2,over*(1-1/ratio),(1-1/ratio)*(over+knee/2)**2/(2*knee)))
    at=math.exp(-1/max(attack_ms,.05));rl=math.exp(-1/max(release_ms,1));g=0.0;env=np.empty(m)
    for i,x in enumerate(gr):
        g=at*g+(1-at)*x if x>g else rl*g+(1-rl)*x;env[i]=g
    gain=10**(-np.interp(np.arange(n)/B,np.arange(m)+.5,env)/20)
    return y*gain[:,None]

def _band_gain_stft(y,frame_gain_db,hz):
    """Apply per-frame gains given on frequencies `hz` (measured plug-in response) via STFT overlap-add."""
    from scipy.signal import stft,istft
    f,t,Z=stft(y.T,SR,nperseg=2048,noverlap=1536)
    g=np.array([np.interp(np.log(np.maximum(f,1)),np.log(hz),row) for row in frame_gain_db(t)])      # frames x bins
    _,out=istft(Z*10**(g.T[None]/20),SR,nperseg=2048,noverlap=1536)
    return out.T[:len(y)]

def render(move,y,t0,ctx=None,key=None):
    """The move applied to stem audio `y` starting at `t0` seconds. Pure; returns new audio."""
    k=move['model'];pts=move['points'];n=len(y)
    if k=='gain' or k=='send':return y*10**(envelope(pts,n,t0)/20)[:,None]
    if k=='pan':
        p=envelope(pts,n,t0);return y*np.stack([np.minimum(1,1-p),np.minimum(1,1+p)],axis=1)
    if k=='bell':return _tv_filter(y,envelope(pts,n,t0),lambda g:eq_sos('bell',move['hz'],g,move['q']))
    if k=='high-pass':
        return _tv_filter(y,envelope(pts,n,t0,log=True),lambda hz:eq_sos('high-pass',hz,0,.71,move.get('slope',48)),quant=1.0)
    if k=='sidechain':
        return _compress(y,key,envelope(pts,n,t0),move['ratio'],move['attackMs'],move['releaseMs'])
    if k in ('width','bass-mono'):
        m=(y[:,0]+y[:,1])/2;s=(y[:,0]-y[:,1])/2;e=envelope(pts,n,t0)
        if k=='width':s=s*e/100.0
        else:
            # zero-phase crossover (Utility's bass mono is a crossover; a model): side below the frequency removed
            lp=butter(4,move['hz'],'lowpass',fs=SR,output='sos');s=s-e*sosfiltfilt(lp,s,axis=0)
        return np.stack([m+s,m-s],axis=1)
    if k=='invert':
        e=envelope(pts,n,t0)>=.5;out=y.copy();out[e,move['channel']]*=-1;return out
    if k=='plugin':
        resp=move['response'];vals=np.array(resp['values'],float);G=np.array(resp['gainDb'],float)
        def frame_gain(t):
            v=np.interp(t0+t,[p['seconds'] for p in pts],[p['value'] for p in pts])
            return np.array([[np.interp(x,vals,G[:,j]) for j in range(G.shape[1])] for x in v])
        return _band_gain_stft(y,frame_gain,np.array(resp['hz'],float))
    raise ValueError('Unknown move model %r'%k)

# ---------------------------------------------------------------- curves
def rdp(points,tol):
    if len(points)<3:return points
    (x0,y0),(x1,y1)=points[0],points[-1];best,idx=0,0
    for i,(x,y) in enumerate(points[1:-1],1):
        yl=y0+(y1-y0)*(x-x0)/(x1-x0) if x1!=x0 else y0;d=abs(y-yl)
        if d>best:best,idx=d,i
    if best<=tol:return [points[0],points[-1]]
    return rdp(points[:idx+1],tol)[:-1]+rdp(points[idx:],tol)

def shape_curve(ctx,h0,h1,amount,neutral,unit,why,tol=.5,maxpts=16,value_of=None,limits=None):
    """Curve over hops h0..h1 following `amount` (per hop, in the move's units), neutral outside, one-beat
    ramps outside the problem so the move is in place when it starts, start/end on beats (bar lines within a beat)."""
    tb=ctx.tb;beat=60/tb.tempo;ramp=max(beat,HOP)
    s0=tb.snap(h0*HOP);s1=tb.snap((h1+1)*HOP)
    if s1<=s0:s1=s0+beat
    a=np.asarray(amount,float)
    if len(a)>=5:a=ndimage.median_filter(a,size=5,mode='nearest')
    if limits:a=np.clip(a,*limits)
    ts=np.linspace(s0,s1,len(a)) if len(a)>1 else np.array([s0,s1]);vals=a if len(a)>1 else np.repeat(a,2)
    core=rdp(list(zip(ts.tolist(),vals.tolist())),tol)
    while len(core)>maxpts:tol*=1.5;core=rdp(list(zip(ts.tolist(),vals.tolist())),tol)
    # the Arrangement continues outside the analysed stems: ramps may sit before or after them (never before beat 0)
    pts=[{'seconds':max(tb.seconds(0.0),s0-ramp),'value':neutral,'reason':'%s: %s starts'%(tb.label(s0),why['start'])}]
    for i,(t,v) in enumerate(core):
        pts.append({'seconds':t,'value':float(v),'reason':why['hold'](t,v)})
    pts.append({'seconds':s1+ramp,'value':neutral,'reason':'%s: %s ends, back to %s'%(tb.label(s1),why['end'],_fmt(neutral,unit))})
    clean=[]
    for p in pts:
        if clean and p['seconds']<=clean[-1]['seconds']+1e-6:continue
        clean.append(p)
    for p in clean:p['beat']=r(tb.beat(p['seconds']),4);p['seconds']=r(p['seconds'],4);p['value']=r(p['value'],3)
    return clean

def _fmt(v,unit):
    return {'dB':'%+.1f dB','Hz':'%.0f Hz','%':'%.0f%%','pan':'%+.2f','amount':'%.2f'}.get(unit,'%.3g')%v

# ---------------------------------------------------------------- candidates, prediction, choice
def _slice(ctx,f):
    h0,h1=f['hops'];a=max(0,h0-8);b=min(ctx.H,h1+9);return a,b

def _apply(ctx,f,move,a,b):
    """(maskee before, maskee after, mix before, mix after) over hops a..b with the move rendered."""
    s0,s1=a*HS,min(ctx.n,b*HS);t0=s0/SR;m=move['track']
    y=ctx.stems[m][s0:s1];key=ctx.stems[move['keyTrack']][s0:s1] if move.get('keyTrack') else None
    y2=render(move,y,t0,ctx,key)
    if move['model']=='send':
        # the return's audio scales with the one feeding send (share measured from send level when several feed it)
        share=move.get('share',1.0);y2=y+(y2-y)*share
    return y,y2,ctx.mix[s0:s1],ctx.mix[s0:s1]-y+y2

def evaluate(ctx,f,move):
    """Measure the finding's own numbers before and after the move on the offline-processed stems."""
    a,b=_slice(ctx,f);y,y2,mx,mx2=_apply(ctx,f,move,a,b)
    fy,fy2,fm,fm2=features(y),features(y2),features(mx),features(mx2)
    h0,h1=f['hops'];span=np.arange(h0-a,h1-a+1);span=span[span<fy['hops']]
    def lufs(ff):return float(db(np.nanmean(10**(np.nan_to_num(ff['lufs'][span],nan=-200)/10))))
    out={'maskeeLUFS':[r(lufs(fy)),r(lufs(fy2))],'mixLUFS':[r(lufs(fm)),r(lufs(fm2))]}
    kind=f['kind']
    if kind=='masking':
        d=f['dominant'];fd=features(ctx.stems[d][a*HS:min(ctx.n,b*HS)]);ks=np.arange(f['bands'][0],f['bands'][1]+1)
        sc=[];ra=[]
        for ff in (fy,fy2):
            s=[];q=[]
            for ch in ('fineL','fineR'):
                Lm,Ld=db(ff[ch]),db(fd[ch]);s.append(masking_score(Lm,Ld,span,ks));q.append(float(np.mean((Lm-Ld)[np.ix_(span,ks)])))
            sc.append(float(np.mean(s)));ra.append(float(np.mean(q)))
        out.update(metric='%s against %s in %s-%s Hz (dB)'%(move['track'],d,_hz(EDGES[ks[0],0]),_hz(EDGES[ks[-1],1])),before=r(ra[0]),after=r(ra[1]),scoreBefore=r(sc[0]),scoreAfter=r(sc[1]))
        oth=np.setdiff1d(np.arange(len(CENTERS)),ks)
        out['collateralDb']=out['maskeeOutsideBandDb']=r(float(db(fy2['fine'][np.ix_(span,oth)].sum())-db(fy['fine'][np.ix_(span,oth)].sum())))
        ok=sc[1]<=(1-IMPROVE)*sc[0] and ra[0]-ra[1]>=IMPROVE_DB
    elif kind=='low-end-clash':
        d=f['dominant'];fd=features(ctx.stems[d][a*HS:min(ctx.n,b*HS)]);fr=lf_frames(fy,span[0],span[-1])
        x0,s0,_=lf_ratio(fy['lf'],fd['lf'],fr);x1,s1,_=lf_ratio(fy2['lf'],fd['lf'],fr)
        out.update(metric='%s against %s at 30-150 Hz while %s sounds (dB)'%(move['track'],d,d),before=r(x0),after=r(x1),scoreBefore=r(s0),scoreAfter=r(s1))
        ok=x0 is not None and x1 is not None and s1<=(1-IMPROVE)*s0 and x0-x1>=IMPROVE_DB
        dt=(np.abs(fd['lf'][fr])**2).sum(1);quiet=fr[dt<dt.max()*.01]          # frames where the dominant is 20 dB down
        Bd=(np.abs(fd['lf'][fr])**2).mean(0);other=Bd<Bd.max()*10**-1.2          # 30-150 Hz bins the dominant does not occupy
        A0=(np.abs(fy['lf'][fr])**2);A1=(np.abs(fy2['lf'][fr])**2)
        out['maskeeWhereDominantSilentDb']=r(float(db(fy2['frameTotal'][quiet].sum())-db(fy['frameTotal'][quiet].sum()))) if len(quiet) else None
        out['maskeeLowEndOutsideDominantBinsDb']=r(float(db(A1[:,other].sum())-db(A0[:,other].sum()))) if other.any() else None
        out['collateralDb']=max(abs(x) for x in (out['maskeeWhereDominantSilentDb'],out['maskeeLowEndOutsideDominantBinsDb'],0.0) if x is not None)
    elif kind=='level-drift':
        rest=smooth_level(features(mx-y)['total']);bal0=smooth_level(fy['total'])-rest;bal1=smooth_level(fy2['total'])-rest;ref=f['numbers']['sectionBalanceDb']
        e0=float(np.nanmean(np.abs(bal0[span]-ref)));e1=float(np.nanmean(np.abs(bal1[span]-ref)))
        out.update(metric='balance against the rest vs the section median (mean |dB|)',before=r(e0),after=r(e1))
        ok=e1<=(1-IMPROVE)*e0 and e0-e1>=IMPROVE_DB
    elif kind=='low-end-phase':
        # Compare the same originally measurable hops. Dropping cancelled
        # (NaN) hops from the mean used to reward destroying the bass.
        valid=np.isfinite(fy['lowCorr'][span]);corr0=fy['lowCorr'][span][valid];corr1=fy2['lowCorr'][span][valid]
        if not valid.any():
            return dict(out,improved=False,benefit=None,error='No measurable original low-band correlation in this span.')
        power=lambda ff:ff['bandL'][span,:2].sum(1)+ff['bandR'][span,:2].sum(1)
        p0,p1=power(fy)[valid],power(fy2)[valid]
        retained=np.clip(p1/np.maximum(p0,1e-20),0.,1.)
        finite=np.isfinite(corr1)
        change=np.where(finite,np.nan_to_num(corr1)-corr0,0.)*retained
        c0=float(np.mean(corr0));c1=c0+float(np.mean(change))
        m0=float(np.nanmean(fm['monoLossDb'][span]));m1=float(np.nanmean(fm2['monoLossDb'][span]))
        low_change=float(db(p1.sum())-db(p0.sum()))
        out.update(metric='20-200 Hz L/R correlation improvement weighted by retained bass energy',before=r(c0),after=r(c1),
                   afterCorrelationRaw=r(float(np.mean(corr1[finite]))) if finite.any() else None,
                   afterCorrelationCoverage=r(float(np.mean(finite)),3),retainedLowBandFraction=r(float(np.mean(retained)),3),
                   lowBandEnergyChangeDb=r(low_change),mixMonoLossDb=[r(m0),r(m1)])
        sd=lambda ff:float(np.sum(np.maximum(0,ff['bandL'][span,2:]+ff['bandR'][span,2:]-2*ff['bandC'][span,2:])))
        side0,side1=sd(fy)/4,sd(fy2)/4
        side_db=float(max(-60.0,db(side1)-db(side0)))
        # A large dB ratio in vanishing high-band leakage is not large damage.
        side_share=min(1.,max(side0,side1)/max(float(fy['total'][span].sum()),float(fy2['total'][span].sum()),1e-20))
        out['sideAbove200HzDb']=r(side_db);out['sideAbove200HzEnergyFraction']=r(side_share,6)
        out['collateralDb']=r(abs(side_db)*side_share+abs(low_change))
        ok=c1-c0>=.2
    elif kind=='width-collapse':
        w0=float(np.nanmean(fy['width'][span]));w1=float(np.nanmean(fy2['width'][span]));med=f['numbers']['medianWidth']
        out.update(metric='side share above 200 Hz (track median %.2f)'%med,before=r(w0,3),after=r(w1,3))
        ok=(w1-w0)>=IMPROVE*(med-w0)
    else:raise ValueError(kind)
    rel=None
    if out.get('scoreBefore'):rel=1-out['scoreAfter']/max(out['scoreBefore'],1e-9)
    elif kind=='level-drift':rel=1-out['after']/max(out['before'],1e-9)
    elif kind=='low-end-phase':rel=(out['after']-out['before'])/2
    elif kind=='width-collapse':rel=1-abs(med-out['after'])/max(abs(med-out['before']),1e-9)
    if kind=='level-drift':cost=0.0                                   # the level change is the move itself
    elif out.get('collateralDb') is not None:cost=abs(out['collateralDb'])
    else:cost=abs((out['maskeeLUFS'][1] or 0)-(out['maskeeLUFS'][0] or 0))
    out['improved']=bool(ok);out['benefit']=r(rel-COLLATERAL*cost,3) if rel is not None else None
    out['basis']='offline: move rendered on the stem with a standard filter/dynamics model, stems re-summed, the same DSP re-run'
    return out

def _plain(unit,v):return _fmt(v,unit)

def candidates(ctx,f):
    """Moves sized from the finding's numbers, on the less important track, shaped to its time envelope."""
    m=f['maskee'];tb=ctx.tb;h0,h1=f['hops'];span=np.arange(h0,h1+1);out=[];kind=f['kind']
    def mk(model,target,unit,neutral,amount,why,**kw):
        pts=shape_curve(ctx,h0,h1,amount,neutral,unit,why,**({'tol':kw.pop('tol')} if 'tol' in kw else {}))
        return {'track':m,'model':model,'target':target,'unit':unit,'neutral':neutral,'points':pts,**kw}
    if kind=='masking' and (f.get('buildUp') or {}).get('track')==m and f.get('movedBuilder'):
        # The clash was built up by this track (measured): dip it in the clash band by its own rise, back toward its
        # own level elsewhere - not under the other track. Bell + fader candidates; the choice weighs collateral.
        bu=f['buildUp'];d=f['dominant'];ks=np.arange(f['bands'][0],f['bands'][1]+1)
        rise=np.array(bu['perHop'],float);cut=-np.clip(rise,0,MAX_CUT)
        if -cut.min()<MIN_MOVE:return []
        lo,hi=EDGES[ks[0],0],EDGES[ks[-1],1];fc=float(math.sqrt(lo*hi));q=float(np.clip(.7*fc/(hi-lo),.5,6))
        at=lambda t:rise[min(len(rise)-1,max(0,int(round((t-tb.snap(h0*HOP))/HOP))))]
        why=lambda what:{'start':'%s build-up over %s'%(m,d),'end':'build-up',
                         'hold':lambda t,v:'%s: %s %s-%s Hz is %+.1f dB above its own level elsewhere (%s changed %+.1f dB there), masking %s, so %s %s'%(
                             tb.label(t),m,_hz(lo),_hz(hi),at(t),d,bu['otherRiseDb'],d,what,_fmt(v,'dB'))}
        out.append(mk('bell',{'kind':'eq','control':'bandGainDb','shape':'bell','hz':round(fc),'q':round(q,2)},'dB',0.0,cut,why('dip EQ bell to'),hz=fc,q=q))
        out.append(mk('gain',{'kind':'mixer','control':'volume'},'dB',0.0,cut,why('lower the track fader by')))
        return out
    if kind=='masking':
        d=f['dominant'];ks=np.arange(f['bands'][0],f['bands'][1]+1)
        need=np.array([np.mean(db(ctx.f[m]['fine'][h,ks])-db(ctx.f[d]['fine'][h,ks])) for h in span])+MARGIN
        cut=-np.clip(need,0,MAX_CUT)
        if -cut.min()<MIN_MOVE:return []
        lo,hi=EDGES[ks[0],0],EDGES[ks[-1],1];fc=float(math.sqrt(lo*hi));q=float(np.clip(.7*fc/(hi-lo),.5,6))   # bell spans the measured band
        hold=lambda t,v,what:'%s: %s %.1f dB above %s at %s-%s Hz here, so %s %s'%(tb.label(t),m,need[min(len(need)-1,max(0,int(round((t-tb.snap(h0*HOP))/HOP))))]-MARGIN,d,_hz(lo),_hz(hi),what,_fmt(v,'dB'))
        why=lambda what:{'start':'overlap with %s'%d,'end':'overlap','hold':lambda t,v:hold(t,v,what)}
        out.append(mk('bell',{'kind':'eq','control':'bandGainDb','shape':'bell','hz':round(fc),'q':round(q,2)},'dB',0.0,cut,why('dip EQ bell to'),hz=fc,q=q))
        out.append(mk('gain',{'kind':'mixer','control':'volume'},'dB',0.0,cut,why('lower the track fader by')))
        bal=float(np.mean(db(ctx.f[m]['fineR'][np.ix_(span,ks)].sum(1))-db(ctx.f[m]['fineL'][np.ix_(span,ks)].sum(1))))
        dbal=float(np.mean(db(ctx.f[d]['fineR'][np.ix_(span,ks)].sum(1))-db(ctx.f[d]['fineL'][np.ix_(span,ks)].sum(1))))
        side=-1 if dbal>0 or (dbal==0 and bal<0) else 1     # away from where the dominant track is louder
        # pan amount that attenuates the far channel by the needed dB
        p=float(np.clip(1-10**(-np.median(-cut)/20),.1,1.0))*side
        out.append(mk('pan',{'kind':'mixer','control':'pan'},'pan',0.0,np.full(len(span),p),{'start':'overlap with %s'%d,'end':'overlap','hold':lambda t,v:'%s: pan %s to %+.2f, away from the side where %s is louder (%+.1f dB R-L in the band)'%(tb.label(t),m,v,d,dbal)}))
        for resp in ctx.responses.get(m,[]):
            mv=_plugin_move(ctx,f,resp,-cut,ks,mk)
            if mv:out.append(mv)
    elif kind=='low-end-clash':
        d=f['dominant'];n=f['numbers'];need=float(np.clip((n['maskeeToDominantDb'] or 0)+MARGIN,0,MAX_CUT))
        if need<MIN_MOVE:return []
        why=lambda what:{'start':'%s shares the low end with %s'%(m,d),'end':'low-end overlap','hold':lambda t,v:'%s: %s %+.1f dB against %s at %.0f Hz while %s sounds, so %s %s'%(tb.label(t),m,n['maskeeToDominantDb'],d,f['centerHz'],d,what,_fmt(v,'dB'))}
        fc=float(f['centerHz']);q=float(np.clip(fc/max(10,(f['topHz']-LF[0])),.7,4))
        out.append(mk('bell',{'kind':'eq','control':'bandGainDb','shape':'bell','hz':round(fc),'q':round(q,2)},'dB',0.0,np.full(len(span),-need),why('dip EQ bell to'),hz=fc,q=q))
        out.append(mk('high-pass',{'kind':'eq','control':'bandHz','shape':'high-pass','slope':48},'Hz',20.0,np.full(len(span),float(f['topHz'])),
                      {'start':'%s shares the low end with %s'%(m,d),'end':'low-end overlap','hold':lambda t,v:'%s: high-pass %s at %.0f Hz, the top of the shared low end (its 30-150 Hz is %.1f dB of its own energy)'%(tb.label(t),m,v,n['maskeeLfShareDb'])},slope=48))
        # sidechain: keyed by the dominant; threshold set so its hits pull the maskee down by the needed dB
        s0,s1=h0*HS,min(ctx.n,(h1+1)*HS);kp=np.abs(ctx.stems[d][s0:s1]).max(axis=1)
        kp=20*np.log10(np.maximum(np.percentile(kp[kp>0],99) if (kp>0).any() else 1e-9,1e-9));ratio=4.0
        thr=float(kp-need*ratio/(ratio-1))
        ons=_onsets(ctx.f[d]['lf'],h0,h1);rel=float(np.clip(.5*np.median(np.diff(ons))*STEP/SR*1000 if len(ons)>2 else 150,30,300))
        out.append(mk('sidechain',{'kind':'compressor','control':'thresholdDb','sidechain':d,'sidechainId':ctx.track_ids.get(d),'ratio':ratio,'attackMs':1.0,'releaseMs':round(rel)},'dB',0.0,np.full(len(span),thr),
                      {'start':'%s shares the low end with %s'%(m,d),'end':'low-end overlap','hold':lambda t,v:'%s: sidechain compressor on %s keyed by %s, threshold %s (%s peaks %.1f dBFS, ratio 4) ducks it %.1f dB on each hit; release %.0f ms = half the measured hit spacing'%(tb.label(t),m,d,_fmt(v,'dB'),d,kp,need,rel)},
                      keyTrack=d,ratio=ratio,attackMs=1.0,releaseMs=rel))
        out.append(mk('gain',{'kind':'mixer','control':'volume'},'dB',0.0,np.full(len(span),-need),why('lower the fader by')))
    elif kind=='level-drift':
        dev=np.array([np.nan if x is None else x for x in f['deviation']],float);dev=np.where(np.isfinite(dev),dev,np.nanmean(dev))
        out.append(mk('gain',{'kind':'mixer','control':'volume'},'dB',0.0,-np.clip(dev,-MAX_CUT,MAX_CUT),
                      {'start':'balance drift','end':'balance drift','hold':lambda t,v:'%s: %s balance off its section median, fader %s to follow the measured drift back'%(tb.label(t),m,_fmt(v,'dB'))}))
    elif kind=='low-end-phase':
        hz=float(f.get('highHz') or 200)
        out.append(mk('bass-mono',{'kind':'utility','control':'bassMono','hz':hz},'amount',0.0,np.ones(len(span)),
                      {'start':'low-end phase problem','end':'low-end phase problem','hold':lambda t,v:'%s: Utility bass mono below %.0f Hz on %s (low-end correlation %.2f)'%(tb.label(t),hz,m,f['numbers']['lowCorrelation'])},hz=hz))
        out.append(mk('width',{'kind':'utility','control':'widthPercent'},'%',100.0,np.zeros(len(span)),
                      {'start':'low-end phase problem','end':'low-end phase problem','hold':lambda t,v:'%s: Utility width %s on %s for the span'%(tb.label(t),_fmt(v,'%'),m)}))
        if f['numbers']['lowCorrelation'] is not None and f['numbers']['lowCorrelation']<LOW_CORR:
            # anti-phase channels: invert the quieter one (measured over the span)
            fm_=ctx.f[m];ch=int(fm_['bandR'][span,:2].sum()<fm_['bandL'][span,:2].sum())
            out.append(mk('invert',{'kind':'utility','control':'invertRight' if ch else 'invertLeft'},'amount',0.0,np.ones(len(span)),
                          {'start':'low-end phase problem','end':'low-end phase problem','hold':lambda t,v:'%s: Utility invert %s channel of %s (low-end correlation %.2f, the channels are in opposite polarity)'%(tb.label(t),'right' if ch else 'left',m,f['numbers']['lowCorrelation'])},channel=ch))
    elif kind=='width-collapse':
        w=np.array([ctx.f[m]['width'][h] for h in span]);st=f['numbers']['medianWidth']
        g2=(st/(1-st))*((1-w)/np.maximum(w,1e-4));pct=np.clip(100*np.sqrt(g2),100,400)
        out.append(mk('width',{'kind':'utility','control':'widthPercent'},'%',100.0,pct,
                      {'start':'width collapse','end':'width collapse','hold':lambda t,v:'%s: Utility width %s to bring side share back toward %.2f'%(tb.label(t),_fmt(v,'%'),st)},tol=10))
    for mv in out:
        if m in ctx.returns and mv['model']=='gain':
            feeds=ctx.returns[m].get('feeds') or {}
            if feeds:
                src=max(feeds,key=lambda k:feeds[k]);lin={k:10**(v/20) for k,v in feeds.items()}
                send=dict(mv,model='send',track=m,target={'kind':'mixer','control':'send','from':src,'return':m},share=float(lin[src]/sum(lin.values())))
                send['points']=[dict(p,reason=p['reason'].replace('lower the track fader by','lower the send from %s by'%src)) for p in mv['points']]
                out.append(send)
    return out

def _onsets(lf,h0,h1):
    p=(np.abs(lf[lf_frames({'lf':lf},h0,h1)])**2).sum(1);rise=np.diff(p,prepend=p[0])
    from scipy.signal import find_peaks
    pk,_=find_peaks(rise,height=rise.max()*.3 if rise.max()>0 else 1);return pk

def _plugin_move(ctx,f,resp,need,ks,mk):
    """A hosted-plugin parameter whose measured response (band gain per value) best matches the needed dip."""
    hz=np.array(resp['hz'],float);G=np.array(resp['gainDb'],float);vals=np.array(resp['values'],float)
    want=np.zeros(len(CENTERS));want[ks]=-float(np.median(need))
    at=np.array([np.interp(np.log(CENTERS),np.log(hz),row) for row in G])
    err=((at-want)**2).sum(1);i=int(np.argmin(err))
    if at[i][ks].mean()>-MIN_MOVE:return None
    neutral=float(resp.get('neutral',vals[0]));v=float(vals[i])
    mv=mk('plugin',{'kind':'device','device':resp['device'],'deviceName':resp['deviceName'],'parameter':resp['parameter'],'name':resp['name']},'internal',neutral,np.full(f['hops'][1]-f['hops'][0]+1,v),
          {'start':'overlap with %s'%f['dominant'],'end':'overlap','hold':lambda t,x:'%s: %s %s to %.3g - its measured response cuts %s-%s Hz by %.1f dB, closest to the needed dip'%(ctx.tb.label(t),f['maskee'],resp['name'],x,_hz(EDGES[ks[0],0]),_hz(EDGES[ks[-1],1]),-at[i][ks].mean())},response=resp)
    return mv

def plan(ctx,analysis):
    """Proposals (kept) and dropped moves, each with the numbers that decided it."""
    proposals=[];dropped=[]
    for f in analysis['findings']:
        if f['kind'] in ('balance-change',) or f['tracks']==['__mix__']:continue
        if f['kind']=='masking':
            f['buildUp']=build_up(ctx,f)
            if f['buildUp'] and f['buildUp']['track']==f['dominant']:
                # the louder track built the clash up: that is the track to move (not the one it masks)
                f=dict(f,maskee=f['dominant'],dominant=f['maskee'],movedBuilder=True)
        cands=candidates(ctx,f)
        if not cands:dropped.append({'finding':f['id'],'why':'the measured overlap needs less than %.0f dB'%MIN_MOVE});continue
        tried=[]
        for mv in cands:
            try:
                ev=evaluate(ctx,f,mv);mv2=refine(mv,ev,f)
                if mv2 is not None:
                    ev2=evaluate(ctx,f,mv2)
                    if (ev2.get('benefit') or -9)>(ev.get('benefit') or -9):mv,ev=mv2,dict(ev2,refinedFrom=ev.get('after'))
            except Exception as exc:ev={'improved':False,'error':str(exc)[:200]}
            tried.append((mv,ev))
        good=[(mv,ev) for mv,ev in tried if ev.get('improved')]
        summary=[{'model':mv['model'],'target':mv['target'],'prediction':ev} for mv,ev in tried]
        if not good:
            dropped.append({'finding':f['id'],'why':'no candidate measurably reduced the problem offline','tried':summary});continue
        mv,ev=max(good,key=lambda x:x[1].get('benefit') or 0)
        pid=hashlib.sha1(json.dumps([f['id'],mv['model'],mv['points']],sort_keys=True,default=str).encode()).hexdigest()[:10]
        decision=[n for n in list(f.get('tracks') or ())+[f.get('maskee'),f.get('dominant'),mv.get('keyTrack')] if n and n!='__mix__']
        p={'id':'P'+pid,'finding':f['id'],'kind':f['kind'],'track':mv['track'],'trackId':ctx.track_ids.get(mv['track']),'trackName':ctx.live_names.get(mv['track'],mv['track']),'model':mv['model'],
           'decisionTrackIds':list(dict.fromkeys(ctx.track_ids[n] for n in decision if n in ctx.track_ids)),
           'target':mv['target'],'unit':mv['unit'],'neutral':mv['neutral'],'points':mv['points'],'bars':f['bars'],
           'startBeat':mv['points'][0]['beat'],'endBeat':mv['points'][-1]['beat'],'prediction':ev,'alternatives':summary,
           **{k:mv[k] for k in ('hz','q','slope','keyTrack','ratio','attackMs','releaseMs','share','response','channel','refined') if k in mv}}
        p['shape']=_shape_words(mv)
        p['explanation']=explain(ctx,f,p)
        proposals.append(p)
    return proposals,dropped

def refine(mv,ev,f):
    """One measured correction of a dB-depth move: scale its depth by wanted/achieved change (bounded), like a
    secant step. Returns the rescaled move, or None when it already reached the target or cannot be scaled."""
    if mv['model'] not in ('bell','gain','send') or ev.get('before') is None or ev.get('after') is None or f.get('movedBuilder'):return None
    if f['kind'] not in ('masking','low-end-clash'):return None
    want=ev['before']+MARGIN;got=ev['before']-ev['after']
    if got<=.1 or got>=.9*want:return None
    k=min(2.0,want/got);pts=[dict(p,value=r(float(np.clip(p['value']*k,-MAX_CUT,0)),3)) for p in mv['points']]
    if all(abs(a['value']-b['value'])<.05 for a,b in zip(pts,mv['points'])):return None
    return dict(mv,points=pts,refined={'scale':r(k,3),'basis':'first prediction moved the level %.2f dB of the %.2f dB wanted'%(got,want)})

def _shape_words(mv):
    vals=[p['value'] for p in mv['points']];extreme=max(vals,key=lambda v:abs(v-mv['neutral']))
    core=vals[1:-1]
    form='held' if len(set(core))<=1 else '%d-point curve following the measured problem'%len(core)
    return '%s at %s, one-beat ramps in and out'%(form,_fmt(extreme,mv['unit']) if mv['unit']!='internal' else '%.3g'%extreme)

def explain(ctx,f,p):
    ev=p['prediction'];t=p['target'];what={'bell':'EQ bell %s (Q %.2f) gain'%(_hz(p.get('hz',0)),p.get('q',0)),'high-pass':'EQ high-pass frequency','gain':'track volume',
         'pan':'track pan','send':'send from %s'%t.get('from'),'sidechain':'sidechain compressor threshold (keyed by %s)'%p.get('keyTrack'),'width':'Utility width',
         'bass-mono':'Utility bass mono below %.0f Hz'%p.get('hz',0),'invert':'Utility %s-channel polarity'%('right' if p.get('channel') else 'left'),'plugin':'%s (hosted plug-in parameter)'%t.get('name')}[p['model']]
    pred='Predicted offline: %s %s -> %s'%(ev['metric'],ev['before'],ev['after'])
    if ev.get('scoreBefore') is not None:pred+=', clash score %s -> %s (%.0f%% less)'%(ev['scoreBefore'],ev['scoreAfter'],100*(1-ev['scoreAfter']/max(ev['scoreBefore'],1e-9)))
    pred+='; %s loudness over those bars %+.1f LU, mix %+.1f LU'%(p['track'],(ev['maskeeLUFS'][1] or 0)-(ev['maskeeLUFS'][0] or 0),(ev['mixLUFS'][1] or 0)-(ev['mixLUFS'][0] or 0))
    if ev.get('maskeeOutsideBandDb') is not None:pred+=', %s outside that band %+.1f dB'%(p['track'],ev['maskeeOutsideBandDb'])
    if ev.get('maskeeWhereDominantSilentDb') is not None:pred+=', %s where %s is silent %+.1f dB'%(p['track'],p.get('keyTrack') or 'the other track',ev['maskeeWhereDominantSilentDb'])
    if ev.get('maskeeLowEndOutsideDominantBinsDb') is not None:pred+=', %s low end outside the shared frequencies %+.1f dB'%(p['track'],ev['maskeeLowEndOutsideDominantBinsDb'])
    if ev.get('sideAbove200HzDb') is not None:pred+=', %s stereo side above 200 Hz %+.1f dB'%(p['track'],ev['sideAbove200HzDb'])
    pred+='.'
    bu=f.get('buildUp') if f.get('movedBuilder') else None
    built=(' Measured build-up: %s rose %+.1f dB in the clash band against its own level elsewhere (%+.1f dB in its other bands) while %s changed %+.1f dB, so the move is on %s.'
           %(bu['track'],bu['riseDb'],bu['outOfBandRiseDb'],bu['other'],bu['otherRiseDb'],bu['track'])) if bu else ''
    return '%s%s Move: %s on %s, %s. %s'%(f['label'],built,what,p['track'],p['shape'],pred)

# ---------------------------------------------------------------- audition, workflow, execution
# The one A/B/Keep/Revert state is automation_actions' (stored in audition_pair's store). This module only turns
# a proposal into its plan, screens targets read-only, renders the offline A/B, and inserts/removes the neutral
# device a move needs (through mix_actions, exact undo) around automation_actions' audition and choice.
OFFERS='mix-automation-offers.json'      # offer id -> mix_actions record id of the device inserted for it
def _data():return Path(os.environ.get('SCRUNCH_DATA',str(ROOT/'data')))

def autonomous_mode():
    """automation_actions' persisted auto-keep flag (off by default); False when that module is unavailable."""
    try:
        import automation_actions as acts;return bool(acts.autonomous())
    except Exception:return False

def audition(ctx,proposal,out_dir,context_bars=1):
    """Offline A/B: the mix over the affected bars (plus one bar each side) as it is (A) and with the move (B).
    Not level matched (a level move must be heard as one); both loudnesses are reported."""
    out_dir=Path(out_dir);out_dir.mkdir(parents=True,exist_ok=True);bar=ctx.tb.bpb*60/ctx.tb.tempo
    t0=max(0,proposal['points'][0]['seconds']-context_bars*bar);t1=min(ctx.n/SR,proposal['points'][-1]['seconds']+context_bars*bar)
    s0,s1=int(t0*SR),int(t1*SR);m=proposal['track'];y=ctx.stems[m][s0:s1]
    key=ctx.stems[proposal['keyTrack']][s0:s1] if proposal.get('keyTrack') else None
    y2=render(proposal,y,t0,ctx,key)
    if proposal['model']=='send':y2=y+(y2-y)*proposal.get('share',1.0)
    A=ctx.mix[s0:s1];B=A-y+y2;peak=max(np.abs(A).max(),np.abs(B).max(),1e-9);trim=min(1.0,.89/peak)
    files={}
    for k,z in (('A',A),('B',B)):
        p=out_dir/('%s-%s.wav'%(proposal['id'],k));sf.write(str(p),z*trim,SR,subtype='FLOAT');files[k]=p.name
    return {'method':'offline render of processed stems (not a Live pass)','files':files,'startSeconds':r(t0),'endSeconds':r(t1),
            'bars':ctx.tb.span(t0,t1),'trimDb':r(20*math.log10(trim)),'lufs':{k:r(dsp.meter(z*trim,SR)['integratedLUFS']) for k,z in (('A',A),('B',B))}}

def _parse_db(display):
    import automation_plans;return automation_plans._db(display)

def _needs_device(p):return p['target']['kind'] in ('eq','compressor','utility')

def screen(proposals,adapter):
    """Read-only, before anything is proposed to the producer: a proposal whose target already carries automation
    (which Scrunch cannot put back exactly) is refused, as is one without a unique live track. Returns the refusals."""
    session={t['id']:t for t in adapter.session()['tracks']};refused=[]
    for p in proposals:
        tid=p.get('trackId');why=None
        if not tid or tid not in session:why='no live track id for %r'%p['track']
        elif session[tid].get('name')!=p.get('trackName',p['track']):why='track %s is named %r, not %r'%(tid,session[tid].get('name'),p.get('trackName',p['track']))
        elif not _needs_device(p):
            t=p['target']
            target={'mixer':'volume'} if t.get('control')=='volume' else {'mixer':'pan'} if t.get('control')=='pan' else \
                   {'mixer':'send','send':t['send']} if t.get('control')=='send' and 'send' in t else \
                   {'device':t['device'],'parameter':t['parameter']} if t['kind']=='device' else None
            if target is None:why='send index for return %r is not known'%t.get('return')
            else:
                info=adapter.automation_target(tid,target)
                if info.get('automation')!='none':why='exact revert not guaranteed: %r already carries automation (%s)'%(info.get('name'),info.get('automation'))
        if why:p['status']='refused';p['refusal']=why;refused.append({'proposal':p['id'],'why':why})
        else:p['status']='proposed'
    return refused

def to_automation_plan(proposal,adapter,track_id=None,track_name=None,device=None):
    """The proposal in automation_actions' plan shape (its stable interface): absolute values in a display unit or
    the host range, one lane, a reason on every point. Mixer moves are relative to the current fader/pan/send,
    read now. `device` = {'id','name','parameter'} of the device inserted for eq/compressor/utility moves."""
    tid=track_id or proposal.get('trackId');name=track_name or proposal.get('trackName') or proposal['track']
    if not tid:raise ValueError('The proposal has no live track id.')
    t=proposal['target'];pts=proposal['points'];rel=[float(q['value']) for q in pts]
    if t['kind']=='mixer':
        if t['control']=='volume':
            target={'mixer':'volume'};base=_parse_db(adapter.automation_target(tid,target).get('display'));unit='dB';vals=[max(-70.0,round(base+v,3)) for v in rel]
        elif t['control']=='pan':
            target={'mixer':'pan'};base=float(adapter.automation_target(tid,target)['value']);unit='';vals=[float(np.clip(base+v,-1,1)) for v in rel]
        elif t['control']=='send':
            if 'send' not in t:raise ValueError('Send index for return %r is not known.'%t.get('return'))
            target={'mixer':'send','send':int(t['send'])};base=_parse_db(adapter.automation_target(tid,target).get('display'));unit='dB';vals=[max(-70.0,round(base+v,3)) for v in rel]
        else:raise ValueError(t['control'])
    elif t['kind']=='device':
        target={'device':t['device'],'deviceName':t['deviceName'],'parameterId':t['parameter']};unit='';vals=rel
    else:
        if not device:raise ValueError('This move needs its device inserted first (insert_device).')
        target={'device':device['id'],'deviceName':device['name'],'parameter':device['parameter']}
        unit={'bandGainDb':'dB','thresholdDb':'dB','widthPercent':'%','bandHz':'Hz'}.get(t['control'],'')
        vals=[float(device['max'] if v>=.5 else device['min']) for v in rel] if unit=='' else rel
    lane={'target':target,'unit':unit,'points':[{'beat':float(q['beat']),'value':float(v),'reason':str(q['reason'])[:300]} for q,v in zip(pts,vals)]}
    return {'track':tid,'trackName':name,'lanes':[lane]}

def _neutral_action(p,track_id):
    """The device the move automates, inserted so it changes nothing until the curve moves it."""
    t=p['target']
    if t['kind']=='eq' and t['shape']=='bell':return {'kind':'eq','track':track_id,'shape':'bell','hz':t['hz'],'db':0.0,'q':t['q'],'label':p['id']}
    if t['kind']=='eq' and t['shape']=='high-pass':return {'kind':'eq','track':track_id,'shape':'high-pass','hz':p['neutral'],'slope':t.get('slope',48),'label':p['id']}
    if t['kind']=='compressor':return {'kind':'compressor','track':track_id,'thresholdDb':p['neutral'],'ratio':t['ratio'],'attackMs':t['attackMs'],'releaseMs':t['releaseMs'],
                                       'sidechain':{'track':t['sidechainId'],'tap':'Post Mixer'},'label':p['id']}
    if t['kind']=='utility':return {'kind':'utility','track':track_id,'widthPercent':100.0,'label':p['id']}
    raise ValueError('No device for %r'%t)

def _bound_control(rec,t):
    """The parameter of the inserted device that the curve moves, read from the verified insert record.
    Live 12 control names (EQ Eight via eq_eight, Compressor, Utility) are the only DAW names in this module."""
    ctrls={c['name']:c for d in rec['after']['devices'] if d['id']==rec['device'] for c in d['controls']}
    names={'bandGainDb':'db','bandHz':'hz'}.get(t['control'])
    if names:
        import eq_eight
        band=next(s['band'] for s in rec['stages'] if 'band' in s);n=eq_eight.band_names(band)[names]
    else:
        n={'thresholdDb':'Threshold','widthPercent':next((k for k in ('Stereo Width','Width') if k in ctrls),'Width'),'bassMono':'Bass Mono',
           'invertLeft':next((k for k in ('Left Inv','Phz-L') if k in ctrls),'Left Inv'),'invertRight':next((k for k in ('Right Inv','Phz-R') if k in ctrls),'Right Inv')}[t['control']]
    if n not in ctrls:raise ValueError('The inserted device has no %r control.'%n)
    return ctrls[n]

def insert_device(proposal,host_mix,track_id):
    """Insert the neutral device through mix_actions (verified, exact undo). Returns (device for the plan, record)."""
    import mix_actions
    rec=mix_actions.apply(_neutral_action(proposal,track_id),host_mix,use='mix-automation')
    c=_bound_control(rec,proposal['target']);dev=next(d for d in rec['after']['devices'] if d['id']==rec['device'])
    return {'id':rec['device'],'name':dev['name'],'parameter':c['name'],'parameterId':c['id'],'min':c.get('min'),'max':c.get('max')},rec

def _save_offers(m):
    p=_data()/OFFERS
    if m:p.write_text(json.dumps(m))
    elif p.exists():p.unlink()

def _offers():
    try:return json.loads((_data()/OFFERS).read_text())
    except (OSError,ValueError):return {}

def audition_live(proposal,adapter,*,host_mix=None,request_id='',track_id=None,track_name=None):
    """Real-time Live pass through automation_actions.audition (records the candidate, reads it back, loops the
    span; nothing is kept until he chooses unless autonomous mode is on). A device the move needs is inserted
    first and taken out again if the audition fails; it stays with the offer until Keep, Revert or Neither."""
    import automation_actions as acts
    tid=track_id or proposal.get('trackId');dev=rec=None
    if _needs_device(proposal):
        if host_mix is None:
            import daw_mix;host_mix=daw_mix.LiveMix(adapter)
        acts.refuse_if_busy(adapter)
        dev,rec=insert_device(proposal,host_mix,tid)
    try:
        out=acts.audition(to_automation_plan(proposal,adapter,tid,track_name,dev),adapter,request_id=request_id)
    except Exception:
        if rec:
            import mix_actions;mix_actions.undo(rec,host_mix)
        raise
    if rec and out.get('failed'):
        import mix_actions;out['deviceRemoved']=mix_actions.undo(rec,host_mix)
    elif rec:
        m=_offers();m[out['offerId']]=rec['id'];p=_data()/OFFERS;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(m))
    out['audition']='real-time Live pass (automation_actions): the candidate is recorded, read back and looped'
    return out

def choose(choice,adapter,*,offer_id=None,host_mix=None):
    """automation_actions.choose, plus taking out the device inserted for this offer on Revert / Neither (exact)."""
    import automation_actions as acts
    oid=offer_id or (acts.offer() or {}).get('offerId')      # read before the choice closes the offer
    out=acts.choose(choice,adapter,offer_id);m=_offers()
    if out.get('decided') in ('reverted','neither','revert') or (choice in ('revert','neither') and out.get('verified')):
        rid=m.pop(oid,None) if oid else None
        if rid:
            import mix_actions
            if host_mix is None:
                import daw_mix;host_mix=daw_mix.LiveMix(adapter)
            out['deviceRemoved']=mix_actions.undo(mix_actions.load(rid),host_mix)
            _save_offers(m)
    elif choice=='keep' and oid in m:
        m.pop(oid);_save_offers(m)
    return out

def remeasure(ctx,finding,stems_after):
    """The finding's own numbers measured on another capture of the same bars (same time base)."""
    c2=Context(stems_after,ctx.tb,ctx.returns,ctx.track_ids,rest=False);h0,h1=finding['hops'];span=np.arange(h0,min(h1+1,c2.H));k=finding['kind']
    if k=='masking':
        ks=np.arange(finding['bands'][0],finding['bands'][1]+1);m,d=finding['maskee'],finding['dominant']
        Lm,Ld=db(c2.f[m]['fine']),db(c2.f[d]['fine'])
        return {'maskeeToDominantDb':r(float(np.mean((Lm-Ld)[np.ix_(span,ks)]))),'score':r(masking_score(Lm,Ld,span,ks))}
    if k=='low-end-clash':
        x,s_,_=lf_ratio(c2.f[finding['maskee']]['lf'],c2.f[finding['dominant']]['lf'],lf_frames(c2.f[finding['maskee']],h0,h1));return {'maskeeToDominantDb':r(x),'score':r(s_)}
    f=c2.f[finding['maskee']]
    if k=='level-drift':
        bal=smooth_level(f['total'])-smooth_level(c2.rest_power(finding['maskee']))
        return {'balanceErrorDb':r(float(np.nanmean(np.abs(bal[span]-finding['numbers']['sectionBalanceDb']))))}
    if k=='low-end-phase':return {'lowCorrelation':r(float(np.nanmean(f['lowCorr'][span])))}
    if k=='width-collapse':return {'width':r(float(np.nanmean(f['width'][span])),3)}
    return {}

# ---------------------------------------------------------------- stem path (status)
STEM_PATHS={
 'folder':'PROVEN offline: a folder of exported stems (one file per track, same start) plus the tempo/bar map read from Live (timebase_from_live, read-only).',
 'post-mixer-tap':'CANDIDATE: daw_mix.capture_track (muted, unarmed, Sends Only capture track with the Live Ears tap, Post Mixer input). Per-track level proven 13 Sep; several taps on one common clock and their placement on Arrangement beats are UNVERIFIED.',
 'export-rendered-track':'CANDIDATE: Live Export Audio with one rendered track (native/capture_export, Accessibility). Single-track export helper exists; per-track loop not re-proven.',
 'resample-record':'NOT USED: recording a routed audio track needs an armed track, which the test rules forbid.'}

# ---------------------------------------------------------------- run + report
def run(ctx,out_dir=None,audition_dir=None,name='mix',adapter=None):
    t=time.time();an=analyse(ctx);t1=time.time();props,dropped=plan(ctx,an);t2=time.time()
    refused=screen(props,adapter) if adapter is not None else None
    auds={}
    if audition_dir:
        for p in props:auds[p['id']]=audition(ctx,p,audition_dir)
    for p in props:
        if p['id'] in auds:p['audition']=auds[p['id']]
        p['choices']=['Original','Candidate','A','B','Keep','Revert','Neither'];p.setdefault('status','proposed')
    res={'version':VERSION,'dspVersion':dsp.VERSION,'name':name,'durationSeconds':r(ctx.n/SR),'timebase':ctx.tb.public(),'tracks':ctx.names,
         'analysis':an,'proposals':props,'dropped':dropped,'refused':refused,'screenedAgainstLive':adapter is not None,'autonomousMode':autonomous_mode(),'stemPaths':STEM_PATHS,
         'timings':{'analyseSeconds':r(t1-t),'planSeconds':r(t2-t1)},'timeline':_timeline(ctx),
         'method':{'hop':'0.25 s hops, 0.5 s windows (8192-point FFT, 1500-sample step)','bands':'reference_dsp 11 bands for stereo measures; 1/3-octave grid 25 Hz-16 kHz for locating overlaps',
                   'loudness':'libebur128 momentary per hop (reference_dsp.meter_blocks)','importance':'power-mean momentary loudness over the span + 10log10(fraction active); active = within %d LU of the mix'%ACTIVE_BELOW_MIX,
                   'masking':'cells (hop x 1/3 octave >=150 Hz) where both tracks are within %g dB of each other, within %g dB of their own strongest cell, within %g dB of the mix, and together >= %d%% of the mix'%(CLOSE,SIG,CONTRIB,PAIRSHARE*100),
                   'lowEnd':'30-150 Hz, per FFT frame and bin: one track\'s energy where the other sounds (frames within 12 dB of its loudest, bins within 12 dB of its strongest); clash when within %g dB; the track encroaching more on the other\'s moments is the one moved (section importance only breaks ties within 1 dB)'%CLOSE,
                   'drift':'balance = track power minus power of the rest of the mix (1 s centred power mean), against its own section median; >= %g dB for 2 s; a move only when the track itself moved >= %g dB the same way'%(DRIFT,DRIFT_OWN),
                   'stereo':'low end 20-200 Hz correlation < 0 or side share >= 0.2; width = side share above 200 Hz, collapse at robust z <= -4 and half the median',
                   'sections':'novelty = distance between one-bar means of the mix spectral shape (per 10 dB) and track activity (x3 per track entering or leaving); peaks snapped to clip edges or bar lines',
                   'prediction':'each candidate rendered on the stem (RBJ bell / Butterworth high-pass with block-wise automation, peak compressor, balance pan, M/S Utility, measured plug-in response) and the same DSP re-run; kept when the clash score falls >= %d%% and the level ratio >= %g dB (drift: error halved)'%(IMPROVE*100,IMPROVE_DB),
                   'choice':'among improving candidates, the one with the largest fraction of the problem removed minus %.2f per dB of collateral change (masking: the track outside the clash band; low end: the track where the other is silent and in 30-150 Hz bins the other does not occupy; low-end phase: the track\'s stereo side above 200 Hz; otherwise the track loudness); dB-depth moves get one measured depth correction'%COLLATERAL,
                   'workflow':'proposal (offline, with A/B renders of the processed stems) -> screened against Live (a target already carrying automation is refused: exact revert not guaranteed) -> automation_actions audition (real-time Live pass: recorded, read back, looped) -> Original / Candidate / A / B, then Keep / Revert / Neither by the producer; auto-keep only in automation_actions\' persisted autonomous mode (off by default)'}}
    if out_dir:
        out=Path(out_dir);out.mkdir(parents=True,exist_ok=True)
        (out/('%s.json'%name)).write_text(json.dumps(res,indent=1,default=_json));(out/('%s.html'%name)).write_text(report(res))
    return res


def _json(o):
    if isinstance(o,(np.floating,np.integer)):return o.item()
    if isinstance(o,np.ndarray):return o.tolist()
    return str(o)

def _timeline(ctx):
    ml=ctx.fm['lufs'];out={'hop':HOP,'count':ctx.H,'mixLUFS':[r(x,1) for x in ml],'tracks':{}}
    for k in ctx.names:
        l=ctx.f[k]['lufs'];out['tracks'][k]={'lufs':[r(x,1) for x in l],'active':ctx.active[k].astype(int).tolist()}
    return out

def report(res):
    data=json.dumps(res,allow_nan=False,separators=(',',':'),default=_json).replace('</','<\\/')
    return PAGE.replace('__TITLE__',html.escape('Mix Automation · '+res['name'])).replace('__DATA__',data)

PAGE=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
:root{color-scheme:light;--bg:#fcfcfb;--panel:#f4f3f0;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--grid:#e3e2de;--m:#2a78d6;--s:#104281;--bad:#d03b3b;--warn:#ec835a;--ok:#2f8a4e;--curve:#7a3fd0;--sec:rgba(42,120,214,.07);--sel:rgba(42,120,214,.14)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1a1a19;--panel:#242422;--ink:#fff;--ink2:#c3c2b7;--muted:#8f8e86;--grid:#33332f;--m:#3987e5;--s:#9ec5f4;--bad:#e66767;--ok:#5cc07f;--curve:#b48cf2;--sec:rgba(57,135,229,.10);--sel:rgba(57,135,229,.2)}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--panel:#242422;--ink:#fff;--ink2:#c3c2b7;--muted:#8f8e86;--grid:#33332f;--m:#3987e5;--s:#9ec5f4;--bad:#e66767;--ok:#5cc07f;--curve:#b48cf2;--sec:rgba(57,135,229,.10);--sel:rgba(57,135,229,.2)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
main{max-width:1400px;margin:0 auto;padding:20px 16px 60px}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.sub{color:var(--ink2)}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.stat{background:var(--panel);border-radius:8px;padding:8px 12px}.stat b{display:block;font-size:17px}.stat span{color:var(--ink2);font-size:12px}
.banner{border-left:4px solid var(--warn);background:var(--panel);padding:8px 12px;margin:10px 0;border-radius:4px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--ink2);font-size:12px;margin:6px 0}.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:4px}
#scroll{overflow-x:auto;border:1px solid var(--grid);border-radius:8px}canvas{display:block}
#tip{position:fixed;pointer-events:none;background:var(--panel);border:1px solid var(--grid);border-radius:6px;padding:6px 8px;font-size:12px;display:none;z-index:5;max-width:360px}
#detail{background:var(--panel);border-radius:8px;padding:12px 14px;margin-top:12px;min-height:60px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}td,th{text-align:left;padding:3px 8px;border-bottom:1px solid var(--grid);vertical-align:top}th{color:var(--ink2);font-weight:500}td.n{text-align:right;font-variant-numeric:tabular-nums}
.item{padding:8px 10px;border-radius:6px;cursor:pointer;border:1px solid transparent}.item:hover{border-color:var(--grid)}.tag{font-size:11px;border-radius:4px;padding:1px 6px;margin-right:6px;color:#fff}
.tp{background:var(--curve)}.tf{background:var(--bad)}.td{background:var(--muted)}.muted{color:var(--muted)}.btns span{display:inline-block;border:1px solid var(--grid);border-radius:6px;padding:2px 9px;margin-right:4px;font-size:12px}
</style></head><body><main>
<h1 id="title"></h1><div class="sub" id="sub"></div><div class="stats" id="stats"></div><div id="banners"></div>
<div class="legend"><span><span class="sw" style="background:var(--sec)"></span>Sections</span><span><span class="sw" style="background:var(--m)"></span>Track loudness vs mix</span><span><span class="sw" style="background:var(--bad)"></span>Measured clash / problem</span><span><span class="sw" style="background:var(--curve)"></span>Planned automation curve (its own units)</span></div>
<div id="scroll"><canvas id="c"></canvas></div><div id="tip"></div>
<div id="detail"><span class="muted">Click a curve, a clash marker or a proposal to see why, with the before/after numbers.</span></div>
<h2>Proposals</h2><div id="props"></div><h2>Dropped moves</h2><div id="drop"></div><h2>Findings</h2><div id="finds"></div>
<h2>Sections and balance</h2><div id="secs"></div><h2>Stem path</h2><div id="stem" class="muted"></div><h2>Method</h2><div id="method" class="muted"></div>
</main><script id="data" type="application/json">__DATA__</script><script>
const R=JSON.parse(document.getElementById('data').textContent),T=R.timeline,H=T.hop,D=R.durationSeconds,TB=R.timebase,A=R.analysis;
const $=id=>document.getElementById(id),esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const clock=s=>Math.floor(s/60)+':'+(s%60).toFixed(1).padStart(4,'0'),bar=t=>(TB.startBeat+t*TB.tempo/60)/TB.beatsPerBar+1;
$('title').textContent='Mix Automation · '+R.name;
$('sub').textContent=`${clock(D)} · ${R.tracks.length} tracks · ${TB.tempo} BPM, ${TB.beatsPerBar} beats per bar (${TB.source}) · 0.25 s hops`;
$('stats').innerHTML=[['Sections',A.sections.length],['Findings',A.findings.length],['Proposals',R.proposals.length],['Dropped',R.dropped.length],['Autonomous mode',R.autonomousMode?'ON':'off']].map(([a,b])=>`<div class="stat"><b>${esc(b)}</b><span>${a}</span></div>`).join('');
$('banners').innerHTML=`<div class="banner"><b>Nothing here is written to Live.</b> Each proposal comes with an offline A/B render of the processed stems; in Live it is auditioned by automation_actions (recorded, read back, looped), then Original / Candidate / A / B and Keep / Revert / Neither are the producer's choice. Measured improvement is evidence, not permission. Autonomous mode (auto-keep): <b>${R.autonomousMode?'ON':'off'}</b>. ${R.screenedAgainstLive?(R.refused.length?R.refused.length+' refused against the open Set (exact revert not guaranteed or no live track).':'Screened against the open Set: every target is free of automation.'):'Not yet screened against the open Set.'}</div>`;
const pairs=[...new Set(A.findings.filter(f=>f.tracks.length==2).map(f=>f.tracks.join(' × ')))];
const LAB=170,TH=46,PH=22;let hits=[];const cv=$('c'),cx=cv.getContext('2d');
function lay(){let y=0;const o={axis:[y,24]};y+=26;o.sec=[y,20];y+=24;R.tracks.forEach(t=>{o['t:'+t]=[y,TH];y+=TH+6});pairs.forEach(p=>{o['p:'+p]=[y,PH];y+=PH+4});return[o,y+4]}
function draw(){const dpr=devicePixelRatio||1,w=Math.max($('scroll').clientWidth,900),[L,hgt]=lay(),x=t=>LAB+t/D*(w-LAB);hits=[];
 cv.width=w*dpr;cv.height=hgt*dpr;cv.style.width=w+'px';cv.style.height=hgt+'px';cx.setTransform(dpr,0,0,dpr,0,0);cx.clearRect(0,0,w,hgt);cx.font='12px -apple-system,sans-serif';cx.textBaseline='middle';
 const bl=TB.beatsPerBar*60/TB.tempo,step=[1,2,4,8,16,32].find(s=>s*bl/D*(w-LAB)>=40)||64;
 for(let b=1;;b+=step){const t=(b-1)*bl-TB.startBeat*60/TB.tempo;if(t>D)break;if(t<0)continue;cx.strokeStyle=css('--grid');cx.beginPath();cx.moveTo(x(t)+.5,20);cx.lineTo(x(t)+.5,hgt);cx.stroke();cx.fillStyle=css('--muted');cx.fillText(b,x(t)+3,10)}
 cx.fillStyle=css('--ink2');cx.fillText('Bar',6,10);
 A.sections.forEach((s,i)=>{const[y,h]=L.sec;cx.fillStyle=i%2?css('--sec'):'transparent';cx.fillRect(x(s.startSeconds),y,x(s.endSeconds)-x(s.startSeconds),hgt-y);cx.fillStyle=css('--ink2');cx.fillText('S'+s.index+' '+s.bars,x(s.startSeconds)+4,y+10)});
 cx.fillStyle=css('--ink2');cx.fillText('Sections',6,L.sec[0]+10);
 R.tracks.forEach(t=>{const[y,h]=L['t:'+t],tr=T.tracks[t];cx.fillStyle=css('--ink');cx.fillText(t,6,y+12);cx.fillStyle=css('--muted');cx.font='11px -apple-system,sans-serif';cx.fillText('loudness vs mix',6,y+28);cx.font='12px -apple-system,sans-serif';
  cx.strokeStyle=css('--grid');cx.strokeRect(LAB,y,w-LAB,h);cx.strokeStyle=css('--m');cx.lineWidth=1.3;cx.beginPath();let pen=false;
  for(let i=0;i<T.count;i++){const v=tr.lufs[i],m=T.mixLUFS[i];if(v==null||m==null||!tr.active[i]){pen=false;continue}const yy=y+h-Math.max(0,Math.min(1,(v-m+30)/30))*h;pen?cx.lineTo(x(i*H),yy):cx.moveTo(x(i*H),yy);pen=true}cx.stroke();cx.lineWidth=1;
  A.findings.filter(f=>f.tracks.length==1&&f.tracks[0]==t).forEach(f=>{cx.fillStyle=css(f.kind=='balance-change'?'--muted':'--bad');cx.globalAlpha=.35;cx.fillRect(x(f.startSeconds),y+h-6,Math.max(3,x(f.endSeconds)-x(f.startSeconds)),6);cx.globalAlpha=1;hits.push({x0:x(f.startSeconds),x1:x(f.endSeconds),y0:y+h-8,y1:y+h,f})});
  R.proposals.filter(p=>p.track==t).forEach(p=>{const vs=p.points.map(q=>q.value),lo=Math.min(...vs,p.neutral),hi=Math.max(...vs,p.neutral),sp=hi-lo||1;cx.strokeStyle=css('--curve');cx.lineWidth=2;cx.beginPath();
   p.points.forEach((q,i)=>{const xx=x(q.seconds),yy=y+4+(hi-q.value)/sp*(h-8);i?cx.lineTo(xx,yy):cx.moveTo(xx,yy)});cx.stroke();cx.lineWidth=1;const a=p.points[0].seconds,b=p.points[p.points.length-1].seconds;hits.push({x0:x(a),x1:x(b),y0:y,y1:y+h-8,p})})});
 pairs.forEach(pr=>{const[y,h]=L['p:'+pr];cx.fillStyle=css('--ink2');cx.fillText(pr,6,y+h/2);A.findings.filter(f=>f.tracks.join(' × ')==pr).forEach(f=>{cx.fillStyle=css(f.kind=='low-end-clash'?'--warn':'--bad');cx.fillRect(x(f.startSeconds),y+3,Math.max(3,x(f.endSeconds)-x(f.startSeconds)),h-6);hits.push({x0:x(f.startSeconds),x1:x(f.endSeconds),y0:y,y1:y+h,f})})});
}
function at(e){const r=cv.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top]}
function hit(x,y){return hits.find(h=>x>=h.x0-2&&x<=h.x1+2&&y>=h.y0&&y<=h.y1)}
cv.onmousemove=e=>{const[x,y]=at(e),h=hit(x,y),tip=$('tip');if(!h){tip.style.display='none';return}tip.innerHTML=h.p?`<b>${h.p.id}</b> ${esc(h.p.track)} · ${esc(h.p.model)} · ${esc(h.p.bars)}`:`<b>${h.f.id}</b> ${esc(h.f.label)}`;tip.style.display='block';tip.style.left=Math.min(innerWidth-370,e.clientX+14)+'px';tip.style.top=(e.clientY+14)+'px'};
cv.onmouseleave=()=>$('tip').style.display='none';cv.onclick=e=>{const[x,y]=at(e),h=hit(x,y);if(h)h.p?showP(h.p):showF(h.f)};
const unit=(p,v)=>p.unit=='dB'?(v>0?'+':'')+v.toFixed(1)+' dB':p.unit=='Hz'?v.toFixed(0)+' Hz':p.unit=='%'?v.toFixed(0)+'%':p.unit=='pan'?(v>0?'+':'')+v.toFixed(2):String(v);
function pred(ev){if(!ev)return'';const rows=[['Measure',ev.metric],['Before → after',`${ev.before} → ${ev.after}`]];if(ev.scoreBefore!=null)rows.push(['Clash score',`${ev.scoreBefore} → ${ev.scoreAfter}`]);rows.push(['Track loudness over the bars (LUFS)',`${ev.maskeeLUFS[0]} → ${ev.maskeeLUFS[1]}`],['Mix loudness over the bars (LUFS)',`${ev.mixLUFS[0]} → ${ev.mixLUFS[1]}`],['Improved',ev.improved?'yes':'no'],['Basis',ev.basis||'']);return`<table>${rows.map(([a,b])=>`<tr><th>${esc(a)}</th><td>${esc(b)}</td></tr>`).join('')}</table>`}
function showP(p){const au=p.audition?`<p>Audition (${esc(p.audition.method)}): A = <code>${esc(p.audition.files.A)}</code>, B = <code>${esc(p.audition.files.B)}</code>, ${esc(p.audition.bars)}, LUFS A ${p.audition.lufs.A} / B ${p.audition.lufs.B}.</p>`:'';
 $('detail').innerHTML=`<b><span class="tag tp">${p.id}</span>${esc(p.bars)} · ${esc(p.track)} · ${esc(p.model)}</b><p>${esc(p.explanation)}</p>${au}<div class="btns">Choice (producer): ${p.choices.map(c=>`<span>${c}</span>`).join('')} <span class="muted">status: ${esc(p.status)}</span></div>${pred(p.prediction)}
 <table><tr><th>Bar.beat</th><th>Value</th><th>Reason</th></tr>${p.points.map(q=>`<tr><td class="n">${(q.beat/TB.beatsPerBar+1).toFixed(2)}</td><td class="n">${unit(p,q.value)}</td><td>${esc(q.reason)}</td></tr>`).join('')}</table>
 <p class="muted">Other candidates measured: ${p.alternatives.map(a=>`${esc(a.model)} (${a.prediction.improved?'improves':'no'}, benefit ${a.prediction.benefit})`).join('; ')}</p>`}
function showF(f){const p=R.proposals.find(p=>p.finding==f.id),d=R.dropped.find(d=>d.finding==f.id);
 $('detail').innerHTML=`<b><span class="tag tf">${f.id}</span>${esc(f.kind)}</b><p>${esc(f.label)}</p><table>${Object.entries(f.numbers||{}).map(([k,v])=>`<tr><th>${esc(k)}</th><td>${esc(typeof v=='object'?JSON.stringify(v):v)}</td></tr>`).join('')}</table>${f.attribution?`<p>Leave-one-out: ${f.attribution.slice(0,4).map(a=>esc(a.track)+' '+(a.change>0?'+':'')+a.change).join(', ')}</p>`:''}${p?`<p>Proposal <a href="#" data-p="${p.id}">${p.id}</a>.</p>`:''}${d?`<p class="muted">No move: ${esc(d.why)}</p>`:''}`}
$('detail').onclick=e=>{const id=e.target.dataset&&e.target.dataset.p;if(id){e.preventDefault();showP(R.proposals.find(p=>p.id==id))}};
$('props').innerHTML=R.proposals.length?R.proposals.map(p=>`<div class="item" data-p="${p.id}"><span class="tag tp">${p.id}</span>${esc(p.explanation)}</div>`).join(''):'<p class="muted">No move measurably improved any finding.</p>';
$('props').onclick=e=>{const el=e.target.closest('[data-p]');if(el)showP(R.proposals.find(p=>p.id==el.dataset.p))};
$('drop').innerHTML=R.dropped.length?R.dropped.map(d=>`<div class="item muted"><span class="tag td">${d.finding}</span>${esc(d.why)}${d.tried?': '+d.tried.map(t=>`${esc(t.model)} ${t.prediction.before}→${t.prediction.after}`).join('; '):''}</div>`).join(''):'<p class="muted">None.</p>';
$('finds').innerHTML=A.findings.map(f=>`<div class="item" data-f="${f.id}"><span class="tag tf">${f.id}</span>${esc(f.label)}</div>`).join('')+A.sectionChanges.map(c=>`<div class="item muted">${esc(c.label)}</div>`).join('');
$('finds').onclick=e=>{const el=e.target.closest('[data-f]');if(el)showF(A.findings.find(f=>f.id==el.dataset.f))};
$('secs').innerHTML=`<table><tr><th>Section</th><th>Bars</th><th>Mix LUFS</th>${R.tracks.map(t=>`<th>${esc(t)} vs mix</th>`).join('')}</tr>${A.sectionBalance.map(s=>`<tr><td>S${s.section}</td><td>${esc(s.bars)}</td><td class="n">${s.mixLUFS}</td>${R.tracks.map(t=>`<td class="n">${s.tracks[t]==null?'–':s.tracks[t].toFixed(1)}</td>`).join('')}</tr>`).join('')}</table>`;
$('stem').innerHTML=Object.entries(R.stemPaths).map(([k,v])=>`<div><b>${esc(k)}</b>: ${esc(v)}</div>`).join('');
$('method').innerHTML=Object.entries(R.method).map(([k,v])=>`<div><b>${esc(k)}</b>: ${esc(v)}</div>`).join('')+`<div>${esc(R.version)} / ${esc(R.dspVersion)} · analysed in ${R.timings.analyseSeconds} s, planned in ${R.timings.planSeconds} s</div>`;
addEventListener('resize',draw);draw();
</script></body></html>'''

def main():
    ap=argparse.ArgumentParser(description='Mix automation: track-vs-track analysis and automation proposals from stems.')
    ap.add_argument('stems');ap.add_argument('--tempo',type=float);ap.add_argument('--beats-per-bar',type=float,default=4);ap.add_argument('--start-beat',type=float,default=0)
    ap.add_argument('--live-timebase',action='store_true',help='read tempo, bar length and clip edges from the open Set (read-only)')
    ap.add_argument('--out');ap.add_argument('--audition',action='store_true')
    a=ap.parse_args();stems,meta=load_stems(a.stems)
    if a.live_timebase:
        import daw_adapter;tb=timebase_from_live(daw_adapter.select_backend(),a.start_beat)
    else:tb=TimeBase(a.tempo or meta.get('tempo') or 120,meta.get('beatsPerBar',a.beats_per_bar),meta.get('startBeat',a.start_beat),source='given' if a.tempo else 'arrangement.json' if meta.get('tempo') else 'default 120 BPM')
    ctx=Context(stems,tb,meta.get('returns'),meta.get('trackIds'),meta.get('responses'))
    out=Path(a.out) if a.out else ROOT/'data'/'mix-automation'/('%s-%s'%(Path(a.stems).name,datetime.datetime.now().strftime('%Y%m%d-%H%M%S')))
    res=run(ctx,out,out/'audition' if a.audition else None,Path(a.stems).name)
    print('%d sections, %d findings, %d proposals, %d dropped. Report: %s'%(len(res['analysis']['sections']),len(res['analysis']['findings']),len(res['proposals']),len(res['dropped']),out/(Path(a.stems).name+'.html')))

if __name__=='__main__':main()
