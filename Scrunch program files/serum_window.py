"""Show a specific Serum instance in Live's floating plug-in window."""
import jup_bridge
import engine, json, subprocess, time, re
from pathlib import Path

SHOW = '''on run argv
tell application "System Events" to tell process "Live"
set frontmost to true
set targetName to "Serum 2/" & item 1 of argv
if not (exists window targetName) then
error "Serum editor did not open on the requested track"
end if
if not (exists window targetName) then error "Serum window is not open on this track"
set position of window targetName to {(item 2 of argv) as integer, (item 3 of argv) as integer}
perform action "AXRaise" of window targetName
return name of window targetName
end tell
end run'''

SCREENS = '''ObjC.import("AppKit"); var s=$.NSScreen.screens, out=[];
for (var i=0;i<s.count;i++){var f=s.objectAtIndex(i).frame; out.push([f.origin.x,f.origin.y,f.size.width,f.size.height]);}
JSON.stringify(out)'''


def placement():
    """Top-left point for the Serum editor: the second display when there is one (John keeps Live's mixer and the
    Scrunch engineer on the main display, 27 Sep 2026), else the main display's corner."""
    try:
        frames = json.loads(subprocess.check_output(['osascript', '-l', 'JavaScript', '-e', SCREENS], text=True, timeout=5))
    except Exception:
        return 40, 80
    if len(frames) < 2:
        return 40, 80
    main_h = frames[0][1] + frames[0][3]
    x, y, w, h = frames[1]
    return int(x + 40), int(main_h - (y + h) + 80)       # Cocoa (bottom-left) -> System Events (top-left)


def show(track_name, device_id=None):
    if device_id:
        engine.host().open_plugin_window(device_id)
    x, y = placement()
    return jup_bridge.script(SHOW, track_name, str(x), str(y))

OPEN_FILE = '''on run argv
tell application id "com.ableton.live" to activate
tell application "System Events" to tell process "Live"
set frontmost to true
repeat 30 times
if exists window "Open" then exit repeat
delay 0.1
end repeat
if not (exists window "Open") then error "Serum preset chooser did not open"
perform action "AXRaise" of window "Open"
return "ready"
end tell
end run'''

def read_name(track_name):
    output=subprocess.check_output([str(engine.ROOT/'native/serum_readback'),'Serum 2/'+track_name],text=True,timeout=10)
    return json.loads(output)

CANCEL_FILE = '''tell application "System Events" to tell process "Live"
if exists window "Open" then
if exists sheet 1 of window "Open" then click button 1 of sheet 1 of window "Open"
if exists button "Cancel" of splitter group 1 of window "Open" then click button "Cancel" of splitter group 1 of window "Open"
end if
end tell'''

def load_preset(track_name, record):
    path=Path(record['sourcePath'])
    if not path.is_file() or path.suffix.lower() not in ('.fxp','.serumpreset'):
        raise ValueError('This Serum preset file is unavailable. Reconnect its drive or choose another preset.')
    show(track_name)
    time.sleep(.3)
    point=jup_bridge.script('''on run argv
tell application "System Events" to tell process "Live"
set p to position of window (item 1 of argv)
set s to size of window (item 1 of argv)
return (((item 1 of p) + (item 1 of s) * 0.892) as text) & "," & (((item 2 of p) + (item 2 of s) * 0.047) as text)
end tell
end run''','Serum 2/'+track_name).split(',')
    subprocess.run([str(engine.ROOT/'ui_click')]+[str(float(x.strip())) for x in point],check=True,timeout=5)
    try:
        time.sleep(.2)
        subprocess.run([str(engine.ROOT/'native/serum_access'),'menu'],check=True,capture_output=True,text=True,timeout=8)
        jup_bridge.script(OPEN_FILE,str(path))
        # serum_access has its own 40 s navigation deadline (one step per folder level on deep paths).
        done=subprocess.run([str(engine.ROOT/'native/serum_access'),'file',str(path)],check=True,capture_output=True,text=True,timeout=50)
        import checkpoints
        checkpoints.event('serum-loader-trace',path=str(path),trace=done.stderr[-4000:])
    except Exception as exc:
        import checkpoints
        detail=getattr(exc,'stderr','')
        if isinstance(detail,bytes):detail=detail.decode('utf-8','replace')
        checkpoints.event('serum-loader-error',error=str(exc),detail=(detail or '')[-4000:])
        try:jup_bridge.script(CANCEL_FILE)
        except Exception:pass
        raise ValueError('The preset chooser did not finish loading. MIDI and the current sound are retained; the editor remains available.') from exc
    normalize=lambda value:re.sub(r'[^a-z0-9]','',value.lower())
    expected={normalize(record['name']),normalize(path.stem)}
    for _ in range(5):
        lines=read_name(track_name)
        matched=next((line for line in lines if normalize(line) in expected),None)
        if matched:
            return {'presetId':record['presetId'],'name':matched.rstrip(' *'),'readback':lines}
        time.sleep(.2)
    raise ValueError('Serum preset name did not match the requested sound. The current MIDI remains available.')
