"""Load the verified local synth Rack and copy the current original to a new track.

The source is validated before any write. An interrupted setup is never retried
blindly. It leaves a named, muted track and a durable journal for inspection.
"""
import hashlib,json,time,uuid,subprocess
from pathlib import Path
import engine,daw_adapter,serum_controls,jup_bridge,serum_assets,workflow_requirements

PRESET=Path.home()/'Music/Ableton/User Library/Presets/Instruments/Instrument Rack/Scrunch Serum - Verified Controls.adg'
PRESET_HASH='a21921515717a4d9b252f4bd9d0e409301f528c89bc86310982510a6523a0d1e'
QUERY='''on run argv
tell application "System Events" to tell process "Live"
set value of text field 1 of group "Browser" of group 1 of window (item 2 of argv) to (item 1 of argv)
end tell
end run'''
LOAD='''on run argv
tell application "System Events" to tell process "Live"
set frontmost to true
perform action "AXRaise" of window (item 2 of argv)
tell window (item 2 of argv)
set results to outline 2 of group "Browser" of group 1
-- User Library search preserves a folder hierarchy, which may be collapsed.
repeat with folderName in {"Presets", "Instruments", "Instrument Rack"}
set matches to every row of results whose name is (item 1 of argv)
if (count of matches) > 0 then exit repeat
set matchingRows to every row of results whose name is (contents of folderName)
if (count of matchingRows) is not 1 then error "Saved synth Rack folder is missing or ambiguous: " & (contents of folderName)
if value of attribute "AXDisclosing" of item 1 of matchingRows is false then
set value of attribute "AXDisclosing" of item 1 of matchingRows to true
delay 0.1
end if
end repeat
set matches to every row of results whose name is (item 1 of argv)
if (count of matches) is 0 then error "Saved synth Rack was not found"
if (count of matches) > 1 then error "Saved synth Rack search has multiple matches: " & (count of matches)
set value of attribute "AXFocused" of item 1 of matches to true
end tell
key code 36
end tell
end run'''

def check_asset(recipe='current-original'):
    item=serum_assets.asset(recipe);p=item['path']
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:
        raise ValueError('The verified synth Rack file is missing or changed. Revalidate it before loading.')

def load_rack(track_id,recipe='current-original',allow_clips=False):
    check_asset(recipe);item=serum_assets.asset(recipe);a=daw_adapter.DEFAULT
    def empty():
        engine.lab_session(a);t=a.track(track_id,clips=True,devices=True,mixer=True,routing=True)
        if t.get('kind')!='midi' or t.get('devices') or (not allow_clips and (t.get('clips') or t.get('sessionClipCount'))):
            raise ValueError('Synth loading needs a new empty MIDI track.')
        return t
    import fresh_session
    title=fresh_session.window_title()
    empty();a.select_track(track_id);jup_bridge.script(jup_bridge.SEARCH,title)
    jup_bridge.script(QUERY,item['name'],title)
    point=jup_bridge.script('''on run argv
tell application "System Events" to tell process "Live"
set p to position of row "User Library" of outline 1 of group "Browser" of group 1 of window (item 1 of argv)
return ((item 1 of p)+40) & "," & ((item 2 of p)+9) as text
end tell
end run''',title).split(',')
    subprocess.run([str(engine.ROOT/'ui_click')]+[str(float(x.strip())) for x in point],check=True,timeout=5)
    time.sleep(.5);empty()
    if a.selected_track()!=track_id:raise ValueError('Selected track changed before Synth loading.')
    # One activation only. Failures remain journalled; never retry this write.
    jup_bridge.script(LOAD,item['path'].name,title)
    for _ in range(40):
        engine.lab_session(a);t=a.track(track_id,clips=True,devices=True,mixer=True,routing=True)
        if t.get('devices'):
            a.update_track(track_id,arm=False,mute=True,gainDb=-18)
            verified=serum_controls.resolve(t['name'],a,profile=item.get('profile','serum'))
            if verified['rack']['name']!=item['name']:raise ValueError('Loaded Serum sound did not match the requested Rack.')
            return t
        time.sleep(.2)
    raise ValueError('synth Rack load was not verified; inspect the setup journal.')

def _path():return engine.DATA/'serum-setup.json'
def _save(tx):
    from transaction_store import Store
    p=_path();Store(p.parent).write(p,tx)
