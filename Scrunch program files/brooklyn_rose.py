"""Brooklyn Rose in the live path: a measured search that moves many synth controls together toward a reference.

One request (engineer op 'search', POST /api/search, or the dashboard button) runs, on the synth hosted in Scrunch
Host:

1. Listen: capture the synth now and compare it with the reference passage (reference_target.compare_audio,
   level-matched), plus sound_audio.measure of both sides (loudness, brightness centre, bands).
2. Space from presets: the hosted synth's own state is parsed (synth_state), every preset in its library is parsed
   (uhe_presets) and the presets nearest this patch (plus a spread across the library) are written into copies of
   the state and rendered OFFLINE by the tested map renderer (patch_map.render: sandboxed, never Live). Each render
   gives the preset's full control vector in the host's own 0..1 space and its eleven raw descriptors. The two
   presets predicted closest to the reference set two directions; a point (a, b) moves every timbre control that
   differs, jointly: value = now + a * (preset1 - now) + b * (preset2 - now).
3. Map: a 7 x 7 grid over (a, b) rendered offline from this state, shifted so its prediction at (0, 0) equals what
   was measured live (held note -> his passage), as warm_gp_engine does.
4. PLAN (a prediction, nothing heard yet): measured now vs target, the predicted result at the warm start, and the
   full list of control moves (from -> to). Returned, shown, appended to data/search-plans.jsonl before any write.
5. Constrained, staged (25 Sep: one 88-control jump of up to 1.0 per control was musically unusable). The map's
   best point is only a direction. The move toward it is split into stages, one module of the synth per stage (the
   module is the synth's own parameter group / name prefix, e.g. Diva's VCF1, ENV1, OSC: plumbing, not words).
   Every control moves at most MAX_STEP (0..1) per try, a stage at most MAX_STAGE_TOTAL summed, the whole search
   at most MAX_TOTAL summed. Which modules run, and in what order, is decided by measurement: the offline-predicted
   error of each module's capped move alone on the patch map (one render per module), largest expected gain first.
   This ordering is DIAGNOSTIC, not the permanent decision-maker (a supervisor/critic layer is to come).
   Pitch/voice-style controls (tune, fine, glide, portamento, slop/drift, voice/poly, bend, octave, transpose) never
   move unless the caller lists them explicitly.
6. Execute through Scrunch Host with compare-and-set writes and readback: one stage per try, measured after each;
   a stage is kept only if it measured closer to the reference than the best so far, otherwise that module is
   written straight back (readback-checked). Then the outcome is measured by DSP (sound_audio.measure + distance to
   the reference), stored next to the prediction. (CLAP was removed on 26 Sep 2026: it disagreed with John's blind
   picks; evidence/clap-vs-john-2026-09-26.)

Never reads or writes the Set file, never saves or reloads the Set. No word handling: what to search toward is the
reference passage he set; which controls may move comes from the synth's own preset data (or the engineer's list).
"""
import json
import shutil
import tempfile
import threading
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

PLANS_FILE = 'search-plans.jsonl'
NEAREST, SPREAD = 16, 16            # presets rendered: nearest this patch, plus a spread across the library
GRID = 7
WORKERS = 6
MAX_TRIES = 4
# Host parameter groups that are voice architecture, output level or display, not timbre: moving them changes his
# notes (pitch, polyphony, glide) or only the level, which the level-matched measure removes.
HELD_GROUPS = frozenset({'Main Output', 'Core', 'VoiceCircuit'})
# Step constraints (normalised 0..1 host values).
MAX_STEP = 0.10          # any one control, per try
MAX_STAGE_TOTAL = 0.60   # sum of |moves| in one try (one module)
MAX_TOTAL = 1.50         # sum of |moves| from where he was, over the whole search
MAX_STAGES = MAX_TRIES
# Pitch / voice-architecture controls, recognised from the synth's own parameter names (word roots of the name,
# split on spaces, digits, ':' and CamelCase). Never moved unless the caller includes them by id or name.
PROTECTED_ROOTS = ('tune', 'fine', 'glide', 'porta', 'slop', 'drift', 'voice', 'poly', 'bend', 'octave',
                   'transp', 'semi', 'coarse', 'pitch')
SYNTH_CODEC = {'Diva': 'Diva'}      # synths whose state and preset library can be parsed for the search space


class NotReady(ValueError):
    pass


class Cancelled(Exception):
    """John pressed Cancel/Stop: stop at the next safe point and put the original back (verified)."""


class ScoringFailed(Exception):
    """A required DSP score (per stage and at the outcome) could not be computed: the search
    stops, keeps nothing and puts the original back with verified writes."""


# ---------------------------------------------------------------- run status (visible while a search runs)
HEARTBEAT_S = 1.0          # the status heartbeat while a search works (John: at least every 2 s)
_RUN_LOCK = threading.Lock()
RUN = {}                   # the current / last search: read by /api/search/status and the HUD feed, never engine-locked


def _status(**kw):
    with _RUN_LOCK:
        RUN.update(kw)


def status():
    """A copy of the run record with elapsed seconds now (no engine lock: readable while the search holds it)."""
    with _RUN_LOCK:
        out = dict(RUN)
    if out.get('startedAt'):
        out['elapsedS'] = round((out.get('endedAt') or time.time()) - out['startedAt'], 1)
        if out.get('running'):          # only while working, so an idle panel feed stays unchanged
            out['heartbeatAgeS'] = round(time.time() - out.get('heartbeatAt', out['startedAt']), 1)
    return out


def cancel(run_id=None):
    """Ask the running search to stop at its next safe point and put the original back."""
    with _RUN_LOCK:
        if not RUN.get('running') or (run_id and run_id != RUN.get('runId')):
            return {'cancelled': False, 'why': 'no search is running'}
        RUN['cancelRequested'] = True
        RUN['phase'] = 'cancelling'
        return {'cancelled': True, 'runId': RUN.get('runId')}


