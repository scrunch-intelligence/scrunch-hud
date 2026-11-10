"""Discover sound controls in the open Set without project registration."""
import hashlib
import fresh_session
import plugin_knobs


class ControlsUnavailable(ValueError):
    def __init__(self,track,plugin):
        self.track=track;self.plugin=plugin
        import sound_coach
        super().__init__(sound_coach.host_step(plugin,track['name']))


# The words of the current request, so "make Bass Low darker" picks that track by name.
TEXT_HINT=''
PINNED=None      # Match Reference ON: the hosted synth's track id; words and selection never move it
MISSING=[]


def _named(choices,text):
    """The track John means, the way a producer says it: "track 9", "the kick", "kick 3", "the pluck".

    "track N" is Live's track number. A word from a track name picks every track carrying it; a number
    after it ("kick 3") picks the third such track, or the one literally named "Kick 3".
    """
    import re
    norm=lambda x:re.sub(r'[^a-z0-9]+',' ',(x or '').lower()).strip()
    words={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10,'eleven':11,
           'twelve':12,'thirteen':13,'fourteen':14,'fifteen':15,'sixteen':16,'seventeen':17,'eighteen':18,'nineteen':19,'twenty':20}
    said=' '.join(str(words.get(w,w)) for w in norm(text).split());padded=' '+said+' '
    number=re.search(r'\btrack (?:number )?(\d+)\b',said)
    if number:
        n=int(number.group(1))
        hit=[c for c in choices if c[0].get('trackIndex') is not None and c[0]['trackIndex']+1==n]
        if hit:return hit
    whole=[c for c in choices if norm(c[0]['name']) and ' '+norm(c[0]['name'])+' ' in padded]
    if whole:return whole
    filler={'make','more','less','the','this','that','sound','synth','track','with','and','work','on','lets','let',
            'it','a','an','to','of','up','down','please'}
    spoken=[w for w in said.split() if w not in filler and not w.isdigit() and len(w)>2]
    for word in spoken:
        hits=[c for c in choices if word in norm(re.sub(r'^\d+\W*','',c[0]['name'])).split()]
        if not hits:continue
        after=re.search(r'\b'+re.escape(word)+r' (\d+)\b',said)
        if after and len(hits)>1:
            k=int(after.group(1))
            exact=[c for c in hits if re.search(r'\b'+re.escape(word)+r'\W*'+str(k)+r'\b',norm(c[0]['name']))]
            if exact:return exact
            if 1<=k<=len(hits):return [hits[k-1]]
        return hits
    return []




def _installed(plugin):
    from pathlib import Path
    import re
    key=lambda n:re.sub(r'[^a-z0-9]','',n.lower())
    roots=[Path(r).expanduser() for r in ('/Library/Audio/Plug-Ins','~/Library/Audio/Plug-Ins')]
    for root in roots:
        for sub in ('Components','VST3','VST','CLAP'):
            folder=root/sub
            if folder.is_dir() and any(key(f.stem)==key(plugin) for f in folder.iterdir()):return True
    return False


FORMATS={'vst':('VST','.vst','VST2'),'vst3':('VST3','.vst3','VST3'),'au':('Components','.component','AU')}


def _missing(device):
    """Plain sentence if this plug-in (in the format the Set asks for) is not on this Mac, else None."""
    if device.get('className')!='PluginDevice':return None
    import re
    from pathlib import Path
    key=lambda n:re.sub(r'[^a-z0-9]','',n.lower())
    plugin=device.get('deviceName') or device.get('name') or ''
    found={}
    for root in (Path('/Library/Audio/Plug-Ins'),Path('~/Library/Audio/Plug-Ins').expanduser()):
        for fmt,(sub,ext,label) in FORMATS.items():
            folder=root/sub
            if folder.is_dir() and any(f.suffix==ext and key(f.stem)==key(plugin) for f in folder.iterdir()):found[fmt]=label
    wanted=device.get('plugin')
    # Live's PluginDevice is VST2 or VST3; AuPluginDevice is AU.
    if (wanted=='vst' and ('vst' in found or 'vst3' in found)) or wanted in found or (wanted not in FORMATS and found):return None
    if found:return '%s wants the %s version, which is not installed (you have %s).'%(plugin,FORMATS[wanted][2],', '.join(sorted(set(found.values()))))
    return '%s is not installed on this Mac.'%plugin


