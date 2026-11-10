"""Expose chosen plug-in knobs to Live by writing a Set's plug-in parameter list (no Configure clicks).

Live shows a plug-in's knobs only from the device's saved ParameterList. Each entry names a
host ParameterId; for Arturia instruments that id is the knob's position in the vendor
Reference_ParamNames.xml <vst> list (lab-verified for Jup-8000 V: 127/128 values equal the
loaded preset file, the 128th a stepped knob snapped by Live).

Hard limit: Live keeps 128 slots per plug-in. A Set with 929 entries crashed Live 12.3.5 on
load (evidence/sound-coach-20260918/crash-929-knobs), so more than 128 is refused here.

Only copies are written: the source Set is read, never modified.
"""
from pathlib import Path
import copy,gzip,json,uuid
import xml.etree.ElementTree as ET
import arturia_presets

MAX_SLOTS=128
EMPTY_ID='-1'
EMPTY_VISUAL='1073741823'

DIVA_MAP='knowledge/u-he/diva-id-map.json'          # under the Scrunch data directory

def vendor_ids(instrument,plugins=arturia_presets.PLUGINS,data=None):
    """[(host parameter id, setting name, display name)] for a plug-in.

    Arturia ships a name file. Diva does not, so Scrunch uses the map it built by exposing id
    pages in Live and matching values against `.h2p` presets; ids without a confident match keep
    the plug-in's own display name as their setting.
    """
    path=Path(plugins)/instrument/'resources'/'Reference_ParamNames.xml'
    if path.is_file():return arturia_presets.vst_params(path)
    if instrument=='Diva':
        import json,os
        root=Path(data or os.environ.get('SCRUNCH_DATA') or (Path(__file__).resolve().parent/'data'))
        record=root/DIVA_MAP
        if not record.is_file():raise ValueError('No knob map for Diva yet; run the id discovery first.')
        ids=json.loads(record.read_text())['ids']
        out=[]
        for key in sorted(ids,key=int):
            entry=ids[key];name=entry.get('key') if entry.get('strong') else None
            out.append((int(key),name or entry.get('name'),entry.get('name')))
        return out
    raise ValueError('Scrunch cannot reach the knobs of %s yet: it has knob maps for Arturia synths (Jup-8000 V, Pigments) and Diva only.'%instrument)

def sound_design_page(instrument='Jup-8000 V',plugins=arturia_presets.PLUGINS,coach=()):
    """The JUP sound-design knob page (<=128): the whole synth panel, each FX slot's mix/bypass/send/routing, then coach knobs."""
    ids=vendor_ids(instrument,plugins);by_name={n:i for i,n,_ in ids}
    panel=[i for i,n,_ in ids if i<82]
    fx=[by_name[f'FX{s}_{k}'] for s in range(1,5) for k in ('Dry_Wet','Bypass','RoutingMode','SendLevel') if f'FX{s}_{k}' in by_name]
    page=[]
    for i in panel+fx+[by_name[c] for c in coach if c in by_name]:
        if i not in page:page.append(i)
    if len(page)>MAX_SLOTS:raise ValueError('Knob page exceeds Live\'s 128 plug-in slots.')
    return page

# Jup-8000 V effect slot types: the stored FXn_ModuleType value is index/18 (vendor item list Vc11FXModuleTypesV1.1),
# paired with the name segment its knobs use in Reference_ParamNames (FXn_<segment>_...).
JUP_FX_TYPES=(('None',None),('Reverb','Reverb'),('Delay','Delay'),('Tape Echo','TapeEcho'),('PS Delay','OctaverDelay'),
              ('Chorus','Chorus'),('Chorus JUN-6','Juno6Chorus'),('Flanger','Flanger'),('Phaser','Phaser'),('Compressor','Compressor'),
              ('Multiband','MultibandCompressor'),('BitCrusher','BitCrusher'),('Multi Filter','MultiModeFilter'),('Param EQ','ParamEq'),
              ('Panner','StereoPan'),('Distortion','Distortion'),('Flanger BL-20','FlangerBelLite'),('Super Unison','SuperUnison'),
              ('Trance Gate','TranceGate'))

# Effects whose knobs shape a sound first; mixing/mastering tools last.
FX_PRIORITY=('Trance Gate','Delay','Tape Echo','PS Delay','Reverb','Chorus','Chorus JUN-6','Super Unison','Distortion','Flanger',
             'Flanger BL-20','Phaser','Multi Filter','BitCrusher','Panner','Compressor','Param EQ','Multiband')

