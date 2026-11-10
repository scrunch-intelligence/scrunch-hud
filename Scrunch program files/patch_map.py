"""Offline map of one synth patch for the warm GP (Brooklyn Rose Part 6's winning method).

The synth's own state (from Scrunch Host: wrapper_client.state()) is rendered outside Live by ScrunchHostTest
--control-map at a grid of settings of the one or two controls being refined; each render is measured into the
eleven raw descriptors the warm GP was tested on (band energy minus integrated loudness, unrounded). Nothing
touches Live or the synth that is playing: the renderer is its own process on its own copy of the state.

The renderer is built from native/wrapper (tests/ControlMap.cpp) into build-maptest/, and is byte-identical to
the host every Brooklyn Rose render was made with (sha c7be3e82, checked before use).
"""
import base64
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
HOST = HERE / 'native/wrapper/build-maptest/ScrunchHostTest_artefacts/Release/ScrunchHostTest'
HOST_SHA = 'c7be3e820fc01933c688e558fae1c8393dba8ff92277954911eb58f333c4037d'
RATE = 48000
SECONDS = 30              # a render that takes longer than this is stopped (Part 6's longest: 0.4 s)
# The tested passage: one held note, three seconds (Part 6: note 60, velocity 90, on 2400, off 120000).
NOTE = {'on': 2400, 'off': 120000, 'pitch': 60, 'velocity': 90}
FRAMES, FLUSH = 144000, 48000


def host_ok(path=None):
    path = path or HOST
    return path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == HOST_SHA


def descriptors(audio):
    """Eleven raw descriptors of one render: band energy (dBFS) minus integrated loudness (LUFS), unrounded.
    Same bands and meter as the Reference page (reference_dsp)."""
    import reference_dsp
    y = reference_dsp.prepare(audio, RATE)
    r = reference_dsp.analyse(y)
    lufs = r['integratedLUFS']
    if lufs is None or any(b['energyDbFS'] is None for b in r['bands']):
        raise ValueError('silent render: no descriptors')
    return [b['energyDbFS'] - lufs for b in r['bands']]


def render(state, changes, out, notes=(NOTE,), frames=FRAMES, host=HOST, plugin='/Library/Audio/Plug-Ins/VST3/Diva.vst3'):
    """One offline render. state: bytes of the synth's VST3 state (or a path); changes: {param id: 0..1}.
    Returns (audio float32 frames x 2, result.json)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if isinstance(state, (bytes, bytearray)):
        (out / 'source.state').write_bytes(state)
        state = out / 'source.state'
    req = {'plugin': plugin, 'sample_rate': RATE, 'block_size': 512, 'frames': frames, 'flush_frames': FLUSH,
           'notes': list(notes), 'changes': [{'id': str(k), 'value': float(v)} for k, v in changes.items()],
           'state': str(state), 'compact_result': True}
    (out / 'request.json').write_text(json.dumps(req, indent=1))
    try:
        # The research containment: no network, no writes anywhere but this render's own folder.
        subprocess.run(['/usr/bin/sandbox-exec', '-p', policy(out), str(host), '--control-map', str(out / 'request.json'),
                        str(out)], timeout=SECONDS, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
                       start_new_session=True)
    except subprocess.CalledProcessError as exc:
        err = out / 'error.json'
        raise RuntimeError(json.loads(err.read_text())['error'] if err.exists() else exc.stderr.decode()[-300:])
    res = json.loads((out / 'result.json').read_text())
    for ch in res['changes']:
        if abs(ch['readback'] - ch['requested']) > 1e-6:
            raise RuntimeError('control %s read back %s, asked %s' % (ch['id'], ch['readback'], ch['requested']))
    raw = (out / 'audio.f32').read_bytes()
    audio = np.frombuffer(raw, dtype='<f4').reshape(-1, 2)
    # The future-render record (evidence/render-dataset README): request, result, PCM hash, state hash, descriptors.
    record = {'pcm_sha256': hashlib.sha256(raw).hexdigest(), 'host_sha256': HOST_SHA,
              'state_sha256': hashlib.sha256(Path(state).read_bytes()).hexdigest(),
              'plugin_version': res.get('plugin_version'), 'start_sha256': res.get('start_sha256')}
    try:
        record['descriptors'] = descriptors(audio)
    except ValueError as exc:
        record['descriptors'] = None
        record['silent'] = str(exc)
    (out / 'measurement.json').write_text(json.dumps(record, indent=1))
    return audio, res


def policy(directory):
    """No sockets and no filesystem writes outside this render's folder (control_map.policy, research)."""
    directory = Path(directory).resolve(strict=True)
    return ('(version 1)(allow default)(deny file-write*)(deny network*)'
            '(allow file-write* (subpath %s))' % json.dumps(str(directory)))


def grid(ids, per_axis=7, lo=0., hi=1.):
    """Settings to render: a per_axis grid over one or two controls (Part 6 used 7 x 9 and 7 x 7)."""
    axis = np.linspace(lo, hi, per_axis)
    if len(ids) == 1:
        return [{ids[0]: float(a)} for a in axis]
    return [{ids[0]: float(a), ids[1]: float(b)} for a in axis for b in axis]


def build(state, ids, settings=None, fixed=None, work=None, notes=(NOTE,), workers=6, budget_s=60., stop=None):
    """Render and measure every setting, several at once. Returns rows [{'x': [c0, c1], 'features': [11]}] in the
    warm GP's shape (a single control gets the fixed value of the second coordinate 0). Stops (RuntimeError) when
    `stop()` says so or the whole map would exceed budget_s; renders already running finish in their own folder."""
    from concurrent.futures import ThreadPoolExecutor
    if not host_ok():
        raise RuntimeError('map renderer missing or not the tested build (native/wrapper build-maptest)')
    settings = settings or grid(ids)
    work = Path(work or tempfile.mkdtemp(prefix='scrunch-map-'))
    if isinstance(state, (bytes, bytearray)):
        work.mkdir(parents=True, exist_ok=True)
        (work / 'source.state').write_bytes(state)
        state = work / 'source.state'
    started = time.monotonic()

    def one(i):
        if (stop and stop()) or time.monotonic() - started > budget_s:
            raise RuntimeError('map stopped before it was finished')
        s = settings[i]
        audio, _ = render(state, dict(fixed or {}, **s), work / ('%03d' % i), notes=notes)
        return {'x': [s[ids[0]], s[ids[1]] if len(ids) > 1 else 0.], 'features': descriptors(audio)}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, range(len(settings))))


def state_from_wrapper(conn):
    """The hosted synth's state, exactly as ScrunchHostTest loads it."""
    return base64.b64decode(conn.state(data=True)['data'])
