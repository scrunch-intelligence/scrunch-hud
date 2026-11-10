"""Validate captured range metadata before a mix comparison. No host or model I/O."""
import math
import numpy as np

VERSION='capture-coverage-1'


def check(stems,record,start,end,tempo,expected_tracks=None,*,require_main=True):
    errors=[];frames={};rate=record.get('sampleRate');first=record.get('firstBeat');last=record.get('lastBeat')
    numeric=lambda v:type(v) in (int,float) and math.isfinite(v)
    if not numeric(rate) or rate<=0:errors.append('Sample rate is missing or invalid.')
    if not numeric(first) or not numeric(last) or (numeric(first) and numeric(last) and first>=last):
        errors.append('Captured start/end beats are missing or invalid.')
    if not numeric(tempo) or tempo<=0 or not numeric(start) or not numeric(end) or start>=end:
        errors.append('Requested time base is invalid.')
    if not isinstance(stems,dict) or not stems:errors.append('Captured audio is missing.')
    elif require_main and 'main' not in stems:errors.append('Main audio is missing.')
    if isinstance(stems,dict):
        if expected_tracks is not None and set(stems)!=set(expected_tracks):
            errors.append('Captured tracks do not match the requested tracks.')
        for name,y in stems.items():
            y=np.asarray(y);frames[name]=len(y) if y.ndim else 0
            if y.ndim!=2 or y.shape[1]!=2 or not len(y) or not np.isfinite(y).all():
                errors.append('%s audio is not finite nonempty stereo.'%name)
    if len(set(frames.values()))>1:errors.append('Tracks in this pass have unequal frame counts.')
    if not errors:
        # The rig rounds endpoints to samples. This checks its recorded extent,
        # not the accuracy of the host clock or the waveform's musical offset.
        tolerance=1.01*tempo/(60*rate)
        if abs(first-start)>tolerance or abs(last-end)>tolerance:
            errors.append('Captured range does not cover the requested range within one sample.')
        expected=(end-start)*60/tempo*rate
        if any(abs(n-expected)>1.01 for n in frames.values()):errors.append('Audio length does not match the requested range.')
        if any(abs(n-(last-first)*60/tempo*rate)>1.01 for n in frames.values()):
            errors.append('Audio length disagrees with captured beat metadata.')
        if 'tempo' in record and (not numeric(record['tempo']) or abs(record['tempo']-tempo)>1e-6):
            errors.append('Recorded tempo differs from the requested tempo.')
    for name,row in (record.get('checks') or {}).items():
        if row.get('gaps') or row.get('droppedChanged') or row.get('nonIntegerSampleTimes'):
            errors.append('%s capture reports missing or irregular samples.'%name)
    return {'verified':not errors,'errors':errors,'sampleRate':rate,'capturedBeats':[first,last],
            'requestedBeats':[start,end],'frames':frames,'tempoRecorded':'tempo' in record,
            'streamChecksRecorded':bool(record.get('checks'))}


def pair(before,after,rec_before,rec_after,plan,tempo,expected_tracks=None):
    a=check(before,rec_before,plan['startBeat'],plan['endBeat'],tempo,expected_tracks)
    b=check(after,rec_after,plan['startBeat'],plan['endBeat'],tempo,expected_tracks)
    errors=[]
    if set(before)!=set(after):errors.append('Original and Candidate contain different tracks.')
    if rec_before.get('sampleRate')!=rec_after.get('sampleRate'):errors.append('Sample rate changed between passes.')
    return {'version':VERSION,'verified':a['verified'] and b['verified'] and not errors,
            'original':a,'candidate':b,'errors':errors,
            'basis':'Recorded ranges, rates and frame counts checked; not waveform alignment or proof of identical musical playback. Host clock timing remains approximate.'}


def explanation(result):
    return '; '.join(result.get('errors',[])+
        ['Original: '+e for e in result.get('original',{}).get('errors',[])]+
        ['Candidate: '+e for e in result.get('candidate',{}).get('errors',[])])