def fx_slots(preset_parameters):
    """{slot: effect type name} from a JUP preset's stored module types; unknown values are refused, not guessed."""
    out={}
    for slot in range(1,5):
        v=preset_parameters.get('FX%d_ModuleType'%slot)
        if v is None:continue
        index=round(v*(len(JUP_FX_TYPES)-1))
        if abs(index/(len(JUP_FX_TYPES)-1)-v)>1e-3:raise ValueError('FX%d type value %r is not a known effect type.'%(slot,v))
        out[slot]=JUP_FX_TYPES[index][0]
    return out

def preset_page(preset_parameters,instrument='Jup-8000 V',plugins=arturia_presets.PLUGINS):
    """Knobs for one JUP preset (<=128): synth panel, each slot's mix/send, then every knob of the effects it uses.

    Low-priority panel knobs (sequencer, then slot bypass/routing) make room when the effects need it;
    if effects alone still overflow, the later knobs of the last effects are dropped.
    """
    ids=vendor_ids(instrument,plugins);by_name={n:i for i,n,_ in ids};slots=fx_slots(preset_parameters)
    core=[i for i,n,_ in ids if i<75 and not n.startswith('FX')]               # macros, oscillators, filter, envelopes, LFOs, tone
    mix=[by_name[f'FX{s}_{k}'] for s in range(1,5) for k in ('Dry_Wet','SendLevel') if f'FX{s}_{k}' in by_name]
    segment={name:seg for name,seg in JUP_FX_TYPES}
    per_effect=[[i for i,n,_ in ids if n.startswith(f'FX{s}_{segment[kind]}_')]
                for s,kind in sorted(slots.items(),key=lambda sk:(FX_PRIORITY.index(sk[1]) if sk[1] in FX_PRIORITY else len(FX_PRIORITY),sk[0]))
                if segment.get(kind)]
    effects=[]                                                                   # take turns so no effect is starved
    for turn in range(max([len(e) for e in per_effect] or [0])):
        effects+=[e[turn] for e in per_effect if turn<len(e)]
    optional=[i for i,n,_ in ids if 75<=i<82]+[by_name[f'FX{s}_{k}'] for s in range(1,5) for k in ('Bypass','RoutingMode') if f'FX{s}_{k}' in by_name]
    page=[]
    for i in core+mix+effects+optional:          # priority order; each knob once; stop at Live's limit
        if i not in page and len(page)<MAX_SLOTS:page.append(i)
    return page,slots

# Which knobs a sound-design page takes for the synths that have no effect-slot model, in
# priority order. Everything the move engine can reach comes first, then the rest of the panel.
PAGE_SECTIONS={
 'Pigments':(r'MasterVolume',r'Macro\d',r'Env1_(Attack|Decay|Sustain|Release|Velocity)',r'Filter[12]_(Cutoff|Resonance|KeyTrack|Drive|Mode|Blend)',
             r'Env[23]_(Attack|Decay|Sustain|Release)',r'Engine[12]_\w*Unison\w*',r'Engine[12]_\w*(MainVolume|Coarse|Fine|Octave|Level|Volume)',
             r'Engine[12]_\w*(FrameIndex|Shape|Symmetry|Timbre|PhaseDist|FMAmount|PMAmount|Sync|Noise\w*)',
             r'FX[1-9]_Dry_Wet',r'FX[1-9]_\w*(Drive|Depth|Rate|Feedback|Size|Time|Mix|Amount)',
             r'(LFO|Function|Random)\d_\w*(Rate|Amount|Depth)',r'Filter[12]_\w+',r'Utility_\w+'),
 'Diva':(r'VCF1:\w+',r'ENV1:\w+',r'ENV2:\w+',r'VCA1:\w+',r'HPF:\w+',r'OSC:\w+',r'LFO[12]:\w+',r'VCC:\w+',
         r'(Chrs|Phase|Plate|Delay|Rtary)[12]:\w+',r'FX[12]:\w+'),
}

def section_page(instrument,plugins=arturia_presets.PLUGINS,data=None,limit=MAX_SLOTS):
    """A sound-design page for a synth without an effect-slot model: roles first, then the rest of the panel."""
    ids=vendor_ids(instrument,plugins,data=data) if instrument=='Diva' else vendor_ids(instrument,plugins)
    patterns=PAGE_SECTIONS.get(instrument)
    if not patterns:raise ValueError('No page layout for %r yet.'%instrument)
    import re as _re
    page=[]
    for pattern in patterns:
        for i,setting,_ in ids:
            if len(page)>=limit:break
            if i not in page and setting and _re.fullmatch(pattern,str(setting)):page.append(i)
    return page

