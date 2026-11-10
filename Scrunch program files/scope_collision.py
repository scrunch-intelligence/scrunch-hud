"""Where two tracks are fighting, at the resolution a mix decision needs.

Pro-Q 4 lets a producer pull another track's spectrum into the EQ so the collision can be *seen*
and carved around by hand. This does the seeing in numbers: third-octave bands, both tracks
measured from the same moment of audio, and the places where they are both loud ranked by how
much it matters.

Two things it deliberately will not do:

  * **It will not call a collision from one loud track.** Two tracks occupying the same band is
    only a problem when both are actually there; a kick alone in the sub is a kick, not a clash.
  * **It will not decide who loses.** Which track keeps a band is a musical decision - the kick
    usually keeps the sub, but not in every record ever made. It says who is on top and by how
    much, proposes the move, and says what it assumed.

The bands are third-octave because that is roughly what an ear resolves and roughly what an EQ
band with a musical Q covers. Anything finer describes the spectrum, not a decision.
"""
import math
import numpy as np

FLOOR_DB=-60.0          # below this a band is not playing
PRESENT_DB=-45.0        # both must be above this before they can be said to collide
CONTEST_DB=10.0         # within this of each other and neither is clearly on top
LOW_SPLIT=150.0
MIN_SECONDS=0.5

def third_octaves(low=25.0,high=16000.0):
    """Centre frequencies and edges of the third-octave bands, ISO style."""
    bands=[];centre=31.5
    while centre>low*1.05:centre/=2**(1/3)
    while centre<=high:
        lo=centre/2**(1/6);hi=centre*2**(1/6)
        if lo>=low:bands.append((round(centre,1),lo,hi))
        centre*=2**(1/3)
    return bands

BANDS=third_octaves()

def spectrum(audio,rate,bands=BANDS):
    """Level per third-octave band, in dBFS, from the newest audio."""
    import scope_dsp
    audio=np.asarray(audio,dtype=np.float64)
    mono=audio.mean(axis=1) if audio.ndim==2 else audio
    if len(mono)<1024 or rate<=0:return []
    freqs,spec=scope_dsp._spectrum(mono,rate)
    power=np.abs(spec)**2
    out=[]
    for centre,lo,hi in bands:
        mask=(freqs>=lo)&(freqs<hi)
        level=scope_dsp.db(np.sqrt(power[mask].sum()/(2.0*scope_dsp.HANN_ENBW))) if mask.any() else FLOOR_DB
        out.append({'hz':centre,'lowHz':round(lo,1),'highHz':round(hi,1),'db':round(level,1)})
    return out

def collisions(a_audio,b_audio,rate,names=('A','B'),present_db=PRESENT_DB):
    """Every third-octave band where both tracks are present, worst first.

    `severity` is how much of a problem the band is: loud, contested and wide all make it worse.
    It is a ranking, not a physical quantity, and it is only ever computed where both are playing.
    """
    a=spectrum(a_audio,rate);b=spectrum(b_audio,rate)
    if not a or not b:return []
    out=[]
    for one,two in zip(a,b):
        both=one['db']>present_db and two['db']>present_db
        if not both:continue
        difference=one['db']-two['db']
        quieter=min(one['db'],two['db'])
        # A band matters more when the quieter of the two is still loud (there is real energy
        # being shared) and when neither is clearly winning (nobody has made room).
        contested=max(0.0,1.0-abs(difference)/CONTEST_DB)
        loudness=max(0.0,(quieter-present_db)/max(1.0,-present_db))
        out.append({'hz':one['hz'],'lowHz':one['lowHz'],'highHz':one['highHz'],
                    names[0]:one['db'],names[1]:two['db'],
                    'differenceDb':round(difference,1),
                    'onTop':names[0] if difference>0 else names[1],
                    'severity':round(contested*loudness,3)})
    return sorted(out,key=lambda row:-row['severity'])

def _group(rows,names):
    """Neighbouring bands that are fighting are one problem, not six."""
    groups=[]
    for row in sorted(rows,key=lambda r:r['hz']):
        if groups and row['lowHz']<=groups[-1]['highHz']*1.05 and row['onTop']==groups[-1]['onTop']:
            last=groups[-1]
            last['highHz']=row['highHz'];last['bands'].append(row)
            last['severity']=max(last['severity'],row['severity'])
        else:
            groups.append({'lowHz':row['lowHz'],'highHz':row['highHz'],'onTop':row['onTop'],
                           'severity':row['severity'],'bands':[row]})
    for g in groups:
        worst=max(g['bands'],key=lambda r:r['severity'])
        g['hz']=worst['hz']
        g[names[0]]=worst[names[0]];g[names[1]]=worst[names[1]]
        g['differenceDb']=worst['differenceDb']
        g['bandCount']=len(g['bands'])
        g.pop('bands')
    return sorted(groups,key=lambda g:-g['severity'])

def _q(low,high):
    """The Q that covers a range: centre frequency over its width."""
    centre=math.sqrt(low*high)
    return round(centre/max(1e-9,high-low),2)

def advise(a_audio,b_audio,rate,names=('A','B'),keeps_low=None,limit=3):
    """What to do about it, said the way an engineer would say it.

    `keeps_low` names the track that should own the low end when both are down there - the kick,
    usually. Nothing is assumed: if it is not given, the low end is reported as contested and the
    producer is told that is the one decision the numbers cannot make for them.
    """
    import scope_dsp
    a_level=scope_dsp.level(a_audio);b_level=scope_dsp.level(b_audio)
    quiet=[name for name,level in zip(names,(a_level,b_level)) if not level['playing']]
    if quiet:
        return {'tracks':list(names),'measurable':False,
                'why':'%s not playing in this window, so there is nothing to compare.'
                      %(' and '.join(quiet)),'moves':[],'collisions':[]}
    rows=collisions(a_audio,b_audio,rate,names)
    groups=_group(rows,names)
    moves=[]
    for group in groups[:limit]:
        loser=names[0] if group['onTop']==names[1] else names[1]
        keeper=group['onTop']
        low_end=group['highHz']<=LOW_SPLIT
        assumed=None
        if low_end and keeps_low in names:
            keeper=keeps_low;loser=names[1] if keeps_low==names[0] else names[0]
            assumed='%s was told to own the low end'%keeper
        elif low_end:
            assumed='nobody was named as owning the low end, so this is the one to decide by ear'
        # Take out what the quieter track is contributing over the top, not the whole overlap:
        # enough to open the band, never enough to remove the part.
        depth=round(min(6.0,max(1.0,abs(group['differenceDb'])/2.0+1.0)),1)
        moves.append({'do':'cut','track':loser,'hz':group['hz'],
                      'fromHz':round(group['lowHz'],1),'toHz':round(group['highHz'],1),
                      'byDb':-depth,'q':_q(group['lowHz'],group['highHz']),
                      'toMakeRoomFor':keeper,'severity':group['severity'],
                      'bandsWide':group['bandCount'],'assumed':assumed,
                      'say':'%s and %s are both loud around %s Hz; %s is on top by %.1f dB. '
                            'Cut %s by %.1f dB there (Q %.1f) to make room for %s.'
                            %(names[0],names[1],group['hz'],group['onTop'],abs(group['differenceDb']),
                              loser,depth,_q(group['lowHz'],group['highHz']),keeper)})
    return {'tracks':list(names),'measurable':True,'moves':moves,
            'collisions':groups,'bandsChecked':len(BANDS),
            'basis':'third-octave bands, both tracks from the same window of audio',
            'note':'Where each band should go is a musical decision; these are the places worth deciding about.'}
