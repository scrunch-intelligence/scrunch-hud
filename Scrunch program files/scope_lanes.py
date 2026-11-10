"""Tell the Scope plug-in what only the host knows: which track a lane is, and in what order.

The plug-in can read its own audio and its own window title, but it cannot see the Set. Scrunch
can. This maps each published lane to a Live track by the name the host gave it, and writes the
track's position and colour into the lane's shared header - so the window stacks its lanes in the
order of the Set and paints them in Live's own colours.

It writes **only** those two fields, into a header whose layout is fixed on both sides
(`native/scope/scope_shared.hpp`). The audio, the label and the liveness flag belong to the
plug-in and are never touched from here.
"""
import mmap,os,re,struct
from pathlib import Path

MAGIC=0x53534350
ABI=2
LABEL=64

# Offsets are the contract with scope_shared.hpp. Fields are appended there, never moved, and the
# numbers below come from `native/scope/offsets.cpp`, which prints them from the real struct.
OFF_MAGIC=0; OFF_ABI=4; OFF_BYTES=8; OFF_PID=16; OFF_COLOUR=20; OFF_ORDER=24; OFF_LABEL=32; OFF_ALIVE=96
HEADER=128
# The audio itself, so Scrunch can measure exactly what the window is drawing.
OFF_RING=128
RING_WRITTEN=0; RING_RATE=8; RING_HOSTTIME=16; RING_CHANNELS=24; RING_SAMPLES=28
RING_LENGTH=768000; RING_CHANNELS_COUNT=2
BLOCK_BYTES=OFF_RING+RING_SAMPLES+RING_LENGTH*RING_CHANNELS_COUNT*4

def folder(uid=None):
    return Path('/tmp/scrunch-scope-%d'%(os.getuid() if uid is None else uid))

def published(uid=None):
    """Every lane the Scope plug-in is currently publishing."""
    root=folder(uid)
    if not root.is_dir():return []
    return sorted(p.name for p in root.iterdir() if p.name.startswith('ss_'))

def samples(name,seconds=2.0,opener=None):
    """The newest audio from a lane, as it is in the plug-in right now.

    Read straight out of the plug-in's own ring - the same memory the window draws from - so what
    Scrunch measures is what the producer is looking at, with nothing copied through a server or
    a second capture. The ring has no read cursor, so reading here takes nothing away from the
    window or from any other reader.
    """
    import numpy as np
    with (opener or _map)(name,BLOCK_BYTES) as block:
        magic,abi=struct.unpack_from('<II',block,OFF_MAGIC)
        if magic!=MAGIC or abi!=ABI:raise ValueError('%s is not a Scope lane Scrunch can read.'%name)
        written,=struct.unpack_from('<Q',block,OFF_RING+RING_WRITTEN)
        rate,=struct.unpack_from('<d',block,OFF_RING+RING_RATE)
        rate=rate or 48000.0
        want=int(min(RING_LENGTH,max(1,seconds*rate),written))
        if not want:return np.zeros((0,2),dtype=np.float32),rate
        audio=np.empty((want,2),dtype=np.float32)
        for channel in range(2):
            base=OFF_RING+RING_SAMPLES+channel*RING_LENGTH*4
            flat=np.frombuffer(block,dtype=np.float32,count=RING_LENGTH,offset=base)
            first=(written-want)%RING_LENGTH
            end=first+want
            audio[:,channel]=flat[first:end] if end<=RING_LENGTH else \
                np.concatenate((flat[first:],flat[:end-RING_LENGTH]))
            # The view must go before the mapping closes, or the shared block stays open: the
            # copy above is what Scrunch keeps, and the plug-in's memory is released cleanly.
            del flat
        return audio,rate

class _Block:
    """A lane's shared header, open for the moment it takes to read or write it.

    Python's shared-memory helper registers everything it opens with a resource tracker that
    **unlinks** the segment when this process exits. That would destroy a lane the plug-in is
    still publishing into, so the registration is dropped: Scrunch is a visitor here, and the
    plug-in owns the memory.
    """
    def __init__(self,name,size=None):
        from multiprocessing import shared_memory,resource_tracker
        self.shm=shared_memory.SharedMemory(name=name.lstrip('/'))
        self.size=size
        try:resource_tracker.unregister(self.shm._name,'shared_memory')
        except Exception:pass
    def __enter__(self):return self.shm.buf
    def __exit__(self,*_):
        try:self.shm.close()
        except Exception:pass
        return False

def _map(name,size=None):
    return _Block(name,size)

def read(name,opener=None):
    """A lane's header: who it is, where it sits, and whether its plug-in is still there."""
    with (opener or _map)(name) as block:
        magic,abi=struct.unpack_from('<II',block,OFF_MAGIC)
        if magic!=MAGIC:raise ValueError('%s is not a Scope lane.'%name)
        if abi!=ABI:raise ValueError('%s speaks version %d; Scrunch speaks %d. Rebuild the plug-in.'%(name,abi,ABI))
        pid,colour=struct.unpack_from('<II',block,OFF_PID)
        order,=struct.unpack_from('<i',block,OFF_ORDER)
        label=bytes(block[OFF_LABEL:OFF_LABEL+LABEL]).split(b'\0',1)[0].decode('utf-8','replace')
        alive,=struct.unpack_from('<I',block,OFF_ALIVE)
        return {'stream':name,'pid':pid,'colour':colour or None,'order':None if order<0 else order,
                'label':label,'alive':bool(alive)}

