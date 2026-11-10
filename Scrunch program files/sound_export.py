"""Write a sound designed in the coach back out as a real preset, in the plug-in's own library.

Everything Scrunch changes goes through exposed knobs, and every exposed knob is one of the
settings the preset file itself stores. So the sound in front of you is exactly: the preset it
started from, with the current value of each exposed knob written over it. That is a complete,
valid preset file - no plug-in UI, no Set surgery, nothing recreated by hand.

Files are written into the instrument's **User** folder and never on top of something that is
already there. What is written is read back and compared setting by setting before it is
reported as saved.
"""
from pathlib import Path
import re,unicodedata
import arturia_presets as ap

ARTURIA_ROOT=ap.ROOT
UHE_ROOT=Path('/Library/Audio/Presets/u-he/Diva')            # where the factory library is read from
UHE_USER_ROOT=Path.home()/'Library'/'Audio'/'Presets'/'u-he'/'Diva'   # where u-he looks for the producer's own
SCRUNCH_FOLDER='Scrunch'
MAX_NAME=64

def safe_name(name):
    """A file name that is the producer's name, minus anything a file system would argue with."""
    text=unicodedata.normalize('NFC',str(name)).strip()
    text=re.sub(r'[\\/:*?"<>|\x00-\x1f]','',text)
    text=re.sub(r'\s+',' ',text).strip(' .')
    if not text:raise ValueError('Give the sound a name.')
    return text[:MAX_NAME]

# ---- Arturia (Jup-8000 V, Pigments) ----

def set_parameters(state,values):
    """A copy of an Arturia state with these settings changed; refuses any the state does not have."""
    ap.parse(state)
    prefix,items,suffix=ap._blocks(state.decode('latin-1'))
    have=dict(items)
    unknown=[k for k in values if k not in have]
    if unknown:raise ValueError('This preset has no setting called %s.'%', '.join(sorted(unknown)[:4]))
    merged=[(k,('%.8g'%float(values[k])) if k in values else v) for k,v in items]
    body=' '.join('%d %s %s'%(len(k),k,v) for k,v in merged)
    out=(prefix+' 0 0 0 7 0 0 0 0 0 0 %d 0 0 0 '%len(merged)+body+' '+suffix).encode('latin-1')
    ap.parse(out)
    return out

def _field(text,index):
    """Where the `index`-th header string sits: (start, end, whether a space followed it)."""
    r=ap._Reader(text)
    if r.str()!='serialization::archive':raise ValueError('Not an Arturia preset archive.')
    for _ in range(5):r.int()
    for n in range(index+1):
        start=r.i;value=r.str();end=r.i
    spaced=text[end-1:end]==' ' or text[start:end].endswith(' ')
    return start,end,value,spaced

def rename(state,name,pack=None):
    """A copy of an Arturia state carrying a new preset name, and optionally a new pack name."""
    text=state.decode('latin-1')
    for index,value in ((1,pack),(0,name)):        # pack first: renaming the name would move it
        if value is None:continue
        start,end,_,spaced=_field(text,index)
        text=text[:start]+'%d %s'%(len(value),value)+(' ' if spaced else '')+text[end:]
    out=text.encode('latin-1')
    if ap.parse(out)['name']!=name:raise ValueError('The preset name was not written as asked.')
    return out

def arturia_preset(base,values,name,pack=None):
    """The bytes of a real Arturia preset: `base` with these settings and this name."""
    return rename(set_parameters(base,values),safe_name(name),pack)

def container(instrument,root=None):
    """A factory preset to build a file around when the sound did not come from one.

    Working on a synth in the producer's own project, Scrunch has no idea which preset that
    track started life as - it may not have been one. A preset file still needs a container:
    its format, and the fields the browser reads. A factory preset of the same instrument
    supplies that, and every value in it is then replaced by what the plug-in is actually
    playing. What it cannot know is what *kind* of sound it is, so it says which container it
    used rather than pretending the label is meaningful.
    """
    folder=Path(root or ARTURIA_ROOT)/instrument/'Factory'/'Factory'
    files=sorted(p for p in folder.glob('*') if p.is_file())
    if not files:raise ValueError('Scrunch found no %s factory preset to build a preset file around.'%instrument)
    for p in files:
        if p.name.lower()=='default':return p
    return files[0]