def discover(adapter, path=None):
    selected=adapter.selected_track()
    selected=selected.get('id') if isinstance(selected,dict) else selected
    tracks=adapter.session()['tracks']
    by_id={t['id']:t for t in tracks}
    choices=[]
    if PINNED:
        if PINNED not in by_id:
            raise ValueError('The Match Reference target track is no longer in the Set. Turn Match Reference off and on '
                             'with the synth\'s track selected.')
        choices=[(by_id[PINNED],d) for d in adapter.devices(PINNED) if d.get('role')=='instrument']
        if not choices:
            raise ValueError('The Match Reference target, %s, has no instrument on it now.'%by_id[PINNED]['name'])
    # Named in what John said ("track 9", "the kick", "kick 3")? That wins over Live's selection.
    named_tracks=[] if PINNED else [t for t,_ in _named([(t,None) for t in tracks],TEXT_HINT)]
    for t in named_tracks:
        choices+=[(t,d) for d in adapter.devices(t['id']) if d.get('role')=='instrument']
    if named_tracks and not choices:
        raise ValueError('%s has no instrument on it. Load a synth there, or name the track that has one.'
                         %', '.join(t['name'] for t in named_tracks[:4]))
    if not named_tracks and not PINNED:
        # Only the track he named or the one selected in Live: never another track's synth.
        if selected not in by_id:
            raise ValueError('No track is selected in Live. Click the synth\'s track, or say its name or number.')
        choices=[(by_id[selected],d) for d in adapter.devices(selected) if d.get('role')=='instrument']
        if not choices:
            raise ValueError('The selected track, %s, has no instrument on it. Load a synth there, or select the '
                             'track that has one.'%by_id[selected]['name'])
    candidates=choices
    missing=[(t,d,_missing(d)) for t,d in candidates]
    usable=[(t,d) for t,d,m in missing if not m]
    gone=[(t,m) for t,d,m in missing if m]
    if gone and len(gone)==len(candidates) and len(candidates)<=2:
        t,m=gone[0]
        raise ValueError('%s On %s. What would you like to do: load a synth that is installed, or pick another track?'%(m,t['name']))
    choices=usable
    MISSING[:]=['%s (%s)'%(t['name'],m.split(' ')[0]) for t,m in gone]
    if not choices:
        raise ValueError('No instrument is loaded in this Set; load a synth on the track you want to design.')
    if len(choices)!=1:
        named=_named(choices,TEXT_HINT) if len(choices)>1 else []
        if len(named)==1:choices=named
        elif len(named)>1:raise ValueError('Which one: '+', '.join('track %d %s'%(t['trackIndex']+1,t['name']) for t,d in named[:8])+'?')
        else:raise ValueError('Which synth? Click its track in Live, or say its track name or number.'+
                              (' Missing plug-ins on this Set: '+', '.join(MISSING)+'.' if MISSING else ''))
    track,device=choices[0]
    parameters=adapter.device(device['id'])['parameters']
    parameters=[p for i,p in enumerate(parameters) if not (i==0 and p['name']=='Device On')]
    plugin=device.get('deviceName') or device['name']
    if not parameters:
        if not _installed(plugin):
            raise ValueError('%s on %s is not installed on this Mac, so Scrunch cannot reach its knobs.'%(plugin,track['name']))
        raise ControlsUnavailable(track,plugin)
    stored=fresh_session.session_manifest(path) if path else None
    try:vendor=plugin_knobs.vendor_ids(plugin)
    except ValueError:vendor=[]
    by_name={}
    for ident,setting,label in vendor:
        by_name.setdefault(label,[]).append((ident,setting))
    knobs=[];mapped=[]
    for i,p in enumerate(parameters):
        matches=by_name.get(p['name'],[])
        # Unknown controls still reach the model under their actual Live names.
        # A synth inside Scrunch Host already has unique "<section> <control>" names: no index needed.
        ident,setting=matches[0] if len(matches)==1 else ('live:'+p['id'],p['name'] if device.get('host') else p['name']+' ['+str(i)+']')
        knobs.append({'id':ident,'name':p['name']});mapped.append((ident,setting,p['name']))
    manifest={'path':str(path or ''),'sessionId':hashlib.sha256(str(path or adapter.connect().get('name')).encode()).hexdigest()[:24],
              'plugin':plugin,'exposedKnobs':knobs,'liveTarget':device['id'],'liveTrack':track['id'],'trackName':track['name'],'trackIndex':track.get('trackIndex')}
    if stored and stored.get('plugin')==plugin and [k['name'] for k in stored.get('exposedKnobs',[])]==[p['name'] for p in parameters]:
        manifest.update({k:stored[k] for k in ('preset','fxSlots') if k in stored})
        manifest['exposedKnobs']=stored['exposedKnobs'];mapped=vendor or mapped
    return manifest,mapped
