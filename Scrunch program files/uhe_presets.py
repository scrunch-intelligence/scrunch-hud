"""Read-only u-he preset library (Diva `.h2p`): identity, categories, character and settings.

An `.h2p` file is text: a `/*@Meta ... */` header of quoted fields, then `#cm=<section>` blocks
of `Key=value` lines. A plug-in's saved state in a Live Set is the same text behind a 4-byte
little-endian length, so a preset can be written into a Set without touching the plug-in's UI.

Categories and character words are the vendor's, not measured sound.
"""
from pathlib import Path
import re,struct

ROOT=Path('/Library/Audio/Presets/u-he/Diva')
MAX_BYTES=4*1024*1024
# Diva string-table references and revision stamp: Diva renumbers these whenever it writes its state
# (observed 24 Sep 2026: preset 2,3,4,5,6,7 -> 5..10 after loading), so they are not sound settings.
RENUMBERED=frozenset({'PCore:Rev','PCore:MidiA','PCore:MidiP','PCore:DFold','VCC:MTunN','VCC:MTunT','OPT:TPots'})
META=('Bank','Author','Description','Usage','Categories','Features','Character')

def parse(data):
    """Preset bytes -> {name, author, description, categories, features, character, parameters}."""
    if len(data)>MAX_BYTES:raise ValueError('u-he preset is too large.')
    text=data.decode('latin-1')
    if '#AM=' not in text:raise ValueError('Not a u-he preset.')
    meta={}
    header=text.split('*/',1)[0] if text.lstrip().startswith('/*@Meta') else ''
    for field in META:
        m=re.search(r"^%s:\s*\n'(.*?)'"%field,header,re.S|re.M)
        if m:meta[field.lower()]=' '.join(m.group(1).split())
    section=None;parameters={}
    for line in text.split('\n'):
        line=line.strip()
        if line.startswith('#cm='):section=line[4:]
        elif '=' in line and not line.startswith(('#','/','*')) and section:
            key,_,value=line.partition('=')
            try:parameters[section+':'+key]=float(value)
            except ValueError:continue
    if not parameters:raise ValueError('u-he preset has no settings.')
    split=lambda k:[w.strip() for w in meta.get(k,'').split(',') if w.strip()]
    return {'name':meta.get('bank',''),'author':meta.get('author',''),'notes':meta.get('description',''),
            'usage':meta.get('usage',''),'categories':split('categories'),'features':split('features'),
            'character':split('character'),'parameters':parameters}

def load(path,root=ROOT):
    p=Path(path).resolve();root=Path(root).resolve()
    if not p.is_relative_to(root) or p.suffix.lower()!='.h2p' or not p.is_file():
        raise ValueError('Preset is outside the local Diva library.')
    d=parse(p.read_bytes());d['path']=str(p);d['name']=p.stem
    parts=p.relative_to(root).parts
    d['folder']=parts[0] if len(parts)>1 else ''
    d['type']=re.sub(r'^\d+\s+','',d['folder']).title() or (d['categories'][0].split(':')[0] if d['categories'] else '')
    d['subtype']=d['categories'][0].split(':')[-1] if d['categories'] else d['type']
    return d

def library(root=ROOT):
    """All Diva presets, sorted by path; unreadable files are listed rather than hidden."""
    presets,errors=[],[]
    for p in sorted(Path(root).rglob('*.h2p')):
        try:presets.append(load(p,root))
        except ValueError as e:errors.append({'path':str(p),'error':str(e)})
    return presets,errors

def state(preset_bytes):
    """A Live plug-in state carrying this preset: 4-byte little-endian length, then the preset text."""
    return struct.pack('<I',len(preset_bytes))+bytes(preset_bytes)

def state_preset(state_bytes):
    """The preset text out of a saved Diva state, refusing a state whose length header does not fit."""
    if len(state_bytes)<5:raise ValueError('Diva state is too short.')
    size=struct.unpack('<I',bytes(state_bytes[:4]))[0]
    body=bytes(state_bytes[4:])
    if size>len(body):raise ValueError('Diva state header claims %d bytes but holds %d.'%(size,len(body)))
    return body[:size]
