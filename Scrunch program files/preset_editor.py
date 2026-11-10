"""Native editor fallback, bound to the instrument selected through the host."""
import json
import subprocess
import threading
import time
from pathlib import Path

HELPER = Path(__file__).resolve().parent / 'native/preset_access'
SUPPORTED = ('Diva', 'Pigments', 'Jup-8000 V')


def step(adapter, track, synth, direction, cancelled=None):
    if direction not in ('next', 'previous'):
        raise ValueError('Unknown preset direction.')
    plugin = synth.get('deviceName') or synth['name']
    if plugin not in SUPPORTED:
        raise ValueError('No editor preset driver for %s.' % plugin)
    # Select and reveal the exact host device; the helper opens its matching editor.
    started = time.perf_counter()
    selected = adapter._request('open_plugin_window', {'device': synth['id'], 'open': True}, write=True)
    selected_at = time.perf_counter()
    title = '%s/%s' % (selected.get('deviceName', synth['name']), selected.get('trackName', track['name']))
    args = [str(HELPER), title, direction, '--instrument', plugin,
            '--device', synth['name'], '--open']
    if cancelled is not None:args.append('--handshake')
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as process:
        timer = threading.Timer(20, process.kill)
        timer.start()
        try:
            if cancelled is not None:
                ready = process.stdout.readline().strip()
                if ready != 'ready':
                    raise ValueError(process.stderr.read().strip() or 'Preset editor did not become ready.')
                allowed = not cancelled() and adapter.preset_clock()['playing']
                process.stdin.write('go\n' if allowed else 'cancel\n')
                process.stdin.flush()
            try:
                stdout, stderr = process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill();process.communicate()
                raise ValueError('The preset editor timed out; inspect the current sound before retrying.') from None
        finally:
            timer.cancel()
    if process.returncode:
        raise ValueError(stderr.strip() or 'The preset editor did not confirm a change.')
    out = json.loads(stdout)
    out['timings'] = {'selectMs': round((selected_at-started)*1000),
                      'editorMs': round((time.perf_counter()-selected_at)*1000)}
    if out.get('skipped'):return out
    if not out.get('preset') or out.get('preset') == out.get('presetBefore'):
        raise ValueError('The editor did not confirm a different preset.')
    return out