def prepare(adapter=None,loader=None,recipe='current-original'):
    item=serum_assets.asset(recipe);a=adapter or daw_adapter.DEFAULT;loader=loader or (lambda ident:load_rack(ident,recipe))
    daw_adapter.require(a,workflow_requirements.SERUM_SETUP,'set up this synth')
    with engine.PLAY_LOCK:
        if _path().exists():
            prior=json.loads(_path().read_text())
            history=engine.DATA/'serum-setup-history';history.mkdir(exist_ok=True)
            (history/(prior['id']+'.json')).write_text(json.dumps(prior,indent=2))
        check_asset(recipe);engine.lab_session(a)
        src=engine.STATE.get('source')
        if not src:raise ValueError('Choose an original MIDI phrase before loading the instrument with it.')
        raw=a.clip(src['id']);notes=engine.clip_notes(raw,src['length'])
        if engine.fingerprint(notes)!=src['hash'] or not engine.same_position(raw.get('start'),src['arrangementStart']):
            raise ValueError('The original MIDI changed; select it again before setup.')
        tx={'id':uuid.uuid4().hex,'stage':'starting','source':src,'notes':notes,'assetSha256':item['sha256'],'recipe':recipe}
        tx['trackName']='Scrunch '+item.get('profile','serum').title()+' - '+tx['id'][:6];_save(tx)
        try:
            engine.lab_session(a);t=a.create_muted_midi_track(tx['trackName']);tx.update(trackId=t['id'],stage='track-created');_save(tx)
            engine.lab_session(a);a.track(t['id']);a.update_track(t['id'],gainDb=-18,arm=False,mute=True)
            loader(t['id']);tx['stage']='rack-loaded';_save(tx)
            r=serum_controls.resolve(tx['trackName'],a,profile=serum_assets.asset(recipe).get('profile','serum'))
            if r['rack']['name']!=item['name']:raise ValueError('synth Rack identity differs from setup recipe.')
            t=a.track(tx['trackId'],clips=True)
            if t.get('clips') or t.get('sessionClipCount'):raise ValueError('New synth track became occupied.')
            tx['stage']='clip-writing';_save(tx)
            clip=a.create_arrangement_clip(t['trackIndex'],src['length'],notes)
            tx.update(clipId=clip['id'],stage='clip-created');_save(tx)
            got=a.clip(clip['id'])
            if got.get('start')!=0 or not engine.notes_equal(engine.clip_notes(got,src['length']),notes):
                raise ValueError('Synth phrase readback differs from the original.')
            engine.load_source(clip['id'])
            tx['stage']='complete';_save(tx)
            return f"Loaded {item["name"]} on {tx['trackName']} and copied all {len(notes)} original notes to bar 1. Original track retained; new track muted. It is now the selected phrase."
        except Exception as exc:
            tx['error']=str(exc);_save(tx);raise


def recover(adapter=None,loader=None):
    """Resume only a uniquely identified, unchanged setup; inspect before writes."""
    a=adapter or daw_adapter.DEFAULT
    daw_adapter.require(a,workflow_requirements.SERUM_SETUP,'recover this synth setup')
    with engine.PLAY_LOCK:
        if not _path().exists():raise ValueError('No Synth setup needs recovery.')
        tx=json.loads(_path().read_text())
        if tx['stage']=='complete':return 'Synth setup is already complete.'
        recipe=tx.get('recipe','current-original');check_asset(recipe);session=engine.lab_session(a)
        candidates=[t for t in session.get('tracks',[]) if t['name']==tx['trackName']]
        if len(candidates)!=1 or candidates[0]['id']!=tx.get('trackId'):raise ValueError('Setup track identity is uncertain; recovery stopped.')
        t=a.track(tx['trackId'],clips=True,devices=True,mixer=True)
        if not t.get('muted') or t.get('gainDb')!=-18 or t.get('sessionClipCount'):raise ValueError('Setup track changed; inspect before recovery.')
        if not t.get('devices'):
            if t.get('clips'):raise ValueError('Setup track became occupied.')
            (loader or (lambda ident:load_rack(ident,recipe)))(t['id'])
        r=serum_controls.resolve(tx['trackName'],a,profile=serum_assets.asset(recipe).get('profile','serum'))
        if r['rack']['name']!=serum_assets.asset(recipe)['name']:raise ValueError('Recovered Rack differs from the setup recipe.')
        t=a.track(tx['trackId'],clips=True);clips=t.get('clips',[])
        if len(clips)>1:raise ValueError('Setup has unexpected clips.')
        if not clips:
            tx['stage']='clip-writing';_save(tx)
            clip=a.create_arrangement_clip(t['trackIndex'],tx['source']['length'],tx['notes'])
        else:clip=clips[0]
        tx.update(clipId=clip['id'],stage='clip-created');_save(tx)
        raw=a.clip(clip['id'])
        if raw.get('start')!=0 or not engine.notes_equal(engine.clip_notes(raw,tx['source']['length']),tx['notes']):raise ValueError('Recovered MIDI does not match; no source promotion.')
        engine.load_source(clip['id']);tx.update(stage='complete',recovered=True);_save(tx)
        return f"Recovered Synth setup on {tx['trackName']}; all {len(tx['notes'])} original notes verified. Track remains muted."
