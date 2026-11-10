#!/bin/zsh
# Start Scrunch app: opens Scrunch in the browser; if Scrunch is not running yet, starts it quietly first, with the
# humming page (Dream, port 3352) and the Scrunch HUD over Live. Clicking it again after closing the browser just
# brings the page back. The app lives inside the Scrunch Intelligence folder and runs the copy in that folder.
dir="${0:A:h}/../../../Scrunch program files"; dir="${dir:A}"   # Contents/Resources -> Scrunch Intelligence/Scrunch program files
port=3361; dream_port=3352; base="http://127.0.0.1:$port"
log="$HOME/Library/Logs/Scrunch Intelligence.log"
say_box(){ osascript -e "display dialog \"$1\" with title \"Scrunch\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1; }
up(){ curl -fs --max-time 1 -o /dev/null "$1"; }
# First start: the guided engineer and voice setup. Afterwards: Mixing.
page="$base/mix-audit"; [[ -f "$dir/data/started" ]] || page="$base/"
hud(){ [[ -d "$dir/Scrunch HUD.app" ]] && ! pgrep -qf "Scrunch HUD.app/Contents/MacOS/scrunch-aar" && open -g "$dir/Scrunch HUD.app"; }
if up "$base/mix-audit"; then hud; open "$page"; exit 0; fi
if [[ ! -x "$dir/.venv/bin/python3" ]]; then say_box "Scrunch is not set up yet. Open the Scrunch Intelligence folder and double-click \\\"1 Set up Scrunch (once)\\\" first."; exit 1; fi
if lsof -ti tcp:$port >/dev/null 2>&1; then say_box "Another program is using Scrunch's port ($port). Restart your Mac, then click Start Scrunch again."; exit 1; fi
mkdir -p "$HOME/Library/Logs" "$dir/data"; cd "$dir"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
nohup nice -n 10 .venv/bin/python3 -B mixing_demo.py --no-open --port $port >> "$log" 2>&1 &
if ! up "http://127.0.0.1:$dream_port/" && ! lsof -ti tcp:$dream_port >/dev/null 2>&1; then
  nohup nice -n 10 .venv/bin/python3 -B dream_server.py >> "$log" 2>&1 &
  print $! > data/dream.pid
fi
for i in {1..40}; do
  if up "$base/mix-audit"; then hud; open "$page"; touch "$dir/data/started"; exit 0; fi
  sleep 0.5
done
say_box "Scrunch did not start. Details are in ~/Library/Logs/Scrunch Intelligence.log."; exit 1
