"""Acting on a measured collision, in Live's own EQ Eight.

`scope_collision.advise` says *cut the kick 3 dB at 400 Hz, Q 4.3*. This is the half that
does it, and then proves it was done by reading Live's own display back.

Why EQ Eight and not Pro-Q 4: it is native, everyone has it, and it is settable today with no
Set rewrite. Pro-Q exposes only `Device On` until its parameter list is rewritten. The moat is
the decision, not the filter - and a visible EQ band is one John can grab and overrule.

Two things worth knowing about the way this reaches the device:

  * **Name-based writes are refused for EQ Eight.** The bridge inherits Producer Pal's
    SPECIALIZED_DEVICES list (Compressor, EQ Eight, Auto Filter and nine others), so
    `set_parameters_by_name` will not touch it. Parameters are addressed by id instead, which
    has no such guard. `NAME_BASED_REFUSED` records that, because the measured ducker will meet
    the same wall on Compressor.
  * **Frequency and Q are not in Hz and Q.** Live carries them as 0..1 over a log range. The two
    curves below are exact, not fitted: FREQ reproduces Live's own 200 Hz band default to six
    decimals, and Q reproduces its 0.7071 default. Verified against Live's display in
    `test_eq_eight`.

It will only ever write to an EQ Eight it made itself (`OWNED_PREFIX`). A producer's own EQ curve
is their work, and a band they placed by ear is not ours to move.
"""
import math
import chain_position

NAME_BASED_REFUSED=('Compressor','Drift','EQ Eight','Hybrid Reverb','Meld','Auto Filter',
                    'Auto Pan-Tremolo','Phaser-Flanger','Roar','Simpler','Spectral Resonator','Wavetable')

OWNED_PREFIX='Scrunch EQ'
BANDS=8
FREQ_MIN_HZ=10.0        # value 0.0
FREQ_SPAN=2200.0        # value 1.0 is FREQ_MIN_HZ * FREQ_SPAN = 22 kHz
Q_MIN=0.1               # value 0.0
Q_SPAN=180.0            # value 1.0 is Q_MIN * Q_SPAN = 18.0
GAIN_LIMIT=15.0         # EQ Eight's own dB range, +-15
BELL='Bell'

def hz_to_value(hz):
    """Hz -> the 0..1 a Frequency parameter actually carries."""
    if not isinstance(hz,(int,float)) or not math.isfinite(hz) or hz<=0:
        raise ValueError('Frequency must be a positive number of Hz.')
    value=math.log10(hz/FREQ_MIN_HZ)/math.log10(FREQ_SPAN)
    if not -1e-9<=value<=1+1e-9:
        raise ValueError('%g Hz is outside EQ Eight\'s %g Hz..%g Hz range.'%(hz,FREQ_MIN_HZ,FREQ_MIN_HZ*FREQ_SPAN))
    return min(1.0,max(0.0,value))

def value_to_hz(value):
    return FREQ_MIN_HZ*FREQ_SPAN**value

def q_to_value(q):
    """Q -> the 0..1 a Resonance parameter actually carries, bounded to what EQ Eight has."""
    if not isinstance(q,(int,float)) or not math.isfinite(q) or q<=0:
        raise ValueError('Q must be a positive number.')
    return min(1.0,max(0.0,math.log10(min(Q_MIN*Q_SPAN,max(Q_MIN,q))/Q_MIN)/math.log10(Q_SPAN)))

def value_to_q(value):
    return Q_MIN*Q_SPAN**value

def band_names(band,side='A'):
    """The five parameter names of one band. Bands are 1..8; A and B are the two filter slots."""
    if band not in range(1,BANDS+1):raise ValueError('EQ Eight has bands 1..%d.'%BANDS)
    if side not in ('A','B'):raise ValueError('A band slot is A or B.')
    return {k:'%d %s %s'%(band,label,side) for k,label in
            (('on','Filter On'),('type','Filter Type'),('hz','Frequency'),('db','Gain'),('q','Resonance'))}

def _params(device):
    return {p['name']:p for p in device.get('parameters',()) if p.get('name')}

def owned(device):
    """Is this an EQ Eight that Scrunch made, and may therefore rewrite?"""
    return (device.get('deviceName')=='EQ Eight' or device.get('type')=='audio-effect: EQ Eight') \
        and str(device.get('name') or '').startswith(OWNED_PREFIX)

