"""Write a plain drum pattern into the arrangement, with no model involved.

Scrunch could only ever vary an original the producer had already selected, so "put a 4/4 kick
on track 1" had nothing to work from. A four-on-the-floor is not a creative act that needs a
model - it is four notes on the beat - so it is written here, in code, the same way every time.

Patterns are written into the **arrangement**, never into Session clip slots, and always as a
new clip: nothing already recorded is edited or replaced. The General MIDI drum pitches are the
ones Live's own drum racks use, so a kick lands on the kick pad of a Drum Rack; on a Simpler
holding one sample, every pitch plays that sample, so the kick note is the one that matters.
"""
import re

# General MIDI drum map, which Live's Drum Racks follow.
VOICES={'kick':36,'snare':38,'clap':39,'closed hat':42,'open hat':46,'rim':37,'tom':45,'ride':51,'crash':49}
ALIASES={'bass drum':'kick','bd':'kick','hat':'closed hat','hats':'closed hat','hihat':'closed hat',
         'hi-hat':'closed hat','closed hi-hat':'closed hat','open hi-hat':'open hat','sd':'snare'}
MAX_BARS=16
MAX_NOTES=512

def voice(name):
    """The drum this is, by the words a producer actually uses."""
    key=re.sub(r'\s+',' ',str(name or '').strip().lower())
    key=ALIASES.get(key,key)
    if key not in VOICES:
        raise ValueError('Scrunch does not know a drum called %r. It knows %s.'%(name,', '.join(sorted(VOICES))))
    return key,VOICES[key]

def pattern(kind='four on the floor',bars=1,beats_per_bar=4,velocity=100,length=.25,swing=0.0):
    """Notes for one of the patterns worth having in code, in beats from the clip start.

    `four on the floor` is a hit on every beat. `offbeat` is the and-of-every-beat, which is
    where a hat or a bass stab goes against that kick. `eighths` and `sixteenths` are straight
    subdivisions. Swing delays every second subdivision by that fraction of its own step.
    """
    kind=re.sub(r'\s+',' ',str(kind or '').strip().lower())
    bars=int(bars)
    if not 1<=bars<=MAX_BARS:raise ValueError('Write between 1 and %d bars.'%MAX_BARS)
    if not 1<=int(beats_per_bar)<=16:raise ValueError('A bar here is between 1 and 16 beats.')
    steps={'four on the floor':1.0,'4/4':1.0,'downbeats':1.0,'eighths':0.5,'sixteenths':0.25}
    if kind in ('offbeat','offbeats','upbeats'):step,shift=1.0,0.5
    elif kind in steps:step,shift=steps[kind],0.0
    else:raise ValueError('Scrunch knows these patterns: %s, offbeat.'%', '.join(sorted(steps)))
    total=bars*int(beats_per_bar)
    times=[]
    position=shift
    index=0
    while position<total-1e-9:
        late=swing*step if (index%2 and swing) else 0.0
        times.append(round(position+late,6))
        position+=step;index+=1
    if len(times)>MAX_NOTES:raise ValueError('That would be %d notes; keep it under %d.'%(len(times),MAX_NOTES))
    return times,total

def notes(drum='kick',kind='four on the floor',bars=1,beats_per_bar=4,velocity=100,length=.25,swing=0.0):
    """The clip's notes: {p, t, d, v} per hit, and how long the clip is in beats."""
    name,pitch=voice(drum)
    velocity=int(max(1,min(127,int(velocity))))
    length=float(max(.05,min(4.0,float(length))))
    times,total=pattern(kind,bars,beats_per_bar,swing=swing)
    return [{'p':pitch,'t':t,'d':length,'v':velocity} for t in times],total,name

def write(adapter,track_index,drum='kick',kind='four on the floor',bars=1,beats_per_bar=4,
          velocity=100,length=.25,swing=0.0,start=0,name=None,looping=True):
    """Put the pattern into the arrangement on that track as a new clip, and say what was written."""
    hits,total,drum_name=notes(drum,kind,bars,beats_per_bar,velocity,length,swing)
    title=name or 'Scrunch %s %s'%(drum_name,kind)
    clip=adapter.create_arrangement_clip(int(track_index),total,hits,start=start,name=title,looping=looping)
    return {'clip':clip,'track':int(track_index),'drum':drum_name,'pattern':kind,'bars':int(bars),
            'notes':len(hits),'lengthBeats':total,'startBeat':start,'name':title,
            'say':'Wrote %d %s notes — %s over %d bar%s — into the arrangement on track %d.'
                  %(len(hits),drum_name,kind,int(bars),'' if int(bars)==1 else 's',int(track_index)+1)}
