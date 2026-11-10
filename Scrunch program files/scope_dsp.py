"""What the scope draws, and what it can honestly say about how two tracks sit together.

The drawing half is an envelope per lane: for every pixel column, the lowest and highest sample
it covers. Peaks are kept with their polarity, so a transient is a transient and not an average
of one - which is the whole point of looking at a kick against a bass.

The listening half answers the questions a mix actually turns on:

  * **Do they fight for the same frequencies?** Overlap of the two spectra where both are loud.
  * **Are they in phase where it matters?** Correlation per band, and especially below 150 Hz,
    which is what survives a mono fold and what cuts to vinyl.
  * **Do their transients land together?** The lag, in milliseconds, at which the two envelopes
    correlate best - a kick and a bass an eighth of a millisecond apart are not a phase problem,
    twelve milliseconds apart is.

Every answer says how loud the audio was when it was taken, because none of it means anything
about near-silence, and a number computed from silence is a lie with a decimal point.
"""
import numpy as np

FLOOR_DB=-60.0           # below this a lane is treated as not playing
LOW_SPLIT=150.0          # the mono-compatibility band everyone argues about
BANDS=[('sub',20,60),('low',60,150),('low mids',150,500),('mids',500,2000),
       ('high mids',2000,6000),('highs',6000,12000),('air',12000,20000)]
MAX_LAG_MS=30.0
CONFIDENT=0.3           # below this, a best-fit lag is noise rather than a result
HANN_ENBW=1.5           # a Hann window's equivalent noise bandwidth, in bins

def db(x):
    x=float(x)
    return float(20*np.log10(x)) if x>1e-12 else FLOOR_DB

def envelope(audio,columns):
    """(min, max, rms) per drawn column, keeping polarity."""
    audio=np.asarray(audio,dtype=np.float32)
    if audio.ndim==2:mono=audio.mean(axis=1)
    else:mono=audio
    columns=int(max(1,columns))
    if len(mono)<columns:
        pad=np.zeros(columns-len(mono),dtype=np.float32);mono=np.concatenate((pad,mono))
    # Vectorised on purpose: a per-column Python loop across several lanes at 30 frames a second
    # is more arithmetic than the interpreter can do in the time between frames.
    edges=np.linspace(0,len(mono),columns+1).astype(int)
    starts=np.minimum(edges[:-1],len(mono)-1)
    lo=np.minimum.reduceat(mono,starts)
    hi=np.maximum.reduceat(mono,starts)
    power=np.add.reduceat(mono.astype(np.float64)**2,starts)
    counts=np.maximum(1,np.diff(edges))
    rms=np.sqrt(power/counts)
    empty=np.diff(edges)<=0                       # a column narrower than one sample repeats it
    if empty.any():
        lo[empty]=mono[starts[empty]];hi[empty]=mono[starts[empty]];rms[empty]=np.abs(mono[starts[empty]])
    return lo.astype(np.float32),hi.astype(np.float32),rms.astype(np.float32)

def level(audio):
    """Peak and RMS of a lane, in dBFS, plus its stereo correlation."""
    audio=np.asarray(audio,dtype=np.float64)
    if audio.size==0:return {'peakDbFS':FLOOR_DB,'rmsDbFS':FLOOR_DB,'correlation':None,'playing':False}
    if audio.ndim==1:audio=audio[:,None]
    peak=float(np.max(np.abs(audio)));rms=float(np.sqrt(np.mean(audio*audio)))
    corr=None
    if audio.shape[1]>=2:
        l,r=audio[:,0],audio[:,1]
        energy=np.sqrt(np.sum(l*l)*np.sum(r*r))
        if energy>1e-12:corr=float(np.clip(np.sum(l*r)/energy,-1,1))
    return {'peakDbFS':db(peak),'rmsDbFS':db(rms),'correlation':corr,'playing':db(rms)>FLOOR_DB}

MAX_FFT=65536            # ~1.4 s at 48 kHz: long enough to span a bar-ish, so a kick that hits
                         # once a beat is actually inside the window being judged