def _plugin_name(device):
    for tag in ('Vst3PluginInfo','VstPluginInfo','AuPluginInfo'):
        info=device.find('PluginDesc/'+tag)
        if info is not None:
            n=info.find('Name') if info.find('Name') is not None else info.find('PlugName')
            if n is not None:return n.get('Value')
    return None

def _pointee_ids(root):
    return [int(e.get('Id')) for e in root.iter() if (e.tag.endswith('Target') or e.tag=='Pointee') and e.get('Id','').isdigit()]

def track_name(track):
    e=track.find('Name/EffectiveName') if track.find('Name/EffectiveName') is not None else track.find('Name/UserName')
    return (e.get('Value') if e is not None else '') or ''

def device_on_track(doc,name,plugin):
    """The one `plugin` device on the track called `name`; ambiguity is refused, never guessed at."""
    tracks=[t for t in doc.iter() if t.tag in ('MidiTrack','AudioTrack') and track_name(t)==name]
    if not tracks:raise ValueError('This Set has no track called %r.'%name)
    if len(tracks)>1:raise ValueError('This Set has %d tracks called %r; rename one so Scrunch can tell them apart.'%(len(tracks),name))
    devices=[d for d in tracks[0].iter('PluginDevice') if _plugin_name(d)==plugin]
    if len(devices)!=1:raise ValueError('Track %r holds %d copies of %s; Scrunch works on one.'%(name,len(devices),plugin))
    return devices[0]

def automated_slots(doc,device):
    """Which of a device's knobs the producer has already automated or modulated in this Set.

    Exposing a new page replaces the device's parameter slots, and an envelope that points at an
    old slot has nothing left to point at. Anything found here is the producer's own work, so it
    is reported before a single thing is written.
    """
    plist=device.find('ParameterList')
    if plist is None:return []
    targets={}
    for c in plist:
        name=c.find('ParameterName')
        for e in c.iter():
            if (e.tag.endswith('Target') or e.tag=='Pointee') and (e.get('Id','') or '').isdigit():
                targets[int(e.get('Id'))]=(name.get('Value') if name is not None else '')
    pointed=set()
    for env in doc.iter('AutomationEnvelope'):
        pid=env.find('EnvelopeTarget/PointeeId')
        if pid is not None and (pid.get('Value','') or '').isdigit():pointed.add(int(pid.get('Value')))
    return sorted({targets[i] for i in pointed if i in targets})

def expose(doc,plugin,knobs,devices=None):
    """Rewrite a `plugin` device's ParameterList in a parsed Set to expose `knobs` [(id, display name)] in order.

    With `devices` given, only those devices are rewritten - which is what working inside somebody's
    own project needs, where other copies of the same synth belong to other parts of the music.
    Returns the number of devices rewritten. New automation/modulation targets get fresh ids
    above the Set's NextPointeeId, which is advanced.
    """
    knobs=list(knobs)
    if not 0<len(knobs)<=MAX_SLOTS:raise ValueError('Expose between 1 and 128 knobs; Live crashes above 128.')
    if len({i for i,_ in knobs})!=len(knobs):raise ValueError('Each knob may be exposed once.')
    live=doc.find('LiveSet') if doc.tag=='Ableton' else doc
    nxt_el=live.find('NextPointeeId')
    nxt=max([int(nxt_el.get('Value'))]+[i+1 for i in _pointee_ids(doc)])
    def fresh():
        nonlocal nxt
        nxt+=1;return str(nxt-1)
    devices=list(devices) if devices is not None else [d for d in doc.iter('PluginDevice') if _plugin_name(d)==plugin]
    for device in devices:
        plist=device.find('ParameterList')
        if plist is None:raise ValueError('Plug-in device has no parameter list.')
        template=next((c for c in plist if c.tag=='PluginFloatParameter' and c.find('ParameterValue/AutomationTarget') is not None),None)
        if template is None:raise ValueError('Plug-in device has no float parameter slot to copy.')
        template=copy.deepcopy(template)
        for c in list(plist):plist.remove(c)
        for slot in range(MAX_SLOTS):
            c=copy.deepcopy(template);c.set('Id',str(slot))
            ident,name=knobs[slot] if slot<len(knobs) else (EMPTY_ID,'')
            c.find('ParameterName').set('Value',name);c.find('ParameterId').set('Value',str(ident))
            c.find('VisualIndex').set('Value',str(slot) if slot<len(knobs) else EMPTY_VISUAL)
            value=c.find('ParameterValue')
            for tag in ('KeyMidi','MidiCCOnOffThresholds'):
                e=value.find(tag)
                if e is not None:value.remove(e)
            if slot>=len(knobs):
                m=value.find('ModulationTarget')
                if m is not None:value.remove(m)
            for e in value:
                if e.tag.endswith('Target'):e.set('Id',fresh())
                if e.tag=='LomId':e.set('Value','0')
            plist.append(c)
    nxt_el.set('Value',str(nxt+1))
    return len(devices)

