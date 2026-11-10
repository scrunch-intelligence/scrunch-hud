"""Bounded local WAV/AIFF measurements; estimates explicitly separated from facts."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'.analysis-deps'))
VERSION='audio-foundation-1'

def fast(path):
    import numpy as np,soundfile as sf,librosa,pyloudnorm as pyln
    from scipy.signal import resample_poly
    info=sf.info(path)
    if info.channels not in (1,2) or info.duration>600 or info.frames<1:raise ValueError('Analysis supports nonempty mono/stereo audio up to 10 minutes.')
    y,sr=sf.read(path,always_2d=True,dtype='float64')
    if not np.isfinite(y).all():raise ValueError('Nonfinite audio samples.')
    peak=float(np.max(np.abs(y)));mono=y.mean(axis=1)
    db=lambda x:float(20*np.log10(x)) if x>0 else None
    loud=pyln.Meter(sr).integrated_loudness(y) if len(y)>=sr*.4 else float('nan')
    # Oversampled approximation, not certified BS.1770 true-peak metering.
    inter=float(np.max(np.abs(resample_poly(y,4,1,axis=0))))
    sample=librosa.resample(mono[:min(len(mono),sr*60)],orig_sr=sr,target_sr=22050)
    onset=librosa.onset.onset_strength(y=sample,sr=22050)
    bpm,frames=librosa.beat.beat_track(onset_envelope=onset,sr=22050)
    beats=librosa.frames_to_time(frames,sr=22050).tolist();tempo=float(np.asarray(bpm).reshape(-1)[0])
    sections=[]
    major=np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88]);minor=np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
    for start in range(0,len(sample),22050*15):
        chunk=sample[start:start+22050*15]
        if len(chunk)<2048:continue
        chroma=librosa.feature.chroma_stft(y=chunk,sr=22050).mean(axis=1)
        scores=[]
        if np.std(chroma)>1e-8 and np.max(np.abs(chunk))>1e-6:
            for mode,profile in [('major',major),('minor',minor)]:
                for n in range(12):scores.append((float(np.corrcoef(chroma,np.roll(profile,n))[0,1]),n,mode))
        scores.sort(reverse=True)
        candidates=[{'tonic':['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][n],'mode':mode,'score':score} for score,n,mode in scores[:3]]
        sections.append({'startSeconds':start/22050,'endSeconds':(start+len(chunk))/22050,'kind':'fixed-analysis-window-not-musical-section','keyCandidates':candidates,'confidence':'uncalibrated','method':'chroma/profile-correlation'})
    return {'durationSeconds':info.duration,'sampleRate':sr,'channels':info.channels,
      'samplePeakDbFS':db(peak),'oversampledPeakDbFS':db(inter),'peakMethod':'scipy 4x polyphase approximation; not certified true peak',
      'integratedLUFS':float(loud) if np.isfinite(loud) else None,'loudnessMethod':'pyloudnorm BS.1770','silence':peak==0,
      'tempo':{'candidatesBPM':[tempo,tempo/2,tempo*2] if tempo else [],'beatSeconds':beats,'confidence':'uncalibrated','coverageSeconds':len(sample)/22050,'method':'librosa beat_track'},
      'downbeat':{'value':None,'confidence':0,'reason':'No validated downbeat estimator in this build.'},
      'sections':sections,'material':{'label':'silence' if peak==0 else 'unknown','confidence':1 if peak==0 else 0},
      'automationGate':{'allowed':False,'reason':'Grid/downbeat confidence is not calibrated; no automatic warp or timeline edits.'},
      'analysisVersion':VERSION}

def deep(path):
    import numpy as np,soundfile as sf
    y,sr=sf.read(path,always_2d=True)
    return {'rmsByChannel':np.sqrt(np.mean(y*y,axis=0)).tolist(),'method':'sample RMS','sampleRate':sr}