def _spectrum(mono,rate):
    """The newest audio as a spectrum, scaled so a band's level is a real dBFS figure.

    Without the window's own gain divided out, band levels come back as large positive numbers -
    internally comparable, but nonsense as dBFS, and they would put a threshold like 'both louder
    than -40 dB' in the wrong place. Each bin is scaled to the amplitude it represents.
    """
    n=int(2**np.floor(np.log2(max(256,min(len(mono),MAX_FFT)))))
    window=np.hanning(n)
    chunk=np.asarray(mono[-n:],dtype=np.float64)*window
    spec=np.fft.rfft(chunk)*(2.0/np.sum(window))
    freqs=np.fft.rfftfreq(n,1.0/float(rate))
    return freqs,spec

def spectrum(audio,rate,bands=BANDS):
    """Energy per band, in dB, from the newest audio in the lane."""
    audio=np.asarray(audio,dtype=np.float64)
    mono=audio.mean(axis=1) if audio.ndim==2 else audio
    if len(mono)<256:return []
    freqs,spec=_spectrum(mono,rate)
    power=np.abs(spec)**2
    out=[]
    for name,lo,hi in bands:
        mask=(freqs>=lo)&(freqs<hi)
        # Amplitude bins to an RMS level for the band: what a meter would show for that range.
        # A window spreads a tone across neighbouring bins, so the summed power is its noise
        # bandwidth times the real thing - 1.5 for a Hann window - and that is divided back out.
        out.append({'name':name,'lowHz':lo,'highHz':hi,
                    'energyDbFS':db(np.sqrt(power[mask].sum()/(2.0*HANN_ENBW))) if mask.any() else FLOOR_DB})
    return out

def correlation_by_band(left_audio,right_audio,rate,bands=BANDS):
    """How two signals line up in phase, band by band: +1 together, 0 unrelated, -1 fighting."""
    a=np.asarray(left_audio,dtype=np.float64);b=np.asarray(right_audio,dtype=np.float64)
    a=a.mean(axis=1) if a.ndim==2 else a;b=b.mean(axis=1) if b.ndim==2 else b
    n=min(len(a),len(b))
    if n<256:return []
    a,b=a[-n:],b[-n:]
    fa,sa=_spectrum(a,rate);fb,sb=_spectrum(b,rate)
    size=min(len(sa),len(sb));sa,sb,freqs=sa[:size],sb[:size],fa[:size]
    cross=(sa*np.conj(sb)).real
    out=[]
    for name,lo,hi in bands:
        mask=(freqs>=lo)&(freqs<hi)
        ea=float((np.abs(sa[mask])**2).sum());eb=float((np.abs(sb[mask])**2).sum())
        value=float(np.clip(cross[mask].sum()/np.sqrt(ea*eb),-1,1)) if ea*eb>1e-20 else None
        out.append({'name':name,'lowHz':lo,'highHz':hi,'correlation':value,
                    'bothPresent':bool(ea>1e-12 and eb>1e-12)})
    return out

def mono_compatibility(audio,rate,split=LOW_SPLIT):
    """What folding to mono would cost, and how much of the low end survives it.

    Vinyl and a club sub both hear the sum. Energy that only exists because the sides are
    fighting disappears there, so this reports the level in and out of mono and what is lost.
    """
    audio=np.asarray(audio,dtype=np.float64)
    if audio.ndim!=2 or audio.shape[1]<2 or len(audio)<256:return None
    l,r=audio[:,0],audio[:,1]
    stereo=np.sqrt(np.mean(l*l+r*r)/2);mono=np.sqrt(np.mean(((l+r)/2)**2))
    freqs,spec=_spectrum((l+r)/2,rate)
    low=(freqs<split)
    lows=np.abs(spec[low])**2
    _,sl=_spectrum(l,rate);_,sr=_spectrum(r,rate)
    size=min(len(sl),len(sr),len(spec))
    cross=(sl[:size]*np.conj(sr[:size])).real
    ea=float((np.abs(sl[:size])**2)[low[:size]].sum());eb=float((np.abs(sr[:size])**2)[low[:size]].sum())
    return {'stereoDbFS':db(stereo),'monoDbFS':db(mono),'lostInMonoDb':round(db(stereo)-db(mono),2),
            'lowCorrelation':float(np.clip(cross[low[:size]].sum()/np.sqrt(ea*eb),-1,1)) if ea*eb>1e-20 else None,
            'belowHz':split,'lowEnergyDbFS':db(np.sqrt(lows.sum()/max(1,size)))}

