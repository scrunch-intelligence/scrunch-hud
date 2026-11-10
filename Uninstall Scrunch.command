#!/bin/zsh
# Removes everything Scrunch Intelligence put on this Mac: this folder (with its private Python), the Live bridge,
# the Live Ears capture plug-in, Mixing's settings and saved conversations, and any AI keys it stored in Keychain.
# Your music, bounces and Ableton Sets are never touched.
set -u
dir="${0:A:h}"
print 'This removes Scrunch Intelligence from this Mac:'
print "  - this folder: $dir"
print '  - Live bridge:  ~/Music/Ableton/User Library/Remote Scripts/ScrunchBridge'
print '  - capture plug-in: ~/Library/Audio/Plug-Ins/Components/ScrunchLiveEarsBound.component'
print '  - settings and saved chats: ~/Library/Application Support/Scrunch/Mixing'
print '  - the Scrunch HUD settings, and AI keys Scrunch saved in Keychain'
print 'Your music, bounces and Ableton Sets are not touched. Quit Live first if it is open.'
read 'answer?Type yes to remove: '
[[ "$answer" == yes ]] || { print 'Nothing was removed.'; read 'r?Press Return to close.'; exit 0; }
pkill -f "mixing_demo.py --no-open --port 3361" 2>/dev/null   # stop Scrunch if it is running
[[ -f "$dir/Scrunch program files/data/dream.pid" ]] && kill "$(cat "$dir/Scrunch program files/data/dream.pid")" 2>/dev/null; pkill -f "Scrunch HUD.app/Contents/MacOS/scrunch-aar" 2>/dev/null
# Take the Start Scrunch app out of the Dock (through cfprefsd, so the Dock does not write the old list back).
defaults export com.apple.dock - | /usr/bin/python3 -c '
import plistlib,sys
d=plistlib.loads(sys.stdin.buffer.read())
d["persistent-apps"]=[a for a in d.get("persistent-apps",[]) if not any(n in a.get("tile-data",{}).get("file-data",{}).get("_CFURLString","") for n in ("Start%20Scrunch.app","Scrunch%20Mixing.app"))]
plistlib.dump(d,sys.stdout.buffer)' > "${TMPDIR:-/tmp}/scrunch-dock.plist" && defaults import com.apple.dock "${TMPDIR:-/tmp}/scrunch-dock.plist" && killall Dock
rm -f "${TMPDIR:-/tmp}/scrunch-dock.plist"
rm -rf "$HOME/Music/Ableton/User Library/Remote Scripts/ScrunchBridge" \
       "$HOME/Library/Audio/Plug-Ins/Components/ScrunchLiveEarsBound.component" \
       "$HOME/Library/Application Support/Scrunch/Mixing" \
       "$HOME/Library/Application Support/Scrunch/aar-visible-prototype" "$HOME/Library/Logs/Scrunch Intelligence.log"
rmdir "$HOME/Library/Application Support/Scrunch" 2>/dev/null
keys=0; while security delete-generic-password -s org.scrunch.midi-engineer >/dev/null 2>&1; do keys=$((keys+1)); done
cd /; rm -rf "$dir"
print "Scrunch Intelligence removed ($keys saved key(s) deleted from Keychain)."
print 'If Live is open, restart it; its Control Surface slot will then show None.'
read 'r?Press Return to close.'