def plugin_state(doc,plugin):
    """The single `plugin` device's saved VST3 processor state bytes."""
    import binascii
    presets=[d.find('PluginDesc/Vst3PluginInfo/Preset/Vst3Preset') for d in doc.iter('PluginDevice') if _plugin_name(d)==plugin]
    if len(presets)!=1 or presets[0] is None or presets[0].find('ProcessorState') is None:raise ValueError('Expected exactly one %s with saved state.'%plugin)
    return binascii.unhexlify(''.join((presets[0].find('ProcessorState').text or '').split()))

def set_plugin_state(doc,plugin,state):
    """Replace the single `plugin` device's processor and controller state (Arturia keeps both identical)."""
    import binascii
    plugin_state(doc,plugin)
    preset=next(d for d in doc.iter('PluginDevice') if _plugin_name(d)==plugin).find('PluginDesc/Vst3PluginInfo/Preset/Vst3Preset')
    hexed=binascii.hexlify(state).decode().upper()
    for tag in ('ProcessorState','ControllerState'):
        e=preset.find(tag)
        if e is not None:e.text='\n'+'\n'.join(hexed[i:i+80] for i in range(0,len(hexed),80))+'\n'

def write_session(folder,source,plugin,knobs,rehearsals,purpose):
    """Copy `source` Set into rehearsals/<folder>/<folder>.als with `knobs` exposed; authorised by session.json."""
    if not folder or folder!=Path(folder).name or folder.startswith('.'):raise ValueError('invalid session folder name')
    doc=ET.fromstring(gzip.decompress(Path(source).read_bytes()))
    if expose(doc,plugin,knobs)==0:raise ValueError(f'No {plugin} device in the source Set.')
    out=Path(rehearsals)/folder
    out.mkdir(parents=True,exist_ok=False);(out/'Ableton Project Info').mkdir()
    als=out/(folder+'.als')
    als.write_bytes(gzip.compress(b'<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(doc)))
    manifest={'path':str(als),'sessionId':uuid.uuid4().hex,'purpose':purpose,'source':str(source),'plugin':plugin,
              'exposedKnobs':[{'id':i,'name':n} for i,n in knobs]}
    (out/'session.json').write_text(json.dumps(manifest,indent=2))
    return als

def check_exposed(exposed,knobs):
    """Live's exposed parameters (bridge readback) minus 'Device On', refused unless they are exactly `knobs` [(id, name)] in order."""
    live=[p for p in exposed if not (p['name']=='Device On' and p is exposed[0])]
    if [p['name'] for p in live]!=[n for _,n in knobs]:
        raise ValueError('Live\'s exposed knobs differ from the session record; reload the Set before writing.')
    return live

def adopt_live_names(manifest,exposed):
    """Session record with knob names as the plug-in reports them in Live (plug-in updates rename knobs, e.g. Pigments
    'Env VCA Attack' -> 'Env Amp Attack'). Ids and order are unchanged; renames are listed. Refused if the slot count differs."""
    live=[p for p in exposed if not (p['name']=='Device On' and p is exposed[0])]
    knobs=manifest.get('exposedKnobs',[])
    if len(live)!=len(knobs):raise ValueError('Live exposes %d knobs but the session records %d; the Set was not loaded as written.'%(len(live),len(knobs)))
    renamed=[{'id':k['id'],'from':k['name'],'to':p['name']} for k,p in zip(knobs,live) if k['name']!=p['name']]
    return dict(manifest,exposedKnobs=[{'id':k['id'],'name':p['name']} for k,p in zip(knobs,live)],
                renamedByPlugin=list(manifest.get('renamedByPlugin',[]))+renamed)

def preset_writes(preset_parameters,exposed,knobs,vendor):
    """Writes that put a preset file's stored values on the exposed knobs: [{parameter, name, setting, value}].

    Values are the preset's own units, which equal Live's for Arturia plug-ins (lab-verified);
    stepped knobs are snapped by Live, so callers compare readback with the display, not exact value.
    Settings the preset lacks are skipped, never guessed.
    """
    setting={i:n for i,n,_ in vendor};out=[]
    for p,(ident,name) in zip(check_exposed(exposed,knobs),knobs):
        key=setting.get(ident)
        if key in preset_parameters:
            out.append({'parameter':p['id'],'name':name,'setting':key,
                        'value':min(max(preset_parameters[key],p.get('min',0)),p.get('max',1))})
    return out