def _cancel_requested():
    with _RUN_LOCK:
        return bool(RUN.get('cancelRequested'))


def _heartbeat(stop_event):
    while not stop_event.wait(HEARTBEAT_S):
        _status(heartbeatAt=time.time())


# ---------------------------------------------------------------- helpers
def _rms(e):
    e = np.asarray(e, dtype=float)
    return float(np.sqrt(np.mean(e * e)))


def _numeric(text):
    try:
        float(str(text).strip().split(' ')[0])
        return True
    except (TypeError, ValueError):
        return False


def _append(row):
    import engine
    path = engine.DATA / PLANS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps(row, default=float) + '\n')


def _dsp(y, sr):
    import sound_audio
    y = np.asarray(y, dtype=float)
    m = sound_audio.measure(y, sr, y.shape[1] if y.ndim > 1 else 1)
    return {'loudnessLUFS': m['loudnessLUFS'], 'brightnessHz': m['brightnessHz'], 'highShare': m['highShare'],
            'bands': {b['name']: None if b['energyDbFS'] is None else round(b['energyDbFS'], 2) for b in m['bands']}}


# ---------------------------------------------------------------- the host side
class Live:
    """The hosted synth through the coach: capture, measure, compare-and-set writes with readback."""

    def __init__(self, c, target, evidence_dir=None):
        self.c, self.target, self.evidence_dir = c, target, evidence_dir
        self.device = c.device or c._device()

    def knobs(self):
        return {k['parameter']: k for k in self.c.knobs_view()}

    def listen(self):
        import reference_target
        y, sr = self.c.capture_raw(self.target['durationSeconds'])
        m = reference_target.compare_audio(y, sr, self.target, self.evidence_dir)
        if not m.get('descriptors'):
            raise ValueError('the capture could not be measured on the eleven-band descriptors')
        return m, (np.asarray(y, dtype=float), sr)

    def write(self, values, expected):
        """values / expected: {id: 0..1}. Every write carries what Scrunch last saw there; readback is checked."""
        knobs = self.knobs()
        writes = [{'id': p, 'name': knobs[p]['name'], 'value': round(float(v), 6), 'expect_value': expected[p]}
                  for p, v in values.items()]
        back = self.c.a.set_device_parameters_together(self.device, writes)
        got = {a.get('id'): a.get('value') for a in back or []}
        bad = [w['name'] for w in writes if got.get(w['id']) is None or abs(got[w['id']] - w['value']) > 1e-4]
        if bad:
            raise ValueError('%d control(s) did not read back as written: %s' % (len(bad), ', '.join(bad[:4])))
        return {p: float(got[p]) for p in values}


def _words(name):
    """'OSC TuneModOsc3' -> ['osc', 'tune', 'mod', 'osc']: the synth's parameter name split into its parts."""
    out, cur = [], ''
    for ch in str(name or ''):
        if not ch.isalpha():
            if cur:
                out.append(cur)
            cur = ''
        elif ch.isupper() and cur and not cur[-1].isupper():
            out.append(cur)
            cur = ch
        elif ch.islower() and len(cur) > 1 and cur[-1].isupper() and cur[-2].isupper():   # 'PWSlop' -> PW, Slop
            out.append(cur[:-1])
            cur = cur[-1] + ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return [w.lower() for w in out]


def protected(knob):
    """A pitch / voice-style control (tune, fine, glide, slop, voice, bend, octave, ...) by its own name."""
    words = _words(knob.get('name')) + _words(knob.get('label')) + _words(knob.get('group'))
    return any(w.startswith(r) for w in words for r in PROTECTED_ROOTS)


def module(knob):
    """The synth module a control belongs to: its host parameter group, else the name's prefix before ':' or ' '."""
    g = knob.get('group')
    if g:
        return str(g)
    name = str(knob.get('name') or knob.get('label') or '')
    head = name.split(':', 1)[0] if ':' in name else name.split(' ', 1)[0]
    return head.strip() or name


def stage_order(scored, now_rms):
    """DIAGNOSTIC ordering (not the permanent decision-maker): modules by measured expected gain, i.e. the
    offline-predicted error of each module's capped move alone, smallest error first; only modules predicted to
    help (at most MAX_STAGES), else the single least-bad one. scored: [(predicted error dB, module, ...)]."""
    ranked = sorted(scored, key=lambda r: r[0])
    return [r[1] for r in ranked if r[0] < now_rms][:MAX_STAGES] or [r[1] for r in ranked[:1]]


def cap_stages(ids, cur, to, knobs, order=None, total_cap=None):
    """Split the move cur -> to into one stage per module, each control capped at MAX_STEP, each stage at
    MAX_STAGE_TOTAL (scaled down together), and the whole search at MAX_TOTAL. order: modules in stage order
    (default: first appearance). Returns [{'group', 'idx': [...], 'delta': [...]}] with no empty stage."""
    groups = {}
    for i, p in enumerate(ids):
        groups.setdefault(module(knobs[p]), []).append(i)
    stages, left = [], MAX_TOTAL if total_cap is None else total_cap
    for g in (order or list(groups)):
        idx = [i for i in groups.get(g, []) if abs(to[i] - cur[i]) > 1e-4]
        if not idx or left <= 1e-6:
            continue
        d = np.clip(np.array([to[i] - cur[i] for i in idx]), -MAX_STEP, MAX_STEP)
        total = float(np.sum(np.abs(d)))
        scale = min(1., MAX_STAGE_TOTAL / total, left / total) if total > 0 else 0.
        d = d * scale
        new = np.clip(np.array([cur[i] for i in idx]) + d, 0., 1.)
        d = new - np.array([cur[i] for i in idx])
        keep = np.abs(d) > 1e-4
        if not keep.any():
            continue
        idx, d = [i for i, k in zip(idx, keep) if k], d[keep]
        left -= float(np.sum(np.abs(d)))
        stages.append({'group': g, 'idx': idx, 'delta': [float(x) for x in d]})
    return stages


