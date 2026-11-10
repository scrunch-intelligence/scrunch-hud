"""Remember a measured loudness offset, and know exactly when it stops being true.

Measuring loudness costs two three-second captures, so flipping between the original and the
moved sound re-measures the same pair over and over. A measurement is kept and reused, but only
while the two sounds being compared and the way they are being auditioned are identical to when
it was taken. Anything else and it is thrown away and measured again, immediately.

This is not automatic gain control. Nothing here measures on a timer or follows the sound while
it plays: a cached value is reused or discarded, never trimmed towards a target. A move's
dynamics are the producer's, and are left alone.
"""
import hashlib,json,time

MAX_ENTRIES=32
STALE_SECONDS=15*60        # a measurement older than this is re-taken even if nothing looks changed

def _round(value,places=5):
    try:return round(float(value),places)
    except (TypeError,ValueError):return value

def sound_state(knobs,skip=()):
    """What the instrument is set to now: every exposed knob's value, except ones a caller is about to change."""
    return [(k['setting'],_round(k['value'])) for k in sorted(knobs,key=lambda k:k['setting']) if k['setting'] not in set(skip)]

def audition_state(transport,track=None):
    """How it is being listened to: transport, loop region, and the track's own level and routing."""
    loop=(transport or {}).get('loop') or {}
    out={'playing':bool((transport or {}).get('playing')),
         'loopStart':_round(loop.get('start')),'loopEnd':_round(loop.get('end'))}
    for key in ('muted','soloed','mutedViaSolo','gainDb','monitoringState'):
        if track and key in track:out[key]=_round(track[key]) if isinstance(track.get(key),(int,float)) else track[key]
    return out

def signature(**parts):
    """One short key for a comparison: the sounds, the move, and the listening conditions."""
    blob=json.dumps(parts,sort_keys=True,default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:20]

class Cache:
    """Measured offsets, keyed by everything that would make them wrong."""
    def __init__(self,clock=time.time,stale_seconds=STALE_SECONDS):
        self.clock=clock;self.stale=stale_seconds;self._entries={};self.hits=0;self.misses=0
    def get(self,key):
        entry=self._entries.get(key)
        if not entry:
            self.misses+=1;return None
        if self.clock()-entry['at']>self.stale:
            self._entries.pop(key,None);self.misses+=1;return None
        self.hits+=1
        return dict(entry['value'],cached=True,measuredSecondsAgo=round(self.clock()-entry['at'],1))
    def put(self,key,value):
        self._entries[key]=({'at':self.clock(),'value':dict(value)})
        while len(self._entries)>MAX_ENTRIES:self._entries.pop(next(iter(self._entries)))
        return value
    def drop_all(self,why=''):
        """Everything measured is out of date: used when the patch or the session changes underneath."""
        count=len(self._entries);self._entries.clear()
        return {'dropped':count,'why':why}
    def stats(self):
        return {'entries':len(self._entries),'reused':self.hits,'measured':self.misses}
