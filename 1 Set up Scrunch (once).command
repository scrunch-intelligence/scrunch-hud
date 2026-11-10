#!/bin/zsh
# Scrunch Intelligence setup: everything in one go. Run it once; running it again only fills in what is missing.
#   1. Python 3.12 and the Mixing runtime            (this folder only)
#   2. The Scrunch Bridge for Ableton Live + Live Ears (your Live User Library / Audio Plug-Ins)
#   3. Audio-to-MIDI and Hum-to-MIDI                  (this folder only; about 6.5 GB, mostly the MuScriptor model)
#   4. The Start Scrunch app in your Dock
# Nothing is installed system-wide and no admin password is needed. "Uninstall Scrunch" removes all of it.
set -eu
top="${0:A:h}"; cd "$top/Scrunch program files"   # this folder holds Scrunch itself
export PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Downloaded files are quarantined, and macOS then refuses to load Scrunch's own libraries and apps (it hangs
# waiting on a hidden security check). Clear it on this Scrunch folder only; nothing else on the Mac changes.
xattr -dr com.apple.quarantine "$top" 2>/dev/null || true
step(){ print ''; print "== $1"; }
step '1 of 4  Python and the Mixing runtime'
scrunch_mixing_python=''
# A Python this setup fetched earlier (python-runtime/) is tried first, then the usual installs.
for scrunch_candidate in "$PWD/python-runtime/python/bin/python3.12" /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3.12 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3; do
  if [[ -x "$scrunch_candidate" ]] && "$scrunch_candidate" -B -c 'import sys; raise SystemExit(not ((3,12) <= sys.version_info[:2] < (3,13)))'; then
    scrunch_mixing_python="$scrunch_candidate"
    break
  fi
done
if [[ -z "$scrunch_mixing_python" && "$(uname -m)" == arm64 ]]; then
  # No Python 3.12 on this Mac: fetch one pinned, checksum-verified standalone build (python-build-standalone,
  # the same build uv uses) into this folder only. Nothing is installed system-wide; delete the folder to remove it.
  scrunch_py_url='https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.12.15%2B20261003-aarch64-apple-darwin-install_only_stripped.tar.gz'
  scrunch_py_sha='ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2'
  print 'Python 3.12 was not found. Downloading a private copy for this folder (25 MB, checksum checked)...'
  rm -rf python-runtime python-runtime.tar.gz
  if curl -fL --retry 2 --connect-timeout 20 -o python-runtime.tar.gz "$scrunch_py_url" &&
     [[ "$(shasum -a 256 python-runtime.tar.gz | cut -d' ' -f1)" == "$scrunch_py_sha" ]]; then
    mkdir python-runtime && tar -xzf python-runtime.tar.gz -C python-runtime && rm -f python-runtime.tar.gz
    if "$PWD/python-runtime/python/bin/python3.12" -B -c 'import sys; raise SystemExit(sys.version_info[:2] != (3,12))'; then
      scrunch_mixing_python="$PWD/python-runtime/python/bin/python3.12"
    fi
  else
    rm -rf python-runtime python-runtime.tar.gz
    print 'The download failed or did not match its checksum, so nothing was kept.'
  fi
fi
if [[ -z "$scrunch_mixing_python" ]]; then
  print 'Install Python 3.12 for macOS from python.org, then reopen this setup.'
  print 'This preview uses Python 3.12 and does not require Codex.'
  read 'scrunch_mixing_reply?Press Return to close.'
  exit 1
fi
if [[ ! -d .venv ]]; then "$scrunch_mixing_python" -B -m venv .venv; fi
if ! .venv/bin/python3 -B -c 'import sys; raise SystemExit(sys.version_info[:2] != (3,12))'; then
  print 'The existing environment uses a different Python version. Use a fresh Scrunch Intelligence folder.'
  read 'scrunch_mixing_reply?Press Return to close.'
  exit 1
fi
nice -n 10 .venv/bin/python3 -B -m pip install -q --no-cache-dir --only-binary=:all: -r mixing-requirements.txt

step '2 of 4  Ableton Live: Scrunch Bridge and Live Ears'
live_lib="$HOME/Music/Ableton/User Library"
if [[ -d "$live_lib" ]]; then
  mkdir -p "$live_lib/Remote Scripts"
  rm -rf "$live_lib/Remote Scripts/ScrunchBridge"   # an older Scrunch Bridge is simply replaced; nothing else is kept
  cp -R live-bridge/ScrunchBridge "$live_lib/Remote Scripts/ScrunchBridge"
  # Live Ears: the small Audio Unit Live capture puts on a temporary track to hear the Main output.
  au="$HOME/Library/Audio/Plug-Ins/Components"; mkdir -p "$au"
  rm -rf "$au/ScrunchLiveEarsBound.component"; cp -R live-bridge/ScrunchLiveEarsBound.component "$au/"
  # Downloaded files are quarantined; clear it on these two Scrunch files only (not a system-wide change).
  xattr -dr com.apple.quarantine "$au/ScrunchLiveEarsBound.component" native/live_ears/build/libScrunchReceiver.dylib 2>/dev/null || true
  print 'Installed.'
  live_ok=1
