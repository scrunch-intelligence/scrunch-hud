"""Bounded local JUP loader; no model-supplied scripts or shell commands."""
import subprocess,time
import engine

def script(source,*args):
    result=subprocess.run(['/usr/bin/osascript','-']+list(args),input=source,text=True,capture_output=True,timeout=15)
    if result.returncode:raise ValueError('Ableton UI helper: '+result.stderr.strip()[:300])
    return result.stdout.strip()

SEARCH='''on run argv
tell application "System Events" to tell process "Live"
set frontmost to true
perform action "AXRaise" of window (item 1 of argv)
click menu item "Search in Browser" of menu "View" of menu bar 1
end tell
end run'''
QUERY='''on run argv
tell application "System Events" to tell process "Live"
set value of text field 1 of group "Browser" of group 1 of window (item 1 of argv) to "Jup-8000 vst3"
end tell
end run'''
LOAD='''on run argv
tell application "System Events" to tell process "Live"
set frontmost to true
tell window (item 1 of argv)
set results to outline 2 of group "Browser" of group 1
if (count of rows of results) is not 1 then error "JUP search is not unique"
if name of row 1 of results is not "Jup-8000 V" then error "Unexpected browser result"
set value of attribute "AXFocused" of row 1 of results to true
end tell
key code 36
end tell
end run'''

def read_target(track_id):
    session=engine.live_set()
    if not any(t['id']==track_id for t in session['tracks']):raise ValueError('Track no longer exists.')
    return engine.host().read_track_detail(track_id,'devices','clips','mixer')

def load(track_id):
    with engine.PLAY_LOCK:
        before=read_target(track_id)
        if before.get('type')!='midi' or before.get('devices') or before.get('arrangementClips') or before.get('sessionClipCount'):
            raise ValueError('JUP loading requires an empty MIDI track with no devices or clips.')
        engine.host().select_track(track_id)
        import fresh_session
        title=fresh_session.window_title()
        script(SEARCH,title)
        time.sleep(.25)
        script(QUERY,title)
        # Search is asynchronous; no write retry after the activation below.
        time.sleep(.25)
        current=read_target(track_id)
        selected=engine.host().selected_track()
        if current.get('devices') or current.get('arrangementClips') or selected!=track_id:
            raise ValueError('The target changed while preparing JUP loading.')
        script(LOAD,title)
        for _ in range(20):
            current=read_target(track_id)
            devices=current.get('devices',[])
            if len(devices)==1 and current.get('instrument')=='Jup-8000 V':
                engine.host().update_track(track_id,arm=False)
                return read_target(track_id)
            if devices:break
            time.sleep(.2)
        raise ValueError('JUP loading was not verified. Inspect the track before retrying.')

PRESET_OPEN='''on run argv
tell application "System Events" to tell process "Live"
set frontmost to true
tell window (item 1 of argv)
perform action "AXRaise"
set position to {20, 40}
tell group 1
click button "Library toggle"
end tell
end tell
end tell
end run'''
PRESET_SEARCH='''on run argv
tell application "System Events" to tell process "Live"
set frontmost to true
tell group 1 of window (item 1 of argv)
if name of text area 2 is not "Searchbar" then error "Preset search not found"
set value of attribute "AXFocused" of text area 2 to true
set value of text area 2 to (item 2 of argv)
end tell
end tell
end run'''
PRESET_APPLY='''on run argv
tell application "System Events" to tell process "Live"
set frontmost to true
perform action "AXRaise" of window (item 1 of argv)
set position of window (item 1 of argv) to {20, 40}
set value of attribute "AXMain" of window (item 1 of argv) to true
tell group 1 of window (item 1 of argv)
if value of text area 2 is not (item 2 of argv) then error "Preset query changed"
set p to position of row 1
end tell
return ((item 1 of p) + 150) & "," & ((item 2 of p) + 20) as text
end tell
end run'''
PRESET_READ='''on run argv
tell application "System Events" to tell process "Live"
tell group 1 of window (item 1 of argv)
return name of first button whose description is "Current preset"
end tell
end tell
end run'''

