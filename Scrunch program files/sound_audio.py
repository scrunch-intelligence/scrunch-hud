"""Hear the result: capture the coach track through the Live Ears tap and measure what changed.

Scrunch's existing analysis (`reference_dsp`) does the work: EBU R128 loudness and the band
energies. On top of that this adds one number a sound-design move can be judged by, the
brightness centre: the energy-weighted centre of the band spectrum in Hz.

The tap sits in the track's device chain, before Live's fader, so these numbers describe the
instrument, not the finished mix. A capture is only valid while the track is playing.
"""
import array,math,threading,time

MIN_SECONDS=3.0            # reference_dsp.prepare refuses anything shorter
CAPTURE_TIMEOUT=4.0        # extra wall-clock allowance over the requested length

CAPTURE_LOCK=threading.Lock()   # a Live Ears stream takes one receiver: captures in this process take turns

def capture(seconds=MIN_SECONDS,source=None,discover=None):
    """Interleaved float32 audio from the tap as (frames, channels) and its sample rate."""
    with CAPTURE_LOCK:
        return _capture(seconds,source,discover)

def _capture(seconds,source,discover):
    from live_ears import LiveEarsSource
    discover=discover or LiveEarsSource.discover
    streams=discover()
    if not streams:raise ValueError('No Live Ears stream: add the Scrunch Live Ears plug-in to the track.')
    seconds=max(MIN_SECONDS,float(seconds))
    src=source or LiveEarsSource(streams[0])
    frames=[];rate=None;channels=2;silent=True
    deadline=time.time()+seconds+CAPTURE_TIMEOUT;started=None
    try:
        while time.time()<deadline:
            block=src.read()
            if block is None:
                time.sleep(.005);continue
            rate=block.sample_rate or rate;channels=block.channels or channels
            samples=array.array('f');samples.frombytes(block.pcm_float32_interleaved)
            if any(samples):silent=False
            if started is None:started=time.time()
            frames.extend(samples)
            if len(frames)/(channels*(rate or 48000))>=seconds:break
    finally:
        if source is None:src.close()
    if not frames or not rate:raise ValueError('The tap delivered no audio; is the track playing?')
    if silent:raise ValueError('The tap delivered silence; check the track is playing and audible.')
    return frames,int(rate),int(channels)

ONSET_GAP_MS=70            # notes closer than this are treated as one
ONSET_RISE=2.5             # how far the envelope must jump above its recent floor to count as a note
DECAY_TARGET=.35           # decay time is measured down to this fraction of the note's peak
OVERLAP_LIMIT=.75          # above this much level between notes, attack timing is not trustworthy
                           # (measured: 2 ms reads 5, 40 ms reads 30, 100 ms reads 76 up to this point)

def envelope(mono,rate):
    """How the notes behave in time: {attackMs, decayMs, sustainRatio, notes}.

    A rectified, smoothed envelope; notes are found by rises above the local floor. Per note:
    how long it takes to reach its peak, how long it takes to fall to a third of it, and how much
    level is left a fifth of a second in. Medians across the notes heard.
    """
    import numpy as np
    x=np.abs(np.asarray(mono,dtype=float))
    if not len(x):return {}
    window=max(1,int(rate*.005))
    env=np.convolve(x,np.ones(window)/window,mode='same')
    if env.max()<=1e-6:return {}
    gap=int(rate*ONSET_GAP_MS/1000.)
    # Notes are found by how sharply the level rises, not by it dropping away between them:
    # on a held sound the notes overlap and the level never returns to the floor.
    quiet=float(np.percentile(env,20));loud=float(np.percentile(env,90)) or 1.
    rise=np.maximum(0.,env[gap:]-env[:-gap])
    onsets=[]
    if rise.size and rise.max()>0:
        threshold=max(rise.max()*.25,float(np.percentile(rise,95))*.5)
        i=0
        while i<len(rise):
            if rise[i]>=threshold:
                # rise[i] compares env[i+gap] with env[i], so the note itself starts somewhere in
                # between: take the first point that has clearly left the floor before it.
                span=env[i:i+2*gap]
                start=int(i+int(np.argmax(span>=quiet+(span.max()-quiet)*.1))) if span.size else i
                onsets.append(start);i+=gap*2
            else:i+=1

    attacks=[];decays=[];sustains=[]
    for n,start in enumerate(onsets):
        end=min(len(env),onsets[n+1] if n+1<len(onsets) else start+int(rate*.6))
        segment=env[start:end]
        if len(segment)<window*4:continue
        peak_at=int(np.argmax(segment));peak=segment[peak_at]
        if peak<=1e-6:continue
        # Time the attack from the dip before the peak, not from the detected onset: with
        # overlapping notes the level never returns to silence, so a fixed start overstates it.
        head=segment[:peak_at+1]
        if len(head)>2:
            dip=float(head.min());crossed=np.where(head<=dip+(peak-dip)*.1)[0]
            begin=int(crossed[-1]) if len(crossed) else 0
            attacks.append((peak_at-begin)/rate*1000)
        below=np.where(segment[peak_at:]<peak*DECAY_TARGET)[0]
        decays.append((below[0]/rate*1000) if len(below) else (len(segment)-peak_at)/rate*1000)
        # How much of the note is still there just after the start, measured against its own peak
        # and with the room's floor taken off, over a window that ends before the next note.
        tail_from=peak_at+int(rate*.06);tail_to=min(len(segment),peak_at+int(rate*.30))
        if tail_to-tail_from>window:
            tail=float(np.mean(segment[tail_from:tail_to]))
            # In dB against the note's own peak: a plucky note sits far below it, a held one close.
            # dB keeps small tails stable, where a plain ratio wanders by tens of percent.
            sustains.append(20*math.log10(max(tail-quiet,1e-6)/max(peak-quiet,1e-6)))
    # How much level survives between the loud moments: high for a pad, low for a pluck.
    hold=round(quiet/loud,4) if loud else None
    if not decays:return {'holdRatio':hold,'notes':0}
    median=lambda v:float(np.median(v))
    out={'decayMs':round(median(decays),1),'sustainDb':round(median(sustains),2),
         'holdRatio':hold,'notes':len(decays)}
    # Attack is only honest when the notes are actually separated; on a washy sound it is not.
    if attacks and hold is not None and hold<OVERLAP_LIMIT:out['attackMs']=round(median(attacks),1)
    return out

