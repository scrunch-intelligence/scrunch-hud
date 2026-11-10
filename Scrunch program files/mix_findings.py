"""Evidence-linked observations shared verbatim by Mixing and model context.

No genre score, taste verdict, track attribution or automatic correction. Findings
are saved with new audits; legacy observations are generated from their stored numbers.
"""

VERSION='mix-findings-3'
# A check requires >1 dB additional loss beyond decorrelated equal-channel mono.
# This is an explicit reporting sensitivity, never a musical approval threshold.
CANCELLATION_RATIO=.5*10**(-1/10)
# Reporting priority only. Keep faint measurements; do not call them inaudible.
FAINT_RELATIVE_DB=40

def _stamp(t):
    return '%d:%02d'%(int(t)//60,int(t)%60)

def build(report):
    rows=[];duration=report['durationSeconds'];whole=report['whole']
    def add(key,title,text,evidence,*,interval=None,status='measured',next_step=''):
        rows.append({'id':key,'auditId':report['id'],'title':title,'text':text,'status':status,
                     'intervalSeconds':interval or [0,duration],'evidence':evidence,
                     'basis':'Saved audio measurements; not a current Live reading.',
                     'interpretation':'Musical value and source-track attribution are unproved.',
                     'nextStep':next_step})
    loud=whole.get('integratedLUFS');peak=whole.get('truePeakDbTP')
    if loud is None:
        add('level','No measurable program loudness','The loudness meter could not produce an integrated LUFS reading. Silence or ungated signal is not a good-mix score.',{'integratedLUFS':None})
    else:
        add('level','Your average playback level',f'This bounce measures {loud:.2f} LUFS integrated. Its version type is {report["stage"]}; no fixed mastering target or quality score is implied.',{'integratedLUFS':loud,'meter':whole.get('meter'),'stage':report['stage']},next_step='Compare tone and balance at matched playback loudness before judging a change.')
    if peak is not None:
        add('peak','Peaks above digital full scale' if peak>0 else 'Your highest reconstructed peak',f'True peak measures {peak:.2f} dBTP.'+(' Reconstructed peaks exceed 0 dBTP; encoded samples and playback hardware may behave differently.' if peak>0 else ' This measures reconstructed peaks, not proof of distortion-free playback.'),{'truePeakDbTP':peak,'validation':whole.get('truePeakValidation')},status='check' if peak>0 else 'measured',next_step='Check transient peaks and the final limiter if unexpected distortion is audible.')
    source=report.get('liveSource')
    if source:
        coverage=source.get('captureCoverage')
        proven=bool(coverage and coverage.get('verified'))
        add('capture-range','Where these timestamps begin',
            (f'The recorded extent matches requested Live bars {source.get("startBar")}–{source.get("endBarExclusive")} (end exclusive). '
             if proven else 'This saved Live excerpt has no passing recorded range check. ')+
            'The timeline starts at 0:00 at the capture start. Its seconds are relative to this excerpt; absolute song time, tempo-map timing and waveform alignment are unverified.',
            {'captureCoverage':coverage,'startBar':source.get('startBar'),'endBarExclusive':source.get('endBarExclusive'),
             'reportTimeOrigin':source.get('reportTimeOrigin'),'songSecondsMapping':source.get('songSecondsMapping')},
            status='measured' if proven else 'unmeasured',
            next_step='Keep excerpt seconds separate from the mastering engineer’s whole-song timestamps.')
    stereo=report.get('stereo')
    if not stereo:
        add('stereo-missing','Mono compatibility is unmeasured','This saved report contains side energy but no measured left/right correlation or mono fold-down. Side energy alone cannot prove mono compatibility.',{'stereo':None},status='unmeasured',next_step='Analyse the original bounce again to measure correlation and mono fold-down.')
        return rows
    for name,title in [('broadband','The whole mix in mono'),('lowEnd','Your low end in mono')]:
        value=stereo.get(name)
        if not value or value.get('belowReportingFloor'):
            add('mono-'+name,title,'No reportable '+('20–200 Hz signal above -80 dBFS.' if name=='lowEnd' else 'broadband signal.')+' Mono compatibility remains unknown.',{'measurement':value},status='unmeasured')
            continue
        corr=value.get('correlation');change=value.get('monoChangeDb');cancelled=value.get('monoCancelled')
        region='20–200 Hz' if name=='lowEnd' else 'the whole bounce'
        loss='cancels to the measurement floor' if cancelled else f'changes by {change:.2f} dB' if change is not None else 'is unmeasurable'
        correlation=f'{corr:+.3f}' if corr is not None else 'unmeasurable (one channel is absent or too quiet)'
        check=corr is not None and corr<0 and value['monoPowerRatio']<CANCELLATION_RATIO
        add('mono-'+name,title,f'For {region}, left/right correlation is {correlation}; the mono fold-down {loss}. Mono is (L+R)/2 versus mean channel power. Independent equal channels normally lose 3.01 dB by this convention.'+(' Negative cross-power indicates cancellation; listen in mono before changing the sound.' if check else ' This is a measurement, not a width-quality verdict.'),{'measurement':value,'method':stereo['basis'],'checkSensitivity':'More than 1 dB extra mono loss beyond decorrelated equal channels; not a quality rule.'},status='check' if check else 'measured',next_step='Compare stereo and mono; inspect individual sources only if the loss matters musically.')
    # Bounded timeline evidence: show the strongest cancellation windows, not every
    # low/negative reading. This flags a mathematical condition, not a genre rule.
    candidates=[]
    levels=[w.get('stereo',{}).get('lowEnd',{}).get('energyDbFS') for w in report.get('windows',[])]
    levels=[v for v in levels if type(v) in (int,float)]
    loudest=max(levels) if levels else None
    for w in report.get('windows',[]):
        m=w.get('stereo',{}).get('lowEnd',{})
        if not m.get('belowReportingFloor',True) and m.get('correlation') is not None and m['correlation']<0 and m['monoPowerRatio']<CANCELLATION_RATIO:
            # Amount of band power lost, rather than ratio alone: a near-silent
            # tail must not outrank a much louder affected musical interval.
            power=(m.get('leftPower',0)+m.get('rightPower',0))/2
            candidates.append((power*(1-m['monoPowerRatio']),w,m))
    for _,w,m in sorted(candidates,key=lambda x:(-x[0],x[1]['startSeconds']))[:6]:
        a,b=w['startSeconds'],w['endSeconds']
        relative=m.get('energyDbFS')-loudest if loudest is not None and m.get('energyDbFS') is not None else None
        faint=relative is not None and relative<=-FAINT_RELATIVE_DB
        loss='cancels to the measurement floor' if m['monoCancelled'] else f'changes by {m["monoChangeDb"]:.2f} dB'
        context=(f' Its low-end level is {m["energyDbFS"]:.2f} dBFS, {abs(relative):.2f} dB below this bounce’s loudest low-end window. This is retained as a faint measurement, not a priority fault; audibility is unproved.' if faint else ' This interval has measured cancellation, but the responsible track and musical impact are unproved.')
        add('low-mono-'+w['id'],('Faint low-end measurement at ' if faint else 'Check low end at ')+_stamp(a),f'From {a:.3f} to {b:.3f} seconds, 20–200 Hz correlation is {m["correlation"]:+.3f} and mono {loss}.'+context,{'windowId':w['id'],'measurement':m,'method':stereo['basis'],'relativeToLoudestLowEndDb':relative,'priorityBasis':'Ranked by low-end power lost; intervals at least 40 dB below the loudest low-end window remain measurements rather than priority checks. Not an audibility or quality rule.','checkSensitivity':'More than 1 dB extra mono loss beyond decorrelated equal channels; not a quality rule.'},interval=[a,b],status='measured' if faint else 'check',next_step='Listen to this faint interval before treating it as a problem.' if faint else 'Ask the engineer to inspect this interval and audition a reversible source-specific trial.')
    return rows
