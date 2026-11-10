"""Switch supported presets and effect slots in the current Set and target track.

File-based plugin state changes save and reload the Set in place. Creating a sibling
Set is an explicit build option, not an automatic backup or permission requirement.
"""
import gzip,json,re,subprocess,time
import xml.etree.ElementTree as ET
from pathlib import Path
import arturia_presets,uhe_presets,plugin_knobs,fresh_session,jup_bridge

ARTURIA=('Jup-8000 V','Pigments')
SUPPORTED=ARTURIA+('Diva',)

def presets(query='',instrument='Jup-8000 V',limit=12,library=None,likes=None):
    """Presets ranked for a start sound: every query word must match name/type/subtype/tags/notes; liked presets first."""
    if library is None:
        if instrument=='Diva':
            library,_=uhe_presets.library()
            library=[dict(p,tags={'Character':p['character'],'Features':p['features']}) for p in library]
        else:library,_=arturia_presets.library(instrument)
    if likes is None:likes=arturia_presets.liked(instrument) if instrument in ARTURIA else set()
    words=[w for w in re.findall(r'[a-z0-9]+',str(query).lower()) if w]
    ranked=[]
    for p in library:
        if p['type'] in ('Custom',''):continue
        name=p['name'].lower();hay=' '.join([name,p['type'],p['subtype'],' '.join(sum(p['tags'].values(),[])),p['notes']]).lower()
        if not all(w in hay for w in words):continue
        score=(p['path'] in likes)*100+sum(10 for w in words if w in name)+sum(3 for w in words if w in (p['type']+' '+p['subtype']).lower())
        ranked.append((-score,p['name'],p))
    ranked.sort(key=lambda r:(r[0],r[1]))
    return [{'name':p['name'],'type':p['type'],'subtype':p['subtype'],'liked':p['path'] in likes,'notes':p['notes'],
             'tags':p['tags'],'path':p['path']} for _,_,p in ranked[:limit]]

def _safe(name):
    s=re.sub(r'[^A-Za-z0-9 _.-]+','',name).strip(' .')
    return s[:60] or 'Preset'