# ---------------------------------------------------------------- the search space from preset data
def preset_space(state, library, codec='Diva'):
    """Parsed current patch + library -> [(preset, state blob)] for the nearest presets and a spread."""
    import synth_state
    import uhe_presets
    now = {k: v for k, v in synth_state.open_state(codec, state).values().items()
           if isinstance(v, (int, float)) and k not in uhe_presets.RENUMBERED}
    keys = sorted(now)
    rows = [(p, np.array([p['parameters'].get(k, np.nan) for k in keys], dtype=float)) for p in library]
    matrix = np.array([r for _, r in rows]) if rows else np.zeros((0, len(keys)))
    with np.errstate(all='ignore'), warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)       # keys no preset stores
        span = np.nanmax(matrix, axis=0) - np.nanmin(matrix, axis=0) if len(rows) else np.ones(len(keys))
        dist = np.array([np.nanmean(np.abs(r - np.array([now[k] for k in keys], dtype=float)) / np.where(
            np.isfinite(span) & (span > 0), span, 1.)) if np.isfinite(r).any() else np.inf for r in matrix])
    seen, order = set(), []
    for i in np.argsort(dist):                 # one of each preset name (libraries repeat some across folders)
        if dist[i] > 1e-9 and np.isfinite(dist[i]) and library[i].get('name') not in seen:
            seen.add(library[i].get('name'))
            order.append(i)
    chosen = order[:NEAREST]
    rest = order[NEAREST:]
    if rest:
        chosen += [rest[int(j)] for j in np.linspace(0, len(rest) - 1, min(SPREAD, len(rest)))]
    out = []
    for i in dict.fromkeys(chosen):
        p = library[i]
        ps = synth_state.open_state(codec, state)
        have = ps.values()
        for k, v in p['parameters'].items():
            if k in have and k not in uhe_presets.RENUMBERED and isinstance(have[k], (int, float)):
                ps.set(k, v)
        out.append(({'name': p.get('name'), 'path': p.get('path'), 'categories': p.get('categories', [])[:3],
                     'distance': round(float(dist[i]), 4)}, ps.blob()))
    return out


def _render_all(jobs, work, stop=None, budget_s=90.):
    """jobs: [(state bytes, {vst id: 0..1})] -> [(descriptors or None, parameters_after)] rendered offline."""
    import patch_map
    started = time.monotonic()

    def one(i):
        if (stop and stop()) or time.monotonic() - started > budget_s:
            raise RuntimeError('offline renders stopped before they finished')
        state, changes = jobs[i]
        audio, res = patch_map.render(state, changes, Path(work) / ('%03d' % i))
        try:
            d = patch_map.descriptors(audio)
        except ValueError:
            d = None
        return d, res.get('parameters_after') or []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(one, range(len(jobs))))


def controls_to_move(knobs, rendered_now, allowed=None):
    """Host ids of timbre controls this search may move (continuous, numeric display, not a held group, not a
    pitch/voice-style control unless `allowed` - the caller's explicit list - names it)."""
    info = {str(p['id']): p for p in rendered_now if p.get('automatable')}
    out = []
    for pid, k in knobs.items():
        vst = str(pid)[2:] if str(pid).startswith('w:') else str(pid)
        p = info.get(vst)
        if p is None or k.get('stepped') or p.get('discrete') or p.get('boolean'):
            continue
        if (k.get('group') or p.get('group')) in HELD_GROUPS or not _numeric(p.get('text')):
            continue
        if allowed is not None and pid not in allowed:
            continue
        if protected(k) and (allowed is None or pid not in allowed):
            continue
        out.append(pid)
    return out


def _vector(params, ids):
    by = {str(p['id']): p for p in params}
    return np.array([by[str(i)[2:]]['value'] for i in ids], dtype=float), [by[str(i)[2:]].get('text') for i in ids]


def point(cur, v1, v2, x):
    return np.clip(cur + x[0] * (v1 - cur) + x[1] * (v2 - cur), 0., 1.)


