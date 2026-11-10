"""Canonical MIDI: quarter-note beats, MIDI numbers, no executable model output."""
import json, math, re, hashlib, struct
from fractions import Fraction

def validate(notes, length):
    if type(length) not in (int, float) or not math.isfinite(length) or length <= 0:
        raise ValueError('Phrase length must be a positive number of quarter-note beats.')
    if not isinstance(notes, list) or not notes:
        raise ValueError('A variation must contain notes.')
    result = []
    for n in notes:
        if not isinstance(n, dict) or set(n) != {'p','t','d','v'}:
            raise ValueError('Each note needs exactly p, t, d and v.')
        if any(type(n[k]) not in (int,float) or not math.isfinite(n[k]) for k in n):
            raise ValueError('All MIDI values must be finite numbers.')
        p,t,d,v = (n[k] for k in ('p','t','d','v'))
        if p != int(p) or v != int(v) or not 0 <= p <= 127 or not 1 <= v <= 127:
            raise ValueError('Invalid MIDI pitch or velocity.')
        if t < 0 or d <= 0 or t+d > length+0.00001:
            raise ValueError('A note falls outside the phrase.')
        result.append(dict(p=int(p),t=round(t,8),d=round(d,8),v=int(v)))
    result.sort(key=lambda n:(n['t'],n['p'],n['d'],n['v']))
    if len({tuple(n.values()) for n in result}) != len(result):
        raise ValueError('Duplicate notes detected.')
    return result

# Stored 4/4 timeline notation (takes/journals keep these strings). The grammar
# matches what the host has historically reported: "bar|beat", decimal beats,
# and note-value offsets such as "3|2+n/8t"; durations "Nbar", "n3/16",
# "1bar+n/4". Values are quarter-note beats; unreadable text returns None.
_NOTE_VALUE = r'(\d+\.\d+|\d*)/(0|[1-9]\d*)([dt]?)'

def _note_value_beats(numerator, denominator, modifier):
    if int(denominator) == 0: return None
    value = (1.0 if numerator == '' else float(numerator)) / int(denominator) * 4
    return value * (1.5 if modifier == 'd' else 2/3 if modifier == 't' else 1)

def _whole(value):
    return int(round(value)) if abs(value - round(value)) < 1e-9 else value

def position_beats(text):
    m = re.fullmatch(r'(-?\d+)\|(-?\d+(?:\.\d+)?)(?:([+-])n' + _NOTE_VALUE + r')?', str(text or ''))
    if not m: return None
    beat = float(m[2])
    if m[3]:
        offset = _note_value_beats(m[4], m[5], m[6])
        if offset is None: return None
        beat += offset if m[3] == '+' else -offset
    beats = (int(m[1])-1)*4 + beat - 1
    return _whole(beats) if beats > -1e-9 else None

def duration_beats(text):
    m = re.fullmatch(r'(?:(0|[1-9]\d*)bars?(?:([+-])n' + _NOTE_VALUE + r')?|n' + _NOTE_VALUE + r')', str(text or ''))
    if not m: return None
    bars = int(m[1]) if m[1] is not None else 0
    if m[4] is not None:
        extra = _note_value_beats(m[3], m[4], m[5])
        if extra is not None and m[2] == '-': extra = -extra
    elif m[7] is not None:
        extra = _note_value_beats(m[6], m[7], m[8])
    else:
        extra = 0
    if extra is None: return None
    beats = bars*4 + extra
    return _whole(beats) if beats > 1e-9 else None

def _decimal(value):
    return ('%.6f' % value).rstrip('0').rstrip('.')

def position_text(beats):
    if type(beats) not in (int, float) or not math.isfinite(beats) or beats < 0:
        raise ValueError('Timeline positions must be finite, non-negative beats.')
    bar = int(beats//4) + 1
    return f"{bar}|{_decimal(beats-(bar-1)*4+1)}"

def duration_text(beats):
    if type(beats) not in (int, float) or not math.isfinite(beats) or beats <= 0:
        raise ValueError('Durations must be finite, positive beats.')
    bars = int((beats + 1e-9)//4); rest = round(beats - bars*4, 6)
    if rest <= 0: return f'{bars}bar'
    return (f'{bars}bar+' if bars else '') + f'n{_decimal(rest)}/4'

def fingerprint(notes):
    return hashlib.sha256(json.dumps(notes,sort_keys=True).encode()).hexdigest()

def compare(source, notes):
    src = {(n['t'],n['p'],n['d']) for n in source}
    same = sum((n['t'],n['p'],n['d']) in src for n in notes)
    pcs = {n['p']%12 for n in source}
    warnings=[]
    if len(notes) != len(source): warnings.append('Note density changed.')
    if any(n['p']%12 not in pcs for n in notes): warnings.append('New pitch classes: listen in harmonic context; not automatically an error.')
    if same/max(len(source),len(notes)) < .75: warnings.append('Less than 75% of the original note positions, pitches and lengths remain.')
    ordered=sorted(notes,key=lambda n:n['t'])
    if any(all(0 < ordered[j+1]['p']-ordered[j]['p'] <= 2 for j in range(i,i+3)) or
           all(-2 <= ordered[j+1]['p']-ordered[j]['p'] < 0 for j in range(i,i+3)) for i in range(len(ordered)-3)):
        warnings.append('A four-note staircase occurs; check against your brief.')
    return dict(unchangedPercent=round(100*same/max(len(source),len(notes))),noteCount=len(notes),warnings=warnings)

def notation(notes):
    def f(x): return str(Fraction(x).limit_denominator(96000))
    def pitch(p): return ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'][p%12]+str(p//12-2)
    return '\n'.join(f"v{n['v']} n{f(n['d']/4)} {pitch(n['p'])} {int(n['t']//4)+1}|1+n{f(n['t']%4/4)}" for n in notes)

def midi_bytes(notes,length,tempo=137):
    def vlq(n):
        b=[n&127]; n >>= 7
        while n: b.insert(0,(n&127)|128); n >>= 7
        return bytes(b)
    events=[]
    for n in notes:
        events.extend([(round(n['t']*960),1,bytes([0x90,n['p'],n['v']])),
                       (round((n['t']+n['d'])*960),0,bytes([0x80,n['p'],0]))])
    data=b'\x00\xff\x51\x03'+round(60000000/tempo).to_bytes(3,'big')+b'\x00\xff\x58\x04\x04\x02\x18\x08'
    last=0
    for tick,_,event in sorted(events): data+=vlq(tick-last)+event; last=tick
    data+=vlq(max(0,round(length*960)-last))+b'\xff\x2f\x00'
    return b'MThd'+struct.pack('>IHHH',6,0,1,960)+b'MTrk'+struct.pack('>I',len(data))+data