def build(current,manifest,preset_path,root=arturia_presets.ROOT,plugins=arturia_presets.PLUGINS,state=None,in_place=True):
    """Write the requested preset or state in place, unless a sibling copy is requested."""
    current=Path(current);plugin=manifest['plugin']
    if manifest.get('trackName'):
        import saved_set   # the track is from the open Set; a stale file is saved first
        doc,target,_=saved_set.locate(current,manifest['trackName'],plugin,manifest.get('trackIndex'))
    else:
        doc=ET.fromstring(gzip.decompress(current.read_bytes()));target=doc
    if state is None:
        if plugin=='Diva':
            preset=Path(uhe_presets.load(preset_path)['path'])         # refuses files outside the Diva library
            state=uhe_presets.state(preset.read_bytes())
        else:
            preset=Path(arturia_presets.load(preset_path,root)['path'])   # refuses files outside the Arturia library
            state=arturia_presets.merge_state(plugin_knobs.plugin_state(target,plugin),preset.read_bytes())
    plugin_knobs.set_plugin_state(target,plugin,state)
    extra={}
    if plugin in ('Pigments','Diva'):
        # No effect-slot model for these yet: a fixed sound-design page of the sections that matter.
        page=plugin_knobs.section_page(plugin,plugins)
        vendor=plugin_knobs.vendor_ids(plugin,plugins) if plugin=='Pigments' else plugin_knobs.vendor_ids(plugin)
        names={i:d for i,_,d in vendor}
        knobs=[(i,names[i]) for i in page]
        plugin_knobs.expose(doc,plugin,knobs,devices=[target] if target is not doc else None)
        extra={'exposedKnobs':[{'id':i,'name':n} for i,n in knobs]}
    if plugin=='Jup-8000 V':
        # expose the knobs of the effects this preset actually uses
        page,slots=plugin_knobs.preset_page(arturia_presets.parse(state)['parameters'],plugin,plugins)
        vendor=plugin_knobs.vendor_ids(plugin,plugins);knobs=[(i,vendor[i][2]) for i in page]
        plugin_knobs.expose(doc,plugin,knobs,devices=[target] if target is not doc else None)
        extra={'exposedKnobs':[{'id':i,'name':n} for i,n in knobs],'fxSlots':{str(k):v for k,v in slots.items()}}
    name=(uhe_presets.parse(uhe_presets.state_preset(state)).get('name') or Path(preset_path).stem if plugin=='Diva'
          else arturia_presets.parse(state)['name'])
    dest=current
    if not in_place:
        dest=current.parent/(_safe(name)+'.als');n=2
        while dest.exists():dest=current.parent/('%s %d.als'%(_safe(name),n));n+=1
    tmp=dest.with_suffix('.tmp');tmp.write_bytes(gzip.compress(b'<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(doc)));tmp.replace(dest)
    new=dict(manifest,**extra,path=str(dest),preset={'name':name,'path':str(preset_path) if preset_path else (manifest.get('preset') or {}).get('path')},
             history=list(manifest.get('history',[]))+[{'path':str(current),'preset':(manifest.get('preset') or {}).get('name')}])
    return dest,new

def effect_change(slot,effect,wait_seconds=90,manifest=None):
    """Put a different effect in one JUP slot. This is the case that needs a reload.

    Which effect a slot holds is not a knob, so Scrunch rewrites the plug-in's saved state and
    reloads the Set: about 12 seconds, and it clears Live's own undo history, so callers announce
    it first. The producer's current sound is saved and carried over; only the slot type changes.
    """
    current=fresh_session.current_path()
    if manifest is None:
        import current_sound,engine
        manifest,_=current_sound.discover(engine.host(),current)
    if manifest.get('plugin')!='Jup-8000 V':raise ValueError('Effect slots are mapped for the Jup-8000 V only so far.')
    if slot not in (1,2,3,4):raise ValueError('Effect slots are 1 to 4.')
    names=[n for n,_ in plugin_knobs.JUP_FX_TYPES]
    if effect not in names:raise ValueError('Unknown effect %r. Choose from: %s.'%(effect,', '.join(names[1:])))
    before=current.stat().st_mtime
    jup_bridge.script(fresh_session.SAVE_SCRIPT)          # keep the sound as it is now, including hand edits
    for _ in range(40):
        if current.stat().st_mtime!=before:break
        time.sleep(.25)
    if manifest.get('trackName'):
        import saved_set
        doc,target,_=saved_set.locate(current,manifest['trackName'],'Jup-8000 V',manifest.get('trackIndex'))
    else:
        doc=ET.fromstring(gzip.decompress(Path(current).read_bytes()));target=doc
    state=plugin_knobs.plugin_state(target,'Jup-8000 V')
    value=names.index(effect)/(len(names)-1)
    state=arturia_presets.set_state_parameter(state,'FX%d_ModuleType'%slot,value)
    # Start the requested effect audibly; subsequent sound instructions can revise its mix.
    try:state=arturia_presets.set_state_parameter(state,'FX%d_Dry_Wet'%slot,0.5)
    except ValueError:pass
    dest,new=build(current,manifest,None,state=state)
    record=current.parent/'session.json';tmp=record.with_suffix('.tmp');tmp.write_text(json.dumps(new,indent=2));tmp.replace(record)
    subprocess.run(['/usr/bin/open','-a','Ableton Live 12 Suite',str(dest)],check=True,timeout=10)
    deadline=time.time()+wait_seconds
    while time.time()<deadline:
        time.sleep(1)
        try:
            if fresh_session.current_path()==dest.resolve():
                return {'session':dest.stem,'slot':slot,'effect':effect,'reloaded':True,
                        'mix':'50%',
                        'note':"Live's undo history is cleared by the reload."}
        except ValueError:pass
    raise ValueError('Live did not reopen %s within %d s; check Live before retrying.'%(dest.name,wait_seconds))

def switch(preset_path,wait_seconds=60,manifest=None):
    current=fresh_session.current_path()
    if manifest is None:
        import current_sound,engine
        manifest,_=current_sound.discover(engine.host(),current)
    before=current.stat().st_mtime
    jup_bridge.script(fresh_session.SAVE_SCRIPT)
    for _ in range(40):
        if current.stat().st_mtime!=before:break
        time.sleep(.25)
    dest,new=build(current,manifest,preset_path)
    record=current.parent/'session.json';tmp=record.with_suffix('.tmp');tmp.write_text(json.dumps(new,indent=2));tmp.replace(record)
    subprocess.run(['/usr/bin/open','-a','Ableton Live 12 Suite',str(dest)],check=True,timeout=10)
    deadline=time.time()+wait_seconds
    while time.time()<deadline:
        time.sleep(1)
        try:
            if fresh_session.current_path()==dest.resolve():return {'session':dest.stem,'preset':new['preset']['name'],'path':str(dest)}
        except ValueError:pass
    raise ValueError('Live did not open %s within %d s; check Live before retrying.'%(dest.name,wait_seconds))