# ---------------------------------------------------------------- plan
def make_plan(c, target, controls=None, stop=None, library=None, work=None, suggested=None):
    """Everything up to (not including) the first write to the live synth. Returns (plan, context for execute)."""
    import patch_map
    import reference_target
    import warm_gp_engine
    plugin = (getattr(c, 'm', None) or {}).get('plugin')
    codec = SYNTH_CODEC.get(plugin)
    if codec is None:
        raise NotReady('The search reads the synth\'s own preset data; %s is not one it can parse yet (Diva is).' % plugin)
    state = warm_gp_engine.hosted_state(c)
    if state is None:
        raise NotReady('%s is not in Scrunch Host, so its state cannot be read for the offline map.' % plugin)
    if not patch_map.host_ok():
        raise NotReady('The offline map renderer is missing or not the tested build (native/wrapper build-maptest).')
    if not target:
        raise NotReady('No reference passage is set: choose a reference and passage for the engineer first.')
    T = {}
    t0 = time.perf_counter()

    def lap(name, since):
        T[name] = round((time.perf_counter() - since) * 1000)
        return time.perf_counter()
    live = Live(c, target, None)
    knobs = live.knobs()
    tick = lap('readControlsMs', t0)
    allowed = None
    if controls:
        by_label = {k['label']: p for p, k in knobs.items()}
        by_label.update({k['name']: p for p, k in knobs.items()})
        by_label.update({p: p for p in knobs})
        allowed = {by_label[x] for x in controls if x in by_label} or None

    tick = time.perf_counter()
    _status(phase='listening to the synth now')
    before, before_audio = live.listen()
    tick = lap('liveCaptureBeforeMs', tick)
    ref_audio = reference_target._read_cue(target)
    measured = {'now': _dsp(*before_audio), 'reference': _dsp(*ref_audio)}
    tick = lap('referenceReadAndDspMs', tick)
    live_f = np.asarray(before['descriptors']['live'], dtype=float)
    target_f = np.asarray(before['descriptors']['reference'], dtype=float)

    if library is None:
        import uhe_presets
        library, _ = uhe_presets.library()
    tick = lap('presetLibraryParseMs', tick)
    _status(phase='rendering presets offline')
    donors = preset_space(state, library, codec)
    tick = lap('presetSpaceMs', tick)
    own = Path(work or tempfile.mkdtemp(prefix='scrunch-search-'))
    try:
        rendered = _render_all([(state, {})] + [(blob, {}) for _, blob in donors], own / 'presets', stop)
        T['presetRenders'] = len(donors) + 1
        tick = lap('presetRendersMs', tick)
        now_d, now_params = rendered[0]
        if now_d is None:
            raise NotReady('This patch renders silent offline on the map note, so there is nothing to map.')
        ids = controls_to_move(knobs, now_params, allowed)
        if not ids:
            raise NotReady('No timbre control of this patch can be moved by the search.')
        eligible = set(ids)
        cur, cur_text = _vector(now_params, ids)
        shift = live_f - np.asarray(now_d)                  # held note offline -> his passage live
        ranked = []
        for (meta, _), (d, params) in zip(donors, rendered[1:]):
            if d is None:
                continue
            v, _ = _vector(params, ids)
            if np.max(np.abs(v - cur)) < 1e-4:
                continue
            ranked.append((_rms(np.asarray(d) + shift - target_f), meta, v))
        if not ranked:
            raise NotReady('None of the rendered presets differs from this patch in the controls allowed.')
        ranked.sort(key=lambda r: r[0])
        first = ranked[0]
        d1 = first[2] - cur
        second = next((r for r in ranked[1:] if abs(float(np.dot(r[2] - cur, d1)) /
                                                    (np.linalg.norm(r[2] - cur) * np.linalg.norm(d1) + 1e-12)) < .9), None)
        v2 = second[2] if second else cur
        moving = [i for i, p in enumerate(ids) if abs(first[2][i] - cur[i]) > 1e-4 or abs(v2[i] - cur[i]) > 1e-4]
        ids = [ids[i] for i in moving]
        cur, v1, v2 = cur[moving], first[2][moving], v2[moving]
        cur_text = [cur_text[i] for i in moving]
        axis = np.linspace(0., 1., GRID)
        xs = [(float(a), float(b) if second else 0.) for a in axis for b in (axis if second else [0.])]
        vst = [str(p)[2:] for p in ids]
        tick = time.perf_counter()
        grid = _render_all([(state, dict(zip(vst, point(cur, v1, v2, x).tolist()))) for x in xs], own / 'map', stop)
        T['mapRenders'] = len(xs)
        tick = lap('mapRendersMs', tick)
        rows = [{'x': list(x), 'features': list(d)} for x, (d, _) in zip(xs, grid) if d is not None]
        texts = {tuple(x): dict(zip(vst, _vector(p, ids)[1])) for x, (d, p) in zip(xs, grid) if d is not None}
    finally:
        if work is None:
            shutil.rmtree(own, ignore_errors=True)
    tick = time.perf_counter()
    rows, map_shift = warm_gp_engine.corrected(rows, [0., 0.], live_f)
    best = min(rows, key=lambda r: _rms(np.asarray(r['features']) - target_f))
    g = [float(best['x'][0]), float(best['x'][1])]
    plan_id = uuid.uuid4().hex
    gp = None
    first_x, predicted = g, np.asarray(best['features'])
    if g == [0., 0.]:
        # The map puts nothing on its grid ahead of where he is: the first write is the warm GP's own first
        # proposal from here (the same policy object executes it, so the plan is exactly what gets written).
        gp = _policy(plan_id, rows, target_f, {'x': [0., 0.], 'features': live_f.tolist()})
        first_x = [float(v) for v in gp.ask()]
    to = point(cur, v1, v2, first_x)
    # Stages: one module each, capped. Ordered by the offline-predicted error of each module's capped move alone.
    _status(phase='measuring each module offline')
    raw = cap_stages(ids, cur, to, knobs, total_cap=float('inf'))
    extra = Path(tempfile.mkdtemp(prefix='scrunch-search-'))
    try:
        jobs = []
        for st in raw:
            v = cur.copy()
            v[st['idx']] += st['delta']
            jobs.append((state, dict(zip(vst, v.tolist()))))
        alone = _render_all(jobs, extra, stop) if jobs else []
        T['stageRenders'] = len(jobs)
    finally:
        shutil.rmtree(extra, ignore_errors=True)
    now_rms = _rms(live_f - target_f)
    scored = []
    for st, (d, p_after) in zip(raw, alone):
        pred = np.asarray(d) + np.asarray(map_shift) if d is not None else None
        scored.append((_rms(pred - target_f) if pred is not None else np.inf, st['group'], pred))
    order = stage_order(scored, now_rms)
    stages = cap_stages(ids, cur, to, knobs, order)            # the caps again in this order (MAX_TOTAL trims last)
    pred_by = {g: (e, pr) for e, g, pr in scored}
    texts_by = {st['group']: dict(zip(vst, _vector(p_after, ids)[1])) for st, (d, p_after) in zip(raw, alone)}
    if stages:
        predicted = pred_by[stages[0]['group']][1] if pred_by[stages[0]['group']][1] is not None else predicted
    tick = lap('gpAndPredictionMs', tick)
    T['planTotalMs'] = round((time.perf_counter() - t0) * 1000)
    bands = [b['band'] for b in before['allBands']]
    moves = []
    for n, st in enumerate(stages, 1):
        for i, dlt in zip(st['idx'], st['delta']):
            p = ids[i]
            moves.append({'id': p, 'control': knobs[p]['label'], 'stage': n, 'group': st['group'],
                          'from': round(float(cur[i]), 4), 'fromDisplay': cur_text[i],
                          'to': round(float(cur[i] + dlt), 4), 'toDisplay': texts_by.get(st['group'], {}).get(str(p)[2:])})
    stage_rows = [{'stage': n, 'group': st['group'], 'controls': len(st['idx']),
                   'totalMove': round(float(np.sum(np.abs(st['delta']))), 4),
                   'maxMove': round(float(np.max(np.abs(st['delta']))), 4),
                   'predictedRmsDb': round(float(pred_by[st['group']][0]), 3) if np.isfinite(pred_by[st['group']][0]) else None}
                  for n, st in enumerate(stages, 1)]
    plan = {
        'planId': plan_id, 'at': time.time(), 'prediction': True, 'kind': 'plan',
        'note': 'Predicted from offline renders of this patch (held note, shifted to the live measurement). '
                'Nothing has been played or written yet; how it sounds is only known after it is measured.',
        'synth': plugin, 'track': getattr(c, 'track', None),
        'reference': {'id': target.get('id'), 'name': target.get('name'), 'path': target.get('path'),
                      'cueSeconds': target.get('cueSeconds'), 'durationSeconds': target.get('durationSeconds'),
                      'part': target.get('part')},
        'measuredNow': {'dsp': measured['now'], 'referenceDsp': measured['reference'],
                        'bandDifferenceDb': {b['band']: round(b['differenceDb'], 2) for b in before['allBands']},
                        'descriptorRmsDb': round(_rms(live_f - target_f), 3), 'distanceDb': before['distanceDb']},
        'predictedAfterFirstWrite': {'a': first_x[0], 'b': first_x[1], 'stage': stages[0]['group'] if stages else None,
                                     'descriptorRmsDb': round(_rms(predicted - target_f), 3),
                                     'bandErrorDb': dict(zip(bands, [round(float(v), 2) for v in predicted - target_f]))},
        'searchWindow': 'one synth module per try, measured after each and kept only if closer; each control moves '
                        'at most %.2f per try, a try at most %.2f summed, the whole search at most %.2f summed; '
                        'pitch/voice-style controls held' % (MAX_STEP, MAX_STAGE_TOTAL, MAX_TOTAL),
        'constraints': {'maxStepPerControl': MAX_STEP, 'maxTotalPerStage': MAX_STAGE_TOTAL, 'maxTotal': MAX_TOTAL,
                        'maxStages': MAX_STAGES, 'protectedHeld': sorted(knobs[p]['label'] for p in knobs if protected(knobs[p])
                                                                         and (allowed is None or p not in allowed))[:60]},
        'stages': stage_rows,
        'stageOrder': {'diagnostic': True,
                       'basis': 'DIAGNOSTIC: modules ordered by measured expected gain (offline-predicted error of each '
                                'module\'s capped move alone on the patch map, shifted to the live measurement); not the '
                                'permanent decision-maker',
                       'predictedErrorDbByModule': {g: (round(float(e), 3) if np.isfinite(e) else None) for e, g, _ in scored},
                       'measuredNowDb': round(float(now_rms), 3)},
        'space': {'presetsRendered': len(donors), 'direction1': first[1], 'direction2': second[1] if second else None,
                  'predictedRmsDb': {'direction1': round(first[0], 3), 'direction2': round(second[0], 3) if second else None},
                  'controlsInSpace': len(ids), 'warmStart': {'a': g[0], 'b': g[1]},
                  'map': {'rows': len(rows), 'grid': '%d x %d' % (GRID, GRID if second else 1),
                          'shiftDb': [round(float(v), 2) for v in map_shift]}},
        'moves': moves, 'timings': T,
        # Log only (no behaviour): what the warm GP was given and what it proposed. It runs here only when the map's
        # best grid point is where he already is; its sole observation is then the live 'before' measurement.
        'gp': {'ran': gp is not None,
               'observations': [{'x': [round(v, 6) for v in r['x']], 'features': [round(v, 4) for v in r['features']],
                                 'measuredLive': True} for r in ([gp.before] + gp.rows)] if gp is not None else [],
               'proposals': [first_x] if gp is not None else [],
               'firstWriteFrom': 'warm GP proposal' if gp is not None else 'best point of the offline map (no GP call)',
               'mapRows': [{'x': [round(v, 6) for v in r['x']], 'features': [round(v, 4) for v in r['features']]}
                           for r in rows]}}
    if suggested:
        # The engineer's candidate controls: validated against this synth's real controls; the search set is
        # never replaced by them, only reported against.
        real = [p for p in dict.fromkeys(suggested) if p in knobs]
        plan['suggestedByEngineer'] = [
            {'id': p, 'control': knobs[p]['label'],
             'inSearch': p in ids,
             'why': 'moved by the search' if p in ids else
                    'a timbre control, but the preset directions do not move it' if p in eligible else
                    'not a timbre control the search can move (stepped, text-only or voice/output)'}
            for p in real]
    ctx = {'ids': ids, 'cur': cur, 'v1': v1, 'v2': v2, 'g': g, 'stages': stages, 'rows': rows, 'target': target_f, 'before': before,
           'before_audio': before_audio, 'ref_audio': ref_audio, 'expected': {p: float(knobs[p]['value']) for p in ids},
           'original': {p: float(knobs[p]['value']) for p in ids}}
    return plan, ctx