def plan(move,band,parameters):
    """The writes that put one collision move on one band, in the two stages Live requires.

    `move` is a `scope_collision.advise` move: hz, byDb (a cut, so negative), q.

    Returns `(enable, settings)`. They cannot be one write: while a band is switched off EQ Eight
    reports its frequency, gain and Q as *not enabled* ("state irrelevant"), and the grouped write
    refuses the lot rather than half-applying it. So the band is switched on first and its four
    settings land together on the next tick. Nothing is heard in between - a band's default is a
    Bell at 0 dB, which is a straight wire.
    """
    if not isinstance(move,dict) or move.get('do')!='cut':
        raise ValueError('Only a cut is applied to an EQ band; a boost is a musical choice, not a collision fix.')
    db=move.get('byDb')
    if not isinstance(db,(int,float)) or not math.isfinite(db) or db>=0:
        raise ValueError('A collision cut must be a negative number of dB.')
    if abs(db)>GAIN_LIMIT:
        raise ValueError('%.1f dB is beyond EQ Eight\'s %.0f dB range.'%(db,GAIN_LIMIT))
    names=band_names(band)
    missing=[n for n in names.values() if n not in parameters]
    if missing:raise ValueError('This device has no %s; it is not an EQ Eight.'%missing[0])
    types=parameters[names['type']].get('options') or ()
    if BELL not in types:raise ValueError('This EQ has no %s filter type.'%BELL)
    def write(key,value):return {'id':parameters[names[key]]['id'],'name':names[key],'value':value}
    enable=[write('on',1.0)]
    settings=[write('type',float(types.index(BELL)+parameters[names['type']].get('min',0))),
              write('hz',hz_to_value(move['hz'])),
              write('db',float(db)),
              write('q',q_to_value(move.get('q') or 1.0))]
    return enable,settings

def free_band(parameters):
    """The lowest band with its A filter switched off - a band we have not already spent."""
    for band in range(1,BANDS+1):
        names=band_names(band)
        p=parameters.get(names['on'])
        if p is not None and not p.get('value'):return band
    return None

def find(track,adapter):
    """The Scrunch-owned EQ Eight on this track, if there is one."""
    for device in adapter.read_track(track).get('devices',()) or ():
        full=adapter.device(device['id'])
        if owned(full):return full
    return None

def ensure(track,track_index,adapter,name=None):
    """The owned EQ Eight on this track, made if it is not there yet, *in front of the tap*.

    Two things this has to get right.

    **Position.** Appending puts the EQ behind the Scope plug-in, which taps at its own device
    position - so the cut is inaudible to measurement and cannot be verified. The EQ goes in front of
    every tap on the track (`chain_position`). If this Live version will not place a device, the
    bridge refuses rather than appending to the wrong place, and that refusal is passed on with the
    reason: an EQ that cannot be verified is worse than no EQ.

    **No parameters at creation.** The bridge's name-based write is refused for EQ Eight, so
    parameters go on afterwards by id.
    """
    found=find(track,adapter)
    if found:return found,False
    devices=adapter.read_track(track).get('devices',()) or ()
    at=chain_position.insert_at(devices)
    created=adapter.create_native_effect(track_index,'EQ Eight',name or OWNED_PREFIX+' collision',None,at=at)
    full=adapter.device(created['id'])
    if not owned(full):
        raise ValueError('Created device is not a Scrunch EQ Eight; it has been left alone for inspection.')
    full['placement']=chain_position.describe(devices,at)
    return full,True

def apply(move,track,track_index,adapter):
    """Put one measured collision move on this track's EQ, and return what Live read back.

    The move is applied to a band nothing has spent yet, so repeated advice stacks up as separate
    bands instead of overwriting the last decision.
    """
    device,created=ensure(track,track_index,adapter)
    parameters=_params(device)
    band=free_band(parameters)
    if band is None:
        raise ValueError('All %d bands of %r are in use; nothing was changed.'%(BANDS,device.get('name')))
    enable,settings=plan(move,band,parameters)          # validated before the first write
    adapter.set_device_parameters_together(device['id'],enable)
    written=adapter.set_device_parameters_together(device['id'],settings)
    read={p['name']:p for p in written}
    names=band_names(band)
    return {'deviceId':device['id'],'device':device.get('name'),'band':band,'created':created,
            'track':track,'move':move,'placement':device.get('placement'),
            'verified':{'hz':read[names['hz']].get('display'),'db':read[names['db']].get('display'),
                        'q':read[names['q']].get('display'),'type':read[names['type']].get('display')},
            'say':'%s: band %d is a Bell at %s, %s, Q %s.'
                  %(device.get('name'),band,read[names['hz']].get('display'),
                    read[names['db']].get('display'),read[names['q']].get('display'))}