KEPT_AUDIO={}             # captureId -> (mono float32 at 48 kHz, rate): the last few captures, for A/B audio evidence
KEPT_MAX=12

def keep_audio(mono,rate):
    """Hold the audio a measurement was made from, so an A/B offer can store what he actually heard."""
    import uuid,numpy as np
    cid=uuid.uuid4().hex[:12]
    KEPT_AUDIO[cid]=(np.asarray(mono,dtype=np.float32),int(rate))
    while len(KEPT_AUDIO)>KEPT_MAX:KEPT_AUDIO.pop(next(iter(KEPT_AUDIO)))
    return cid

def kept_audio(capture_id):
    """(mono float32, rate) for a recent capture, or None when it has been let go."""
    return KEPT_AUDIO.get(capture_id)

def measure(frames,rate,channels):
    """{loudnessLUFS, peakDbFS, brightnessHz, bands, envelope} for captured audio."""
    import numpy as np,reference_dsp as dsp
    y=np.asarray(frames,dtype=float)
    y=y.reshape(-1,channels) if channels>1 else y[:,None]
    prepared=dsp.prepare(y,rate)
    r=dsp.analyse(prepared)
    energies=[]
    for band in r['bands']:
        level=band['energyDbFS']
        if level is None or level<-80:continue
        energies.append((math.sqrt(band['lowHz']*band['highHz']),10**(level/20)))
    total=sum(e for _,e in energies)
    high=sum(e for c,e in energies if c>=2000)
    sides=[b['sideFraction'] for b in r['bands'] if b.get('sideFraction') is not None and (b['energyDbFS'] or -99)>-80]
    mono=prepared.mean(axis=1)
    capture_id=keep_audio(mono,48000)
    return {'captureId':capture_id,'envelope':envelope(mono,48000),
            'loudnessLUFS':r.get('integratedLUFS'),'peakDbFS':r.get('samplePeakDbFS'),
            'brightnessHz':round(sum(c*e for c,e in energies)/total) if total else None,
            # On a bass-heavy patch the brightness centre barely moves, so also track how much of
            # the energy sits above 2 kHz, as a share of the whole.
            'highShare':round(high/total,5) if total else None,
            'widthShare':round(sum(sides)/len(sides),5) if sides else None,
            'bands':[{'name':b['name'],'energyDbFS':b['energyDbFS']} for b in r['bands']]}

ENVELOPE_TOLERANCE=.12     # 12% either way counts as "no real change" in a time measurement
SUSTAIN_DB_TOLERANCE=1.0   # dB: below this the note's tail has not really changed. Measured wander
                           # on steady patches is 0.3-1.1 dB, and the guard raises this to twice
                           # whatever the sound actually wanders on the day.
BRIGHTNESS_TOLERANCE=.03   # 3% either way counts as "no real change"
HIGH_SHARE_TOLERANCE=.08   # 8% change in the share of energy above 2 kHz
ENERGY_TOLERANCE=.5        # dB

def _envelope_value(m,key):
    return (m.get('envelope') or {}).get(key)