def _policy(plan_id, rows, target_f, guess, before_features=None):
    import warm_gp
    ident = 'scrunch-live:brooklyn-rose:' + plan_id
    return warm_gp.SessionPolicy(identity=ident, stored_identity=ident, metric=warm_gp.METRIC,
                                 stored_metric=warm_gp.METRIC, stored=rows, target={'features': list(target_f)},
                                 guess=guess, before={'x': [0., 0.], 'features': list(before_features or guess['features'])},
                                 dimensions=2, seed=0)


# ---------------------------------------------------------------- execute
def execute(c, target, plan, ctx, stop=None, tries=None, restore=False):
    """One module per try (the plan's stages, capped), measured after each; a stage is kept only if it measured
    closer to the reference than the best so far, else that module is written straight back. Then the outcome is
    measured (DSP). Returns the result row."""
    import reference_loop
    import sound_chat
    live = Live(c, target, None)
    ids = ctx['ids']
    expected = dict(ctx['expected'])
    target_f = ctx['target']
    budget = min(MAX_STAGES, tries if tries is not None else reference_loop.search_tries(float(target['durationSeconds'])))
    best_rms = _rms(np.asarray(ctx['before']['descriptors']['live']) - target_f)
    kept_labels = []
    events, stopped, writing = [], None, [False]
    T = {'writeReadbackMs': [], 'liveCaptureMs': [], 'revertWriteMs': []}
    t_exec = time.perf_counter()
    sound_chat.forget_ahead()

    stages = (ctx.get('stages') or [])[:max(0, budget)]
    _status(phase='searching', stagesTotal=len(stages), stage=0, triesDone=0)
    cancelled, failed = False, None

    def write(values, key):
        nonlocal expected
        writing[0] = True
        tick = time.perf_counter()
        expected = dict(expected, **live.write(values, {p: expected[p] for p in values}))
        T[key].append(round((time.perf_counter() - tick) * 1000))
        writing[0] = False

    try:
        for n, st in enumerate(stages, 1):
            if _cancel_requested():
                raise Cancelled()
            if stop and stop():
                writing[0] = True
                raise InterruptedError('Scrunch was switched off or the request was cancelled')
            _status(stage=n, group=st['group'], phase='stage %d of %d: %s' % (n, len(stages), st['group']))
            was = {ids[i]: expected[ids[i]] for i in st['idx']}
            write({ids[i]: float(np.clip(was[ids[i]] + d, 0., 1.)) for i, d in zip(st['idx'], st['delta'])},
                  'writeReadbackMs')
            tick = time.perf_counter()
            try:
                m, _audio = live.listen()
                err = _rms(np.asarray(m['descriptors']['live']) - target_f)
                if not np.isfinite(err):
                    raise ValueError('the descriptor error is not finite')
            except Exception as exc:
                raise ScoringFailed('DSP scoring failed on stage %d (%s): %s' % (n, st['group'], str(exc)[:160]))
            T['liveCaptureMs'].append(round((time.perf_counter() - tick) * 1000))
            _status(triesDone=n)
            keep = err < best_rms - 1e-9
            label = 'stage %d: %s' % (n, st['group'])
            events.append({'label': label, 'stage': n, 'group': st['group'], 'controls': len(st['idx']),
                           'totalMove': round(float(np.sum(np.abs(st['delta']))), 4), 'measuredRmsDb': round(err, 3),
                           'kept': keep})
            if _cancel_requested():         # mid-stage: this stage's write is verified; everything goes back below
                events[-1]['kept'] = False
                raise Cancelled()
            if keep:
                best_rms = err
                kept_labels.append(label)
            else:
                write(was, 'revertWriteMs')         # this module back exactly where it was (readback-checked)
    except Cancelled:
        cancelled, stopped = True, 'cancelled by John'
    except ScoringFailed as exc:
        failed = str(exc)[:300]
        stopped = 'scoring failed: ' + failed
    except Exception as exc:
        stopped = str(exc)[:200]
    outcome, restored = None, None
    if not cancelled and not failed and not writing[0]:   # never listen on after an unverified write or switch-off
        try:
            tick = time.perf_counter()
            after, after_audio = live.listen()
            T['outcomeCaptureMs'] = round((time.perf_counter() - tick) * 1000)
            tick = time.perf_counter()
            outcome = _outcome(ctx, after, after_audio, target_f)
            T['outcomeDspMs'] = round((time.perf_counter() - tick) * 1000)
        except Exception as exc:
            failed = 'DSP scoring failed at the outcome: %s' % str(exc)[:160]
        if failed:
            stopped = 'scoring failed: ' + failed
    if (cancelled or failed) and not writing[0]:
        # Cancel or a required score failed: every control this search moved goes back to where he had it,
        # compare-and-set + readback. Nothing is kept.
        _status(phase='restoring the original')
        try:
            moved = {p: ctx['original'][p] for p in ids if abs(expected[p] - ctx['original'][p]) > 1e-9}
            if moved:
                write(moved, 'revertWriteMs')
            exact = all(abs(expected[p] - ctx['original'][p]) <= 1e-4 for p in ids)
            restored = {'status': 'put back' if exact else 'not put back', 'controls': len(moved), 'exact': exact}
        except Exception as exc:
            restored = {'status': 'not put back', 'why': str(exc)[:200], 'exact': False}
    elif failed:
        restored = {'status': 'not put back', 'why': 'a write was not verified, so nothing is written blind',
                    'exact': False}
    labels = {p: k['label'] for p, k in live.knobs().items()}
    kept = [{'id': p, 'control': labels.get(p, p), 'from': round(ctx['original'][p], 4),
             'to': round(expected[p], 4)} for p in ids if abs(expected[p] - ctx['original'][p]) > 1e-4]
    c.search_undo = None if (cancelled or failed) and restored and restored.get('exact') else \
        {'original': dict(ctx['original']), 'expected': dict(expected), 'planId': plan['planId']}
    c.move, c.last_verified = None, None          # earlier change records no longer describe what is live
    result = {'planId': plan['planId'], 'at': time.time(), 'kind': 'result', 'prediction': False,
              'tries': events, 'stopped': stopped, 'cancelled': cancelled, 'kept': ', '.join(kept_labels) if kept_labels else 'original',
              'keptSetting': {'stages': kept_labels},
              'keptMoves': kept, 'outcome': outcome,
              'predictedVsMeasured': {'predictedRmsDb': plan['predictedAfterFirstWrite']['descriptorRmsDb'],
                                      'measuredBeforeRmsDb': plan['measuredNow']['descriptorRmsDb'],
                                      'measuredAfterRmsDb': outcome['descriptorRmsDb'] if outcome else None},
              'timings': dict(T, executeTotalMs=round((time.perf_counter() - t_exec) * 1000))}
    if failed:
        result['scoringFailed'] = failed
    if cancelled or failed:
        result['restored'] = restored
        result['kept'] = 'original' if restored and restored.get('exact') else result['kept']
    elif restore:
        result['restored'] = put_back(c)
    return result