else
  print 'Ableton Live User Library not found (~/Music/Ableton/User Library). Open Live once, then run this setup again.'
  live_ok=0
fi

step '3 of 4  Audio-to-MIDI and Hum-to-MIDI'
models="$PWD/data/midi-models"; mvenv="$models/venvs/muscriptor"
mkdir -p "$models/venvs" "$models/checkpoints/muscriptor-large" "$models/torch/hub/checkpoints"
if [[ ! -x "$mvenv/bin/python" ]]; then "$scrunch_mixing_python" -B -m venv "$mvenv"; fi
print 'Installing the MIDI runtime (about 1 GB the first time)...'
nice -n 10 "$mvenv/bin/python3" -B -m pip install -q --no-cache-dir --only-binary=:all: -r midi-requirements.txt
beat="$models/torch/hub/checkpoints/beat_this-final0.ckpt"
if [[ ! -f "$beat" ]]; then
  print 'Downloading the beat tracker (80 MB, checksum checked)...'
  if curl -fL --progress-bar --retry 2 --connect-timeout 20 -o "$beat.part" 'https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt' &&
     [[ "$(shasum -a 256 "$beat.part" | cut -d' ' -f1)" == 8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331 ]]; then
    mv "$beat.part" "$beat"
  else rm -f "$beat.part"; print 'The beat tracker download failed; run this setup again later.'; fi
fi
weights="$models/checkpoints/muscriptor-large/model.safetensors"
if [[ ! -f "$weights" || "$(stat -f %z "$weights")" != 5465642136 ]]; then
  print ''
  print 'Audio-to-MIDI uses the MuScriptor large model (5.5 GB). Its makers ask everyone to sign in to'
  print 'Hugging Face and accept their non-commercial licence before downloading. Free, about two minutes:'
  print '  a. A browser page opens. Sign in or create a free Hugging Face account, then click "Agree" on the model page.'
  print '  b. A second page opens: Access Tokens. Click "Create new token", choose "Read", create it, copy it.'
  print '  c. Come back here and paste the token, then press Return. (It is used for this download only and not saved.)'
  print 'Press Return without a token to skip; everything else still works and you can run setup again later.'
  open 'https://huggingface.co/MuScriptor/muscriptor-large'; sleep 2; open 'https://huggingface.co/settings/tokens'
  read -s 'hf_token?Hugging Face token: '; print ''
  if [[ -n "$hf_token" ]]; then
    hdr="$(mktemp)"; chmod 600 "$hdr"; print -r -- "Authorization: Bearer $hf_token" > "$hdr"; unset hf_token
    base='https://huggingface.co/MuScriptor/muscriptor-large/resolve/main'
    print 'Downloading MuScriptor large (5.5 GB; this can take 10-30 minutes)...'
    if curl -fL --progress-bar --retry 3 --connect-timeout 20 -C - -H @"$hdr" -o "$weights.part" "$base/model.safetensors" &&
       curl -fsL -H @"$hdr" -o "${weights:h}/config.json" "$base/config.json"; then
      print 'Checking the download...'
      if [[ "$(shasum -a 256 "$weights.part" | cut -d' ' -f1)" == ac4eb6ea87dfc26b6ca6b954c6b967ab87ad4c7d08e078b25214f13ed051f397 ]]; then
        mv "$weights.part" "$weights"; chmod a-w "$weights"; print 'MuScriptor large installed.'
      else rm -f "$weights.part"; print 'The download did not match its checksum and was deleted. Run setup again.'; fi
    else
      print 'The download was refused or interrupted. Check you clicked "Agree" on the model page and the token is a Read token,'
      print 'then run setup again (an interrupted download resumes).'
    fi
    rm -f "$hdr"
  else print 'Skipped. Audio-to-MIDI will show "not installed" until you run setup again.'; fi
fi
# Instrument samples for playing hummed melodies back on the humming page.
mkdir -p data/dream; cp -R dream-samples/ data/dream/

step '4 of 4  The Start Scrunch app'
nice -n 10 .venv/bin/python3 -B tools/mixing_readiness.py >/dev/null
app="$top/Start Scrunch.app"
if [[ -d "$app" ]] && ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Start%20Scrunch.app"; then
  defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file://${app// /%20}/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>" && killall Dock
fi
print ''
print 'Setup complete. Start Scrunch (orange logo) is now in your Dock. Click it any time to open Scrunch;'
print 'closed the browser? Click it again.'
if (( live_ok )); then
  print ''
  print 'For Ableton Live, once: quit Live and open it again, then Live > Settings > Link, Tempo & MIDI,'
  print 'choose ScrunchBridge in an empty Control Surface slot (Input and Output: None).'
fi
read 'scrunch_mixing_reply?Press Return to start Scrunch now.'
open "$app"
