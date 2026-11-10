"""Fresh rehearsal sets made from a copy; never edits the saved source XML."""
import gzip,hashlib,json,re,time,uuid,subprocess
from pathlib import Path
from urllib.parse import unquote,urlparse
import xml.etree.ElementTree as ET
import engine,jup_bridge

LAB=engine.ROOT.parent/'Producer Pal Lab Project'/'Producer Pal Lab.als'
REHEARSALS=engine.ROOT.parent/'Rehearsals'
LEGACY_REHEARSAL_NAME='Producer Pal Lab.als'  # rehearsal copies made before sessions could be named
# Live's document window is found by the document it shows, never by a fixed title.
PATH_SCRIPT='''tell application "System Events" to tell process "Live"
repeat with w in windows
try
set d to value of attribute "AXDocument" of w
if d is not missing value and d is not "" then return d
end try
end repeat
error "Live has no open Set window"
end tell'''
SAVE_SCRIPT='''tell application "System Events" to tell process "Live"
set frontmost to true
click menu item "Save Live Set" of menu "File" of menu bar 1
end tell'''

ADOPTED=engine.DATA/'coach'/'adopted.json'

def adopted_sets():
    """Projects of the producer's own that Scrunch was explicitly pointed at, and nothing else.

    A rehearsal Set is Scrunch's own and is authorised by where it lives. Somebody's project is
    not: it counts only because they asked for that exact file to be adopted, and it is listed
    here at that moment. Deleting the entry takes the project back out of Scrunch's reach.
    """
    try:listed=json.loads(ADOPTED.read_text())
    except (OSError,ValueError):return []
    return [Path(p).resolve() for p in listed if isinstance(p,str)]

def remember_adopted(path):
    """Add a project to the list Scrunch may work in. Called only by an explicit adoption."""
    path=Path(path).resolve();listed=[str(p) for p in adopted_sets()]
    if str(path) not in listed:listed.append(str(path))
    ADOPTED.parent.mkdir(parents=True,exist_ok=True)
    tmp=ADOPTED.with_suffix('.tmp');tmp.write_text(json.dumps(listed,indent=1));tmp.replace(ADOPTED)
    return listed

def forget_adopted(path):
    """Take a project back out of Scrunch's reach; the file itself is left alone."""
    path=Path(path).resolve();listed=[str(p) for p in adopted_sets() if p!=path]
    ADOPTED.parent.mkdir(parents=True,exist_ok=True)
    tmp=ADOPTED.with_suffix('.tmp');tmp.write_text(json.dumps(listed,indent=1));tmp.replace(ADOPTED)
    return listed

def session_manifest(path):
    """The Scrunch identity record for a rehearsal Set or an adopted project, or None."""
    path=Path(path).resolve();manifest=path.parent/'session.json'
    if not manifest.is_file():return None
    try:data=json.loads(manifest.read_text())
    except ValueError:return None
    return data if isinstance(data,dict) and Path(str(data.get('path',''))).resolve()==path else None

def authorised(path):
    """Any Live Set may be used; location and adoption records are not permissions."""
    return Path(path).suffix.lower()=='.als'

def authorised_names():
    """Live Set names that belong to authorised sessions (for backends that report only a name)."""
    names={LAB.stem,Path(LEGACY_REHEARSAL_NAME).stem}|{p.stem for p in adopted_sets()}
    for manifest in REHEARSALS.glob('*/session.json') if REHEARSALS.is_dir() else ():
        try:target=Path(json.loads(manifest.read_text()).get('path',''))
        except (ValueError,AttributeError):continue
        if target.parent==manifest.parent and target.suffix=='.als':names.add(target.stem)
    return names

def read_current_path():
    """Read the open Set's path without granting permission to save or switch it.

    Live's own report (through the bridge) is preferred, because Live's window cannot be read
    while Live is on another Space or hidden; the window is the fallback.
    """
    value=''
    host=getattr(engine,'host',None)
    try:
        adapter=host() if host else None
        value=adapter.document_path() if hasattr(adapter,'document_path') else ''
    except Exception:value=''
    if not value:value=jup_bridge.script(PATH_SCRIPT)
    path=Path(unquote(urlparse(value).path) if value.startswith('file:') else value).resolve()
    return path

def current_path():
    path=read_current_path()
    if not authorised(path):
        raise ValueError('Save this untitled Live Set once so its file can be accessed.')
    return path

def window_title():
    """Live titles the Set window with the document's file name without extension."""
    return current_path().stem

def save_current():
    path=current_path();jup_bridge.script(SAVE_SCRIPT)
    time.sleep(.5)
    blocked=jup_bridge.script('''tell application "System Events" to tell process "Live"
repeat with w in windows
if subrole of w is "AXDialog" then
if value of attribute "AXFocused" of w is true then return true
end if
end repeat
return false
end tell''')
    if blocked=='true':raise ValueError('Live opened a save dialog; checkpoint refused until the session is saved.')
    if current_path()!=path:raise ValueError('Session changed during save.')
    ET.fromstring(gzip.decompress(path.read_bytes()))
    return path