def _outcome(ctx, after, after_audio, target_f):
    dsp_after = _dsp(*after_audio)
    return {'dsp': dsp_after, 'dspBefore': _dsp(*ctx['before_audio']),
            'bandDifferenceDb': {b['band']: round(b['differenceDb'], 2) for b in after['allBands']},
            'descriptorRmsDb': round(_rms(np.asarray(after['descriptors']['live']) - target_f), 3),
            'distanceDb': after['distanceDb'], 'distanceDbBefore': ctx['before']['distanceDb']}


def put_back(c):
    """Every control the search moved back to where it was before it started, only if he has not moved them since."""
    undo = getattr(c, 'search_undo', None)
    if not undo:
        return {'status': 'nothing to put back'}
    live = Live(c, {'durationSeconds': 1}, None)
    try:
        back = live.write(undo['original'], undo['expected'])
    except Exception as exc:
        return {'status': 'not put back', 'why': str(exc)[:200]}
    c.search_undo = None
    return {'status': 'put back', 'controls': len(back)}


# ---------------------------------------------------------------- the whole request
def run(c, target, controls=None, rid=None, generation=None, plan_only=False, restore=False, tries=None, suggested=None,
        *, driver=None, stop=None):
    """Production entry: real coordinates and the same phrase evaluator as offline acceptance.

    The legacy donor/stage functions remain only to reproduce historical evidence.
    They are never a fallback for missing adaptive Host capabilities.
    """
    import adaptive_match
    with _RUN_LOCK:
        if RUN.get('running'):
            raise NotReady('A search is already running.')
        RUN.clear()
        RUN.update(runId=uuid.uuid4().hex, running=True, phase='adaptive search', startedAt=time.time(),
                   cancelRequested=False, requestId=rid)
    done = None
    try:
        done = adaptive_match.run(c,target,rid,generation,driver=driver,stop=stop,tries=tries,
                                  restore=restore,plan_only=plan_only)
        if driver is None:
            _append(done['plan'])
            if done['result']: _append(done['result'])
        return done
    finally:
        _status(running=False, phase=(done or {}).get('result',{}).get('status','stopped')
                if (done or {}).get('result') else 'stopped', endedAt=time.time())


