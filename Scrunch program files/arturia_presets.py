"""Read-only Arturia preset library: preset settings, vendor tags, the user's likes, sound-type profiles.

Never loads, rewrites or re-saves presets and never opens Arturia's live database:
likes are read from a private temporary copy of db.db3 (with its -wal/-shm, where
recent likes live). Types, tags and notes are vendor metadata; profiles describe
how presets are *set*, not verified sound. Parameter values are the preset's own
stored units (mostly 0..1), not Live's parameter units.
"""
from pathlib import Path
import re,json,shutil,sqlite3,statistics,tempfile,hashlib
import xml.etree.ElementTree as ET
ROOT=Path('/Library/Arturia/Presets')
PLUGINS=Path('/Library/Arturia')
MAX_BYTES=4*1024*1024
TAG_GROUPS=('Characteristics','Genres','Styles')
_PARAMS=re.compile(r' 0 0 0 7 0 0 0 0 0 0 (\d+) 0 0 0 ')

class _Reader:
    """Sequential reader for Boost-style text archives: ints and length-prefixed strings."""
    def __init__(self,text):self.t,self.i=text,0
    def int(self):
        m=re.compile(r'(-?\d+) ?').match(self.t,self.i)
        if not m:raise ValueError('Unsupported Arturia preset archive.')
        self.i=m.end();return int(m.group(1))
    def str(self):
        n=self.int();s=self.t[self.i:self.i+n]
        if len(s)!=n:raise ValueError('Truncated Arturia preset archive.')
        self.i+=n
        if self.t[self.i:self.i+1]==' ':self.i+=1
        return s

def _meta(text,key):
    m=re.search(r' %d %s (\d+) '%(len(key),re.escape(key)),text)
    return text[m.end():m.end()+int(m.group(1))] if m else ''

def parse(data):
    """Preset bytes -> {name, pack, designer, notes, type, subtype, tags, parameters}."""
    if len(data)>MAX_BYTES:raise ValueError('Arturia preset is too large.')
    t=data.decode('latin-1');r=_Reader(t)
    if r.str()!='serialization::archive':raise ValueError('Not an Arturia preset archive.')
    for _ in range(5):r.int()
    name,pack=r.str(),r.str();layout=r.int();designer=r.str()
    inline_type,inline_tags,notes='',[],''
    if layout==98:                 # current layout: an extra field, six flags, then designer notes
        r.str()
        for _ in range(6):r.int()
        notes=r.str()
    elif layout==22:               # older layout (seen in Pigments factory): type, flags, then a tag list
        inline_type=r.str();r.int();r.int()
        count=r.int();r.int()      # collection size, then its item version
        inline_tags=[r.str() for _ in range(count)]
    else:raise ValueError('Unsupported Arturia preset archive layout %d.'%layout)
    tags={}
    for group in _meta(t,'Characteristics').split(';'):
        key,_,values=group.partition(',')
        if key in TAG_GROUPS:tags[key]=[v for v in values.split('|') if v]
    m=_PARAMS.search(t)
    if not m:raise ValueError('Arturia preset has no parameter block.')
    r.i=m.end();parameters={}
    for _ in range(int(m.group(1))):
        key=r.str();m2=re.compile(r'(\S+) ?').match(t,r.i);r.i=m2.end()
        try:parameters[key]=float(m2.group(1))
        except ValueError:raise ValueError('Non-numeric value for Arturia parameter %r.'%key) from None
    if inline_tags and not tags:tags={'Characteristics':inline_tags}
    return {'name':name,'pack':pack,'designer':'' if designer=='Unknown' else designer,'notes':notes,
            'type':_meta(t,'Type') or inline_type,'subtype':_meta(t,'Subtype'),'tags':tags,'parameters':parameters}

def _blocks(text):
    m=_PARAMS.search(text)
    if not m:raise ValueError('Arturia state has no parameter block.')
    r=_Reader(text);r.i=m.end();items=[]
    for _ in range(int(m.group(1))):
        key=r.str();v=re.compile(r'(\S+) ?').match(text,r.i)
        if not v:raise ValueError('Truncated Arturia parameter block.')
        r.i=v.end();items.append((key,v.group(1)))
    return text[:m.start()],items,text[r.i:]