def write(name,order=None,colour=None,opener=None):
    """Set a lane's place in the Set and its colour. Nothing else in the header is touched."""
    with (opener or _map)(name) as block:
        magic,abi=struct.unpack_from('<II',block,OFF_MAGIC)
        if magic!=MAGIC or abi!=ABI:raise ValueError('%s is not a Scope lane Scrunch can write to.'%name)
        if order is not None:struct.pack_into('<i',block,OFF_ORDER,int(order))
        if colour is not None:struct.pack_into('<I',block,OFF_COLOUR,int(colour)&0xFFFFFF)
    return read(name,opener)

def match(lanes,tracks):
    """Pair each lane with the Live track whose name it carries.

    The plug-in takes its label from the host's own window title, so the join is on that name.
    A lane whose name matches nothing, or matches more than one track, is left alone and
    reported - a scope that mislabels a lane is worse than one that leaves it blank.
    """
    by_name={}
    for index,track in enumerate(tracks):
        by_name.setdefault(_plain(track.get('name')),[]).append((index,track))
    paired,unmatched=[],[]
    for lane in lanes:
        found=by_name.get(_plain(lane.get('label')),[])
        if len(found)!=1:
            unmatched.append({**lane,'why':'no track called %r'%lane.get('label') if not found
                              else '%d tracks are called %r'%(len(found),lane.get('label'))})
            continue
        index,track=found[0]
        paired.append({'lane':lane,'trackIndex':index,'track':track})
    return paired,unmatched

def _plain(name):
    """Track names as they compare: Live shows '1 ETB_Kick_51' where the window says '1-ETB_Kick_51'."""
    return re.sub(r'[^a-z0-9]+','',str(name or '').lower())

def sync(tracks,opener=None,lanes=None):
    """Give every lane its track's order and colour. Returns what was set and what was not."""
    found=[]
    for name in (lanes if lanes is not None else published()):
        try:found.append(read(name,opener))
        except (ValueError,OSError) as e:found.append({'stream':name,'label':'','error':str(e)})
    live=[lane for lane in found if lane.get('alive') and not lane.get('error')]
    paired,unmatched=match(live,tracks)
    done=[]
    for pair in paired:
        colour=pair['track'].get('color')
        updated=write(pair['lane']['stream'],order=pair['trackIndex'],
                      colour=colour if isinstance(colour,int) and colour else None,opener=opener)
        done.append({'stream':updated['stream'],'label':updated['label'],
                     'order':updated['order'],'colour':updated['colour'],
                     'trackIndex':pair['trackIndex']})
    return {'lanes':len(found),'named':done,'unmatched':unmatched,
            'gone':[l['stream'] for l in found if not l.get('alive') and not l.get('error')],
            'errors':[l for l in found if l.get('error')]}

# ---- what the lanes mean, for the engineer to act on ----

def loudness(audio,rate):
    """EBU R128 for this stretch of a lane: the same library the plug-in's own panel uses.

    The plug-in integrates from when its window opened; this integrates over the seconds Scrunch
    just read. They are the same measurement of the same audio over different spans, so the two
    will not agree to the decimal and should not be presented as if they were one number - which
    is why this says how long it covered.
    """
    import numpy as np
    if audio is None or len(audio)<int(rate*0.4):return None
    try:
        import reference_dsp
        out=reference_dsp.meter(np.ascontiguousarray(audio,dtype=np.float64),int(rate))
    except Exception as e:
        return {'error':str(e)}
    return {'integratedLUFS':out.get('integratedLUFS'),'truePeakDbTP':out.get('truePeakDbTP'),
            'loudnessRangeLU':out.get('loudnessRangeLU'),'overSeconds':round(len(audio)/rate,2),
            'basis':'EBU R128 (libebur128), over the seconds read — not the plug-in\u2019s running integration'}

def measure(seconds=2.0,names=None,opener=None,tracks=None):
    """Every lane, measured: level, spectrum, mono compatibility — and how each pair sits together.

    The measuring is the same code the browser scope used and the same 22 tests cover it
    (`scope_dsp`). What changed is where the audio comes from: the native plug-in's own ring,
    which is also what the producer is watching. Nothing is measured twice and nothing is
    captured separately, so the numbers and the picture cannot disagree.
    """
    import scope_dsp
    lanes=[]
    for name in (names if names is not None else published()):
        try:
            head=read(name,opener)
            if not head['alive']:continue
            audio,rate=samples(name,seconds,opener)
        except (ValueError,OSError) as e:
            lanes.append({'stream':name,'error':str(e)});continue
        lanes.append({**head,'sampleRate':rate,'seconds':round(len(audio)/rate,3) if rate else 0,
                      **scope_dsp.level(audio),'bands':scope_dsp.spectrum(audio,rate),
                      'mono':scope_dsp.mono_compatibility(audio,rate),
                      'loudness':loudness(audio,rate),'_audio':audio})
    ready=[lane for lane in lanes if '_audio' in lane]
    ready.sort(key=lambda lane:(lane['order'] if lane['order'] is not None else 99,lane['label']))
    pairs=[]
    for i,first in enumerate(ready):
        for second in ready[i+1:]:
            rate=first['sampleRate'] or 48000
            pairs.append(scope_dsp.relationship(first['_audio'],second['_audio'],rate,
                                                names=(first['label'] or first['stream'],
                                                       second['label'] or second['stream'])))
    import scope_collision
    clashes=[]
    for i,first in enumerate(ready):
        for second in ready[i+1:]:
            rate=first['sampleRate'] or 48000
            clashes.append(scope_collision.advise(first['_audio'],second['_audio'],rate,
                                                  names=(first['label'] or first['stream'],
                                                         second['label'] or second['stream'])))
    for lane in lanes:lane.pop('_audio',None)
    return {'lanes':lanes,'pairs':pairs,'collisions':clashes,'seconds':seconds,
            'source':'Scrunch Scope plug-in, read from its own audio'}