def masking(a_bands,b_bands,threshold_db=-40.0):
    """Where two tracks are both loud in the same band, and by how much one sits over the other."""
    out=[]
    by_name={band['name']:band for band in b_bands}
    for band in a_bands:
        other=by_name.get(band['name'])
        if other is None:continue
        both=band['energyDbFS']>threshold_db and other['energyDbFS']>threshold_db
        out.append({'name':band['name'],'lowHz':band['lowHz'],'highHz':band['highHz'],
                    'aDbFS':round(band['energyDbFS'],1),'bDbFS':round(other['energyDbFS'],1),
                    'overlapping':bool(both),
                    'differenceDb':round(band['energyDbFS']-other['energyDbFS'],1) if both else None})
    return out

def transient_lag(a_audio,b_audio,rate,max_ms=MAX_LAG_MS):
    """How far apart two tracks' transients land, in milliseconds, and how sure that is.

    Positive means the second one is late. Measured on the amplitude envelope, not the waveform,
    so it answers 'do they hit together', not 'are they phase aligned at 40 Hz'.
    """
    a=np.asarray(a_audio,dtype=np.float64);b=np.asarray(b_audio,dtype=np.float64)
    a=a.mean(axis=1) if a.ndim==2 else a;b=b.mean(axis=1) if b.ndim==2 else b
    n=min(len(a),len(b))
    if n<int(rate*.05):return None
    a,b=np.abs(a[-n:]),np.abs(b[-n:])
    step=max(1,int(rate/2000))                       # envelope at ~2 kHz is plenty for transients
    a=a[:n//step*step].reshape(-1,step).max(axis=1);b=b[:n//step*step].reshape(-1,step).max(axis=1)
    a=a-a.mean();b=b-b.mean()
    if np.std(a)<1e-9 or np.std(b)<1e-9:return None
    limit=int(max_ms/1000.0*rate/step)
    if limit<1 or limit>=len(a):return None
    best=(None,-2.0)
    denom=np.sqrt(np.sum(a*a)*np.sum(b*b))
    for lag in range(-limit,limit+1):
        if lag<0:x,y=a[-lag:],b[:len(b)+lag]
        elif lag>0:x,y=a[:len(a)-lag],b[lag:]
        else:x,y=a,b
        value=float(np.sum(x*y)/denom) if denom>1e-18 else 0.0
        if value>best[1]:best=(lag,value)
    lag,value=best
    # A best-fit lag always exists, even between two unrelated signals. Below this the answer is
    # the search finding something rather than the tracks actually landing together.
    confidence=round(float(np.clip(value,-1,1)),3)
    return {'lagMs':round(lag*step/rate*1000.0,2),'confidence':confidence,
            'reliable':confidence>=CONFIDENT,'searchedMs':max_ms,
            'why':None if confidence>=CONFIDENT else
                  'their transients do not line up well enough for the timing to mean anything'}

def relationship(a_audio,b_audio,rate,names=('A','B')):
    """Everything the scope can say about two tracks together, with the levels it was judged at."""
    a_level=level(a_audio);b_level=level(b_audio)
    a_bands=spectrum(a_audio,rate);b_bands=spectrum(b_audio,rate)
    quiet=[n for n,lvl in zip(names,(a_level,b_level)) if not lvl['playing']]
    out={'tracks':list(names),'levels':{names[0]:a_level,names[1]:b_level},
         'bandCorrelation':correlation_by_band(a_audio,b_audio,rate),
         'masking':masking(a_bands,b_bands),'transient':transient_lag(a_audio,b_audio,rate),
         'lowCorrelation':None,'belowHz':LOW_SPLIT}
    for band in out['bandCorrelation']:
        if band['highHz']<=LOW_SPLIT and band['correlation'] is not None:
            out['lowCorrelation']=band['correlation'] if out['lowCorrelation'] is None else \
                round((out['lowCorrelation']+band['correlation'])/2,4)
    if quiet:
        out['measurable']=False
        out['why']='%s %s not playing in this window, so nothing here is a judgement about the mix.'%(
            ' and '.join(quiet),'is' if len(quiet)==1 else 'are')
    else:out['measurable']=True
    return out