def _empty(track):
    return not track.findall('.//MidiClip') and not track.findall('.//AudioClip')

def template_tracks(tracks,host_device):
    """The one track a rehearsal keeps. A backend hosted inside the Set (Producer Pal)
    needs its own device track; a control-surface backend keeps the host track when
    present (so the fallback still works) and otherwise the first empty device-free MIDI track."""
    hosted=[t for t in tracks if any('Producer_Pal.amxd' in e.get('Value','') for e in t.findall('.//FileRef/Path'))]
    if host_device:
        hosted=[t for t in tracks if any(host_device in e.get('Value','') for e in t.findall('.//FileRef/Path'))]
        if len(hosted)!=1 or not _empty(hosted[0]):
            raise ValueError('A unique empty '+host_device+' track is required for the rehearsal template.')
        return hosted
    if len(hosted)==1 and _empty(hosted[0]):return hosted
    plain=[t for t in tracks if t.tag=='MidiTrack' and _empty(t) and not list(t.find('DeviceChain/DeviceChain/Devices') or [])]
    if not plain:raise ValueError('An empty MIDI track without devices is required for the rehearsal template.')
    return plain[:1]

MAX_DEVICE_MARKERS=(b'Producer_Pal',b'.amxd',b'<MxDevice')

def create(folder_name=None,bridge_only=False):
    """Open a fresh rehearsal copy of the current Set.

    folder_name names the Rehearsals folder (default: a random session id). bridge_only
    (owned-bridge backend only) strips every device from the kept template track and
    refuses to write the copy if any Max for Live or Producer Pal reference remains.
    With folder_name the Set is saved as '<folder_name>.als' and identified by session.json."""
    engine.daw_adapter.require(engine.host(),engine.needs.FRESH_SESSION,'open a fresh session')
    if bridge_only and getattr(engine.host(),'SET_HOST_DEVICE',None) is not None:
        raise ValueError('A bridge-only session needs the ScrunchBridge backend; the selected backend runs inside the Set.')
    if folder_name is not None and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,60}',folder_name) or (REHEARSALS/folder_name).exists()):
        raise ValueError('Session folder name is invalid or already used; nothing was changed.')
    engine.live_set();engine.stop()
    path=save_current()
    source=path.read_bytes();root=ET.fromstring(gzip.decompress(source));tracks=root.find('LiveSet/Tracks')
    candidates=template_tracks(tracks,getattr(engine.host(),'SET_HOST_DEVICE',None))
    for track in list(tracks):
        if track is not candidates[0] and track.tag!='ReturnTrack':tracks.remove(track)
    if bridge_only:
        devices=candidates[0].find('DeviceChain/DeviceChain/Devices')
        for device in list(devices if devices is not None else []):devices.remove(device)
    # Reset selection/transport references; only the copied document changes.
    for tag,value in [('HighlightedTrackIndex','0'),('SoloOrPflSavedValue','false')]:
        node=root.find('LiveSet/'+tag)
        if node is not None:node.set('Value',value)
    document=gzip.compress(ET.tostring(root,encoding='utf-8',xml_declaration=True))
    if bridge_only and any(marker in gzip.decompress(document) for marker in MAX_DEVICE_MARKERS):
        raise ValueError('The fresh Set would still contain a Max for Live or Producer Pal device; nothing was written.')
    session_id=uuid.uuid4().hex
    folder=REHEARSALS/(folder_name or session_id);folder.mkdir(parents=True)
    (folder/'Ableton Project Info').mkdir()
    destination=folder/((folder_name+'.als') if folder_name else path.name)
    destination.write_bytes(document)
    manifest={'previousPath':str(path),'previousSha256':hashlib.sha256(source).hexdigest(),'path':str(destination),'sessionId':session_id}
    (folder/'session.json').write_text(json.dumps(manifest,indent=2))
    engine.STATE['sessionTransition']=manifest;engine.save()
    subprocess.run(['/usr/bin/open','-a','Ableton Live 12 Suite',str(destination)],check=True,timeout=10)
    for _ in range(40):
        try:
            if current_path()==destination.resolve():
                live=engine.live_set()
                if len(live['tracks'])==1:
                    engine.STATE.setdefault('sessionHistory',[]).append({'sessionId':engine.STATE.get('sessionId','lab'),'path':str(path),'source':engine.STATE.get('source')})
                    engine.STATE.update(sessionId=session_id,sessionPath=str(destination),source=None,sessionTransition=None)
                    engine.save();return manifest
        except (ValueError,subprocess.TimeoutExpired):pass
        time.sleep(.5)
    raise ValueError('Fresh session was not verified; transition record preserved. Do not repeat blindly.')
