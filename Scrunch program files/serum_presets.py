"""Read-only local Serum 2 preset catalogue. Never loads or rewrites presets.

Only the bounded XferJson metadata header is read. Payloads are not interpreted.
Names/tags are vendor metadata, not verified sonic characteristics/instructions.
"""
from pathlib import Path
import json,hashlib
ROOT=Path('/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets')
MAX_HEADER=65536

def metadata(path,root=ROOT):
    p=Path(path).resolve();root=Path(root).resolve()
    if not p.is_relative_to(root) or p.suffix.lower()!='.serumpreset':raise ValueError('Preset is outside the supported local library.')
    with p.open('rb') as f:
        prefix=f.read(17)
        if len(prefix)!=17 or prefix[:9]!=b'XferJson\0':raise ValueError('Unsupported Serum preset header.')
        n=int.from_bytes(prefix[9:17],'little')
        if not 0<n<=MAX_HEADER:raise ValueError('Invalid preset metadata length.')
        raw=f.read(n)
    if len(raw)!=n:raise ValueError('Truncated preset metadata.')
    d=json.loads(raw)
    if d.get('fileType')!='SerumPreset' or d.get('product')!='Serum2':raise ValueError('Not a Serum 2 preset.')
    def text(k):return d.get(k,'')[:500] if isinstance(d.get(k,''),str) else ''
    tags=d.get('tags',[])
    return {'id':hashlib.sha256(str(p.relative_to(root)).encode()).hexdigest()[:20],
        'name':text('presetName') or p.stem,'author':text('presetAuthor'),
        'description':text('presetDescription'),'tags':[x[:80] for x in tags[:30] if isinstance(x,str)] if isinstance(tags,list) else [],
        'relativePath':str(p.relative_to(root)),'headerSha256':hashlib.sha256(raw).hexdigest(),
        'loadVerified':False,'basis':'Local vendor metadata; sound not auditioned'}

def catalogue(root=ROOT):
    root=Path(root);items=[];errors=[]
    if not root.is_dir():return {'presets':[],'errors':['Serum preset library unavailable.']}
    for p in sorted(root.rglob('*.SerumPreset')):
        try:items.append(metadata(p,root))
        except (OSError,ValueError,TypeError) as exc:errors.append({'file':str(p.relative_to(root)),'error':str(exc)})
    return {'presets':items,'errors':errors}

def search(query,limit=12,root=ROOT):
    words=str(query).casefold().split();c=catalogue(root)
    rows=[x for x in c['presets'] if all(w in (' '.join([x['name'],x['description'],x['relativePath']]+x['tags'])).casefold() for w in words)]
    return {'matches':rows[:max(1,min(int(limit),30))],'totalMatches':len(rows),'catalogueCount':len(c['presets']),'errors':c['errors'],
        'capability':'Read-only catalogue. Preset loading is not an Engineer action yet.'}