def merge_state(state,preset):
    """A plug-in state carrying `preset`: its identity, tags and every setting it stores.

    `state` is the instrument's saved host state (same archive format, plus editor extras
    such as open panel tabs); settings the preset lacks keep the state's values, and the
    state's trailing host mappings are kept. Both inputs must parse; the result is re-parsed.
    """
    parse(state);parse(preset)
    _,items,suffix=_blocks(state.decode('latin-1'));prefix,preset_items,_=_blocks(preset.decode('latin-1'))
    values=dict(preset_items);merged=[(k,values.get(k,v)) for k,v in items]
    body=' '.join('%d %s %s'%(len(k),k,v) for k,v in merged)
    out=(prefix+' 0 0 0 7 0 0 0 0 0 0 %d 0 0 0 '%len(merged)+body+' '+suffix).encode('latin-1')
    parse(out)
    return out

def set_state_parameter(state,setting,value):
    """A copy of a plug-in state with one stored setting changed; refuses a setting the state does not have."""
    parse(state)
    prefix,items,suffix=_blocks(state.decode('latin-1'))
    if setting not in dict(items):raise ValueError('This state has no setting called %r.'%setting)
    text='%.8g'%float(value)
    merged=[(k,text if k==setting else v) for k,v in items]
    body=' '.join('%d %s %s'%(len(k),k,v) for k,v in merged)
    out=(prefix+' 0 0 0 7 0 0 0 0 0 0 %d 0 0 0 '%len(merged)+body+' '+suffix).encode('latin-1')
    parse(out)
    return out

def load(path,root=ROOT):
    p=Path(path).resolve();root=Path(root).resolve()
    if not p.is_relative_to(root) or not p.is_file():raise ValueError('Preset is outside the local Arturia library.')
    d=parse(p.read_bytes());d['path']=str(p);d['category']=p.relative_to(root).parts[1] if len(p.relative_to(root).parts)>2 else ''
    return d

def library(instrument,root=ROOT):
    """All presets of one instrument (Factory, User, Tutorials...), sorted by path; unreadable files are listed, not hidden."""
    base=Path(root)/instrument;presets,errors=[],[]
    for p in sorted(x for x in base.rglob('*') if x.is_file() and x.suffix!='.xml'):
        try:presets.append(load(p,root))
        except ValueError as e:errors.append({'path':str(p),'error':str(e)})
    return presets,errors

def liked(instrument,root=ROOT):
    """File paths the user has hearted in Arturia's browser, read from a temporary copy of its database."""
    src=Path(root)/'db.db3'
    if not src.is_file():return set()
    with tempfile.TemporaryDirectory(prefix='scrunch-arturia-db-') as tmp:
        for suffix in ('','-wal','-shm'):
            if Path(str(src)+suffix).is_file():shutil.copy2(str(src)+suffix,Path(tmp)/('db.db3'+suffix))
        con=sqlite3.connect(Path(tmp)/'db.db3')
        try:rows=con.execute('select p.file_path from Preset_Id p join Instruments i on i.key_id=p.instrument_key '
                             'where i.name=? and p.favorite!=0',(instrument,)).fetchall()
        finally:con.close()
    return {str(Path(r[0]).resolve()) for r in rows}

def host_names(instrument,plugins=PLUGINS):
    """Vendor map of preset setting -> automatable parameter name a host such as Live shows (the <vst> section).

    Settings absent here (e.g. modulation-routing amounts) cannot be automated by a host.
    Display names are not unique; callers must not address a host parameter by name alone.
    """
    path=Path(plugins)/instrument/'resources'/'Reference_ParamNames.xml'
    if not path.is_file():return {}
    return {n:d for _,n,d in vst_params(path)}

def vst_params(path):
    """[(position, name, display name)] from a vendor Reference_ParamNames.xml <vst> section.

    Some vendor files contain unescaped '&' (Pigments), so bare ampersands are escaped before parsing.
    """
    text=re.sub(r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)','&amp;',Path(path).read_text(encoding='utf-8',errors='replace'))
    vst=ET.fromstring(text).find('vst')
    return [] if vst is None else [(i,p.get('name'),p.get('display_name')) for i,p in enumerate(vst.findall('param'))]

def _stats(values):
    v=sorted(values);q=statistics.quantiles(v,n=4,method='inclusive') if len(v)>1 else [v[0]]*3
    return {'median':round(q[1],4),'low':round(q[0],4),'high':round(q[2],4)}

