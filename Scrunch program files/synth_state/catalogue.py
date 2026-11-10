"""Which stored patch values are NOT reachable as published parameters (run offline, once per synth version).

Moves every automatable published parameter once inside Scrunch Host (offline host), reads the patch state
after each move, and records which stored paths changed. Paths never changed by any parameter are the
patch-only features the engineer reaches through the state (mod routings, sequencer steps, wavetables, ...).
Usage: python3 -m synth_state.catalogue <VST3 name> [out.json]
"""
import base64, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / 'data' / 'knowledge' / 'patch-state'


def build(vst3_name):
    sys.path[:0] = [str(ROOT), str(ROOT / 'native/wrapper/tests')]
    import wrapper_client as wc, synth_state
    from test_offline import Host
    host = Host(seconds=3600)
    try:
        w = wc.connect(pid=host.p.pid, wait=10)
        hello = w.load(f'/Library/Audio/Plug-Ins/VST3/{vst3_name}.vst3')
        child = hello['child']
        read = lambda: synth_state.open_state(child, base64.b64decode(w.state()['data'])).values()
        base = read()
        params = [p for p in w.params(text=False) if p['automatable']]
        mapping, t0 = {}, time.time()
        for p in params:
            target = 0.137 if p['value'] > 0.5 else 0.863
            if p['steps'] and p['steps'] > 1:
                target = (round(p['value'] * (p['steps'] - 1)) + 1) % p['steps'] / (p['steps'] - 1)
            w.set([{'id': p['id'], 'value': target}])
            now = read()
            changed = [k for k, v in now.items() if base.get(k) != v]
            w.set([{'id': p['id'], 'value': p['value']}])
            if changed:
                mapping[p['id']] = changed
        reached = {k for ks in mapping.values() for k in ks}
        only = sorted(k for k in base if k not in reached)
        return {'synth': child, 'vst3': vst3_name, 'built': time.strftime('%Y-%m-%d %H:%M'),
                'seconds': round(time.time() - t0), 'storedValues': len(base), 'publishedAutomatable': len(params),
                'publishedWithStoredValue': len(mapping), 'patchOnly': only, 'paramToPaths': mapping}
    finally:
        host.close()


def load(synth):
    f = KNOWLEDGE / (synth.replace(' ', '-').lower() + '.json')
    return json.loads(f.read_text()) if f.is_file() else None


if __name__ == '__main__':
    out = build(sys.argv[1])
    KNOWLEDGE.mkdir(parents=True, exist_ok=True)
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else KNOWLEDGE / (out['synth'].replace(' ', '-').lower() + '.json')
    dest.write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k not in ('patchOnly', 'paramToPaths')}), 'patchOnly', len(out['patchOnly']), '->', dest)