def arturia_path(instrument,name,root=None):
    folder=Path(root or ARTURIA_ROOT)/instrument/'User'/'User'
    return folder/safe_name(name)

# ---- u-he (Diva) ----

def diva_preset(base_text,values,name):
    """The text of a real `.h2p`: `base_text` with these `section:Key` settings changed."""
    lines=base_text.split('\n');section=None;seen=set()
    for i,line in enumerate(lines):
        stripped=line.strip()
        if stripped.startswith('#cm='):section=stripped[4:];continue
        if '=' not in stripped or stripped.startswith(('#','/','*')) or not section:continue
        key=section+':'+stripped.partition('=')[0]
        if key in values:
            lines[i]='%s=%s'%(stripped.partition('=')[0],('%.6g'%float(values[key])));seen.add(key)
    missing=[k for k in values if k not in seen]
    if missing:raise ValueError('This preset has no setting called %s.'%', '.join(sorted(missing)[:4]))
    return '\n'.join(lines)

def diva_path(name,root=None,folder=SCRUNCH_FOLDER):
    """Where a Diva sound is saved: u-he scans folders, so a file here is all the browser needs."""
    return Path(root or UHE_USER_ROOT)/folder/(safe_name(name)+'.h2p')

# ---- writing, and proving what was written ----

def write(path,data,values=None,reader=None):
    """Write a preset that is not already there, then read it back and check every setting.

    Never overwrites: a name already in the library is refused, with the path, so the producer
    chooses. Returns what was verified, not merely what was attempted.
    """
    path=Path(path)
    if path.exists():raise ValueError('There is already a preset called %r at %s.'%(path.name,path.parent))
    path.parent.mkdir(parents=True,exist_ok=True)
    payload=data if isinstance(data,bytes) else str(data).encode('latin-1')
    tmp=path.with_name(path.name+'.scrunch-tmp')
    tmp.write_bytes(payload);tmp.replace(path)
    checked=0
    if values and reader:
        stored=reader(path)
        wrong=[k for k,v in values.items() if abs(float(stored.get(k,float('nan')))-float(v))>5e-6]
        if wrong:
            path.unlink(missing_ok=True)
            raise ValueError('The saved preset did not read back correctly (%s); nothing was left behind.'
                             %', '.join(sorted(wrong)[:4]))
        checked=len(values)
    return {'path':str(path),'name':path.name,'bytes':len(payload),'settingsVerified':checked}

# ---- from the plug-in's real state in the Set ----

def carried(base,state):
    """Which of a preset's settings the Set's live state can fill in, and which it cannot.

    A host state stores editor extras a preset file has no place for, and a preset can name a
    setting the state does not carry. Only the overlap is written; both remainders are reported
    rather than quietly dropped.
    """
    ap.parse(base);values=ap.parse(state)['parameters']
    have={k for k,_ in ap._blocks(base.decode('latin-1'))[1]}
    return ({k:v for k,v in values.items() if k in have},
            sorted(k for k in values if k not in have),
            sorted(k for k in have if k not in values))

def arturia_from_state(base,state,name,pack=None):
    """A real preset file carrying the sound the plug-in is making now.

    `base` supplies the container and what kind of sound it is (type, subtype, tags); every
    value comes from `state`, the plug-in's own saved state, so nothing is recreated by hand.
    """
    values,extra,unfilled=carried(base,state)
    data=rename(set_parameters(base,values),safe_name(name),pack)
    return data,{'settingsCarried':len(values),'stateOnly':len(extra),'presetOnly':unfilled}