def legacy_run(c, target, controls=None, rid=None, generation=None, plan_only=False, restore=False, tries=None, suggested=None):
    """Plan (saved before any write), then execute. Returns {'plan', 'result', 'say'}. While it works, RUN holds a
    visible status (phase, stage i of n, tries done, elapsed, heartbeat every HEARTBEAT_S) and cancel() stops it at
    the next safe point with the original put back (verified)."""
    with _RUN_LOCK:
        if RUN.get('running'):
            raise NotReady('A search is already running.')
        RUN.clear()
        now = time.time()
        RUN.update(runId=uuid.uuid4().hex, running=True, phase='planning', startedAt=now, heartbeatAt=now,
                   stage=0, stagesTotal=None, triesDone=0, cancelRequested=False, requestId=rid)
    beat = threading.Event()
    threading.Thread(target=_heartbeat, args=(beat,), daemon=True, name='search-heartbeat').start()
    outcome = 'failed'
    try:
        done = _run(c, target, controls, rid, generation, plan_only, restore, tries, suggested)
        res = done.get('result') or {}
        outcome = ('cancelled' if res.get('cancelled') else
                   'stopped: scoring failed (%s)' % res['scoringFailed'] if res.get('scoringFailed') else 'done')
        return done
    finally:
        beat.set()
        _status(running=False, phase=outcome, endedAt=time.time(), heartbeatAt=time.time())