def _low_energy(m):
    levels=[b['energyDbFS'] for b in m['bands'] if b['name'] in ('sub','bass') and b['energyDbFS'] is not None]
    return sum(levels)/len(levels) if levels else None

def verdict(move,before,after,repeat=None,expect=None):
    """Did the audio move the way the instruction meant? 'achieved', 'no change', 'wrong way' or 'not measurable'.

    `repeat` is a second capture of the *unchanged* sound. The difference between it and `before`
    is how much this measurement wanders by itself, so a change smaller than twice that is not
    called a result. Reverb and delay tails make the note measurements wander most.
    """
    want=expect                              # an interpreted move says for itself what should change
    loudness=None
    if before.get('loudnessLUFS') is not None and after.get('loudnessLUFS') is not None:
        loudness=round(after['loudnessLUFS']-before['loudnessLUFS'],2)
    out={'move':move,'loudnessChangeLU':loudness,
         'brightnessHz':{'before':before.get('brightnessHz'),'after':after.get('brightnessHz')}}
    if not want:return dict(out,result='not measurable',why='No audio test is defined for %s.'%move)
    key,direction=want
    if key=='loudnessLUFS':a,b=before.get('loudnessLUFS'),after.get('loudnessLUFS')
    elif key=='widthShare':a,b=before.get('widthShare'),after.get('widthShare')
    elif key in ('decayMs','attackMs','sustainDb'):
        a,b=_envelope_value(before,key),_envelope_value(after,key)
        if (a is None or b is None) and key!='attackMs':
            # no separate notes to time: fall back to how much level survives between them
            key='holdRatio';a,b=_envelope_value(before,key),_envelope_value(after,key)
    elif key in ('brightnessHz','highShare'):a,b=before.get(key),after.get(key)
    else:a,b=_low_energy(before),_low_energy(after)
    out['notesHeard']=(before.get('envelope') or {}).get('notes')
    absolute=key in ('lowEnergyDb','sustainDb','loudnessLUFS')  # compared in dB, not as a proportion
    if a is None or b is None or (not absolute and not a):
        return dict(out,result='not measurable',
                    why='No separate notes were heard to time.' if key in ('decayMs','attackMs','sustainDb')
                        else 'The capture was too quiet to judge.')
    change=(b-a) if absolute else (b-a)/a
    tolerance=(SUSTAIN_DB_TOLERANCE if key=='sustainDb' else BRIGHTNESS_TOLERANCE if key=='brightnessHz' else
               ENVELOPE_TOLERANCE if key in ('decayMs','attackMs','holdRatio') else
               # shares are judged as a proportion of themselves, like the brightness fallback below,
               # not by ENERGY_TOLERANCE, which is a level in dB and only means anything for lowEnergyDb
               HIGH_SHARE_TOLERANCE if key in ('highShare','widthShare') else ENERGY_TOLERANCE)
    wander=None
    if repeat is not None:
        r=(_envelope_value(repeat,key) if key in ('decayMs','attackMs','sustainDb','holdRatio')
           else repeat.get(key) if key in ('brightnessHz','highShare','widthShare','loudnessLUFS')
           else _low_energy(repeat))
        if r is not None and (a or absolute):
            wander=abs(r-a) if absolute else abs((r-a)/a)
            out['wander']=round(wander,4)
            tolerance=max(tolerance,2*wander)
    moved='up' if change>tolerance else 'down' if change<-tolerance else 'flat'
    measured={'what':key,'before':round(a,2),'after':round(b,2),'direction':moved}
    if moved=='flat' and key=='brightnessHz':
        # Almost all the energy may be low (a bass patch): judge by the share above 2 kHz instead.
        sa,sb=before.get('highShare'),after.get('highShare')
        if sa and sb and sa>1e-6:
            share=(sb-sa)/sa
            if abs(share)>HIGH_SHARE_TOLERANCE:
                moved='up' if share>0 else 'down'
                measured={'what':'highShare','before':round(sa,5),'after':round(sb,5),'direction':moved}
    out['measured']=measured
    if moved=='flat':
        why=('The audio did not move enough to hear.' if not wander or wander<tolerance/2 else
             'This sound wanders by that much between identical takes, so the change cannot be judged; '
             'reverb or delay tails usually cause that.')
        return dict(out,result='no change' if (not wander or wander<tolerance/2) else 'not measurable',why=why)
    what={'brightnessHz':'Brightness','highShare':'The top end','lowEnergyDb':'Low-end energy',
          'decayMs':'How long each note takes to fall away','attackMs':'How long each note takes to come in',
          'sustainDb':'How much each note holds','holdRatio':'How much level survives between notes',
          'widthShare':'Stereo width','loudnessLUFS':'Loudness'}.get(measured['what'],measured['what'])
    return dict(out,result='achieved' if moved==direction else 'wrong way',why='%s went %s.'%(what,moved))