def family(preset):
    """Broad sound family across vendor types: any 'Plucked ...' subtype is a pluck, otherwise the vendor Type."""
    return 'Pluck' if 'pluck' in preset['subtype'].lower() else preset['type']

def profiles(presets,likes=(),distinctive=12,group=lambda p:(p['type'],p['subtype'])):
    """Per sound group: members, likes, and the settings that most separate it from the rest of the library.

    Separation = |type median - library median| / library spread (inter-quartile, floored),
    over parameters that actually vary. Tutorials and templates are excluded.
    """
    likes=set(likes)
    sounds=[p for p in presets if p['type'] not in ('Custom','Template','')]
    varying=sorted(k for k in set().union(*(p['parameters'] for p in sounds)) if not k.startswith('__')
                   and len({round(p['parameters'].get(k,0),3) for p in sounds})>3) if sounds else []
    overall={k:_stats([p['parameters'].get(k,0) for p in sounds]) for k in varying}
    out={}
    for key in sorted({group(p) for p in sounds}):
        members=[p for p in sounds if group(p)==key]
        scored=[]
        for k in varying:
            s=_stats([p['parameters'].get(k,0) for p in members]);o=overall[k]
            score=abs(s['median']-o['median'])/max(o['high']-o['low'],.05)
            if score>=.4:scored.append((round(score,2),k,s,o))
        scored.sort(key=lambda x:(-x[0],x[1]))
        out[key if isinstance(key,str) else ' / '.join(key)]={'count':len(members),
            'presets':[p['name'] for p in members],'liked':[p['name'] for p in members if p.get('path') in likes],
            'distinctive':[{'parameter':k,'score':sc,'typeMedian':s['median'],'typeRange':[s['low'],s['high']],
                            'libraryMedian':o['median']} for sc,k,s,o in scored[:distinctive]]}
    return out

def build(instrument,root=ROOT,plugins=PLUGINS):
    presets,errors=library(instrument,root);likes=liked(instrument,root);names=host_names(instrument,plugins)
    families=profiles(presets,likes,group=family);groups=profiles(presets,likes)
    duplicated={n for n in names.values() if list(names.values()).count(n)>1}
    for profile in list(families.values())+list(groups.values()):
        for d in profile['distinctive']:
            d['hostName']=names.get(d['parameter']);d['hostNameUnique']=d['hostName'] is not None and d['hostName'] not in duplicated
    knobs=sorted({d['parameter'] for f in families.values() for d in f['distinctive'] if d['hostName']})
    return {'instrument':instrument,'basis':'Arturia preset files and browser likes (read-only); settings, not auditioned sound',
            'presetCount':len(presets),'unreadable':errors,
            'liked':sorted(p['name'] for p in presets if p['path'] in likes),
            'hostParameters':{'count':len(names),'source':'Reference_ParamNames.xml <vst>','duplicateDisplayNames':len(duplicated)},
            'coachKnobs':[{'parameter':k,'hostName':names[k],'hostNameUnique':names[k] not in duplicated} for k in knobs],
            'families':families,'profiles':groups,
            'presets':[{'name':p['name'],'category':p['category'],'type':p['type'],'subtype':p['subtype'],'designer':p['designer'],
                        'notes':p['notes'],'tags':p['tags'],'liked':p['path'] in likes,'path':p['path'],
                        'parameterCount':len(p['parameters']),
                        'settingsSha256':hashlib.sha256(json.dumps(p['parameters'],sort_keys=True).encode()).hexdigest()[:16]}
                       for p in presets]}

if __name__=='__main__':
    import sys,os
    instrument=sys.argv[1] if len(sys.argv)>1 else 'Jup-8000 V'
    data=Path(os.environ.get('SCRUNCH_DATA',str(Path(__file__).resolve().parent/'data')))
    out=data/'knowledge'/'arturia'/(re.sub(r'[^a-z0-9]+','-',instrument.lower()).strip('-')+'.json')
    doc=build(instrument);out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_suffix('.tmp');tmp.write_text(json.dumps(doc,indent=1));tmp.replace(out)
    print(json.dumps({'written':str(out),'presets':doc['presetCount'],'unreadable':len(doc['unreadable']),
                      'liked':doc['liked'],'types':len(doc['profiles']),'families':sorted(doc['families']),
                      'hostParameters':doc['hostParameters'],'coachKnobs':len(doc['coachKnobs'])},indent=1))