def _run(c, target, controls, rid, generation, plan_only, restore, tries, suggested):
    import action_layer

    def stop():
        return _cancel_requested() or (rid is not None and not action_layer.cancel_clear(rid, generation)[0])
    import audition_pair
    before = audition_pair.snapshot(c)
    try:
        plan, ctx = make_plan(c, target, controls, stop, suggested=suggested)
    except RuntimeError:
        if _cancel_requested():         # cancelled while planning: nothing was written
            return {'plan': None, 'result': {'cancelled': True, 'restored': {'status': 'nothing written', 'exact': True}},
                    'say': 'Search cancelled while planning; nothing was written.'}
        raise
    _append(plan)
    if plan_only:
        return {'plan': plan, 'result': None, 'say': english_plan(plan)}
    result = execute(c, target, plan, ctx, stop, tries=tries)
    if not result.get('cancelled') and not result.get('scoringFailed'):
        audition_pair.commit(c, before, 'search', plan['planId'])
    if restore and not result.get('cancelled') and not result.get('scoringFailed'):
        tick = time.perf_counter()
        result['restored'] = put_back(c)
        pair = audition_pair.get(c)
        if pair and result['restored'].get('status') == 'put back':
            pair['playing'] = 'original'
            audition_pair._save()
        result['timings']['restoreMs'] = round((time.perf_counter() - tick) * 1000)
    _append(result)
    return {'plan': plan, 'result': result, 'say': english_plan(plan) + '\n' + english_result(plan, result)}


def _f(v, unit=''):
    return 'n/a' if v is None else ('%.1f%s' % (v, unit))


def english_plan(plan):
    m = plan['measuredNow']
    now, ref = m['dsp'], m['referenceDsp']
    s = plan['space']
    lines = ['PLAN (predicted, not heard yet): search toward %s, %s.' % (plan['reference']['name'], plan['reference']['part']),
             'Measured now vs reference: loudness %s vs %s LUFS (compared level-matched); brightness centre %s vs %s Hz; '
             'energy above 2 kHz %s vs %s.' % (_f(now['loudnessLUFS']), _f(ref['loudnessLUFS']), now['brightnessHz'],
                                               ref['brightnessHz'], now['highShare'], ref['highShare']),
             'Bands, synth minus reference (dB): ' + ', '.join('%s %+.1f' % (b, v) for b, v in m['bandDifferenceDb'].items()) + '.',
             'Predicted: descriptor error %.2f dB now -> %.2f dB after the first write (offline map of this patch, %s, %d rows).'
             % (m['descriptorRmsDb'], plan['predictedAfterFirstWrite']['descriptorRmsDb'], s['map']['grid'], s['map']['rows']),
             'Directions from presets (%d rendered): %s%s; map warm start a=%.2f b=%.2f. Constrained: %s. '
             '%d stage%s, %d control moves:'
             % (s['presetsRendered'], s['direction1']['name'], (' and ' + s['direction2']['name']) if s['direction2'] else '',
                s['warmStart']['a'], s['warmStart']['b'], plan['searchWindow'], len(plan.get('stages') or []),
                '' if len(plan.get('stages') or []) == 1 else 's', len(plan['moves']))]
    for st in plan.get('stages') or []:
        lines.append(' Stage %d, %s (%d controls, total move %.2f, largest %.2f):' % (
            st['stage'], st['group'], st['controls'], st['totalMove'], st['maxMove']))
        lines += ['  %s: %s -> %s' % (mv['control'], mv['fromDisplay'] if mv['fromDisplay'] is not None else mv['from'],
                                       mv['toDisplay'] if mv['toDisplay'] is not None else mv['to'])
                  for mv in plan['moves'] if mv.get('stage') == st['stage']]
    if plan.get('suggestedByEngineer'):
        lines.append('Engineer\'s suggested controls: ' + '; '.join(
            '%s (%s)' % (x['control'], x['why']) for x in plan['suggestedByEngineer'][:12]) + '.')
    return '\n'.join(lines)


def english_result(plan, result):
    o = result.get('outcome')
    if result.get('cancelled'):
        r = result.get('restored') or {}
        return 'CANCELLED by you after %d tr%s: %s.' % (len(result['tries']), 'y' if len(result['tries']) == 1 else 'ies',
                                                         'the original is back exactly (read back)' if r.get('exact')
                                                         else 'NOT fully put back: %s' % (r.get('why') or r.get('status')))
    if result.get('scoringFailed'):
        r = result.get('restored') or {}
        return 'STOPPED, nothing kept: %s. %s.' % (result['scoringFailed'],
                                                  'The original is back exactly (read back)' if r.get('exact')
                                                  else 'NOT put back: %s' % (r.get('why') or r.get('status')))
    head = 'MEASURED after %d tr%s, one module each (kept: %s).' % (len(result['tries']), 'y' if len(result['tries']) == 1 else 'ies', result['kept'])
    if result.get('stopped'):
        head += ' Stopped: %s.' % result['stopped']
    if not o:
        return head + ' The outcome could not be measured.'
    b, a = o['dspBefore'], o['dsp']
    return '\n'.join([head,
                      'Descriptor error: %.2f dB before, predicted %.2f, measured %.2f.' % (
                          result['predictedVsMeasured']['measuredBeforeRmsDb'], result['predictedVsMeasured']['predictedRmsDb'],
                          o['descriptorRmsDb']),
                      'DSP: loudness %s -> %s LUFS, brightness centre %s -> %s Hz, band distance to reference %.2f -> %.2f dB.' % (
                          _f(b['loudnessLUFS']), _f(a['loudnessLUFS']), b['brightnessHz'], a['brightnessHz'],
                          o['distanceDbBefore'], o['distanceDb']),
                      'Writes: %d pass%s through Scrunch Host, every one read back exactly as written; %d control%s now '
                      'differ from the start (Revert / Candidate toggle the stored before and after).'
                      % (len(result['timings'].get('writeReadbackMs') or []) + len(result['timings'].get('revertWriteMs') or []),
                         '' if len(result['timings'].get('writeReadbackMs') or []) + len(result['timings'].get('revertWriteMs') or []) == 1 else 'es',
                         len(result['keptMoves']), '' if len(result['keptMoves']) == 1 else 's')] + (['Restored: %s.' % result['restored']['status']] if result.get('restored') else []))
