"""Blind benchmark harness for MIDI foundation candidates (master spec §41, §45, §47, §88).

One interface for every candidate: a structured request goes in, MIDI notes come out.
- Symbolic-native models (MIDI-LLM, Text2MIDI, Amadeus, ...) run through `CommandCandidate`: a
  runner process gets the request as JSON on stdin and returns MIDI. The same seam covers a local
  checkpoint or a remote GPU, so no provider is built in. A candidate with no installed runner
  reports itself unavailable with the reason; it is never replaced by something else.
- `DeterministicBaseline` and `ProviderBaseline` (the existing general-LLM path in providers.py)
  are baselines only. The general-LLM baseline makes a paid call, so it only runs when explicitly
  allowed.

Every take is logged in the take log. Automatic measures are numbers, not musical judgement
(spec §92): validity, distinctness between takes, distance from a given source, share of notes
outside the requested register, latency, compute, and a copy check against the reference
library and any other corpus. John's blind A/B/Neither sheet is written with anonymous ids;
the key and the measures, which could reveal identities, are kept in a separate folder.
"""
import base64, datetime, json, math, os, random, resource, secrets, shutil, subprocess, time
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import midi_analysis, midi_file, music
from midi_take_log import TakeLog

KINDS = ('symbolic', 'deterministic-baseline', 'general-llm-baseline')
NGRAM = 6

# Spec §45 task set. Intents are handed to candidates verbatim; the structured fields are the
# controls a request resolves to (spec §48), set explicitly here so every candidate gets the same.
TASKS = [
    {'id': 'bass', 'intent': 'progressive trance bassline', 'bars': 4, 'tempo': 137, 'key': [9, 'minor'], 'register': [28, 52]},
    {'id': 'lead', 'intent': 'progressive trance lead', 'bars': 8, 'tempo': 137, 'key': [9, 'minor'], 'register': [60, 88]},
    {'id': 'arp', 'intent': 'arp', 'bars': 4, 'tempo': 137, 'key': [9, 'minor'], 'register': [55, 84]},
    {'id': 'hook', 'intent': 'melodic hook', 'bars': 4, 'tempo': 137, 'key': [9, 'minor'], 'register': [60, 86]},
    {'id': 'chords', 'intent': 'chord progression', 'bars': 8, 'tempo': 137, 'key': [9, 'minor'], 'register': [45, 79], 'polyphonic': True},
    {'id': 'syncopated', 'intent': 'rhythmically syncopated sequence', 'bars': 4, 'tempo': 137, 'key': [9, 'minor'], 'register': [48, 79]},
    {'id': 'variation', 'intent': 'variation of accepted phrase', 'needs': 'accepted-take', 'tempo': 137, 'key': [9, 'minor']},
    {'id': 'classical', 'intent': 'classical/emotional transformation', 'needs': 'reference-part', 'tempo': 137, 'key': [9, 'minor']},
    {'id': 'portman', 'intent': 'Rachel-Portman-like emotional contour → original progressive trance', 'needs': 'reference-part', 'tempo': 137, 'key': [9, 'minor']},
    {'id': 'peak', 'intent': '136–138 BPM peak-time trance', 'bars': 8, 'tempo': 138, 'key': [9, 'minor'], 'register': [48, 88]},
    {'id': 'tribal', 'intent': 'tribal/progressive patterns', 'bars': 4, 'tempo': 128, 'key': [9, 'minor'], 'register': [36, 76]},
    {'id': 'many', 'intent': 'multiple clearly different takes from same intent', 'bars': 4, 'tempo': 137, 'key': [9, 'minor'], 'register': [48, 84], 'takes': 4},
]


def default_root():
    return Path(os.environ.get('SCRUNCH_DATA', str(Path(__file__).resolve().parent / 'data'))) / 'midi-bench'


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- candidates ------------------------------------------------------------------------------

class Candidate:
    id = 'candidate'
    kind = 'symbolic'
    accepts_source = False  # can it be given a MIDI phrase to vary or continue?
    source_only = False     # does it need one (no text conditioning)?

    def status(self):
        return {'available': True, 'reason': ''}

    def generate(self, request):
        """request: intent, bars, beatsPerBar, tempo, key, register, polyphonic, source, seed. Returns {'notes','meta'}."""
        raise NotImplementedError


def playable(notes):
    """What a Live clip can hold: one note per pitch per onset, and a note of a pitch ends where the next
    note of that pitch begins (Live shortens it otherwise). Merged parts often break both, so notes
    striking the same pitch together are combined (longest, then loudest, kept) and overlaps trimmed.
    Returns (notes, number of notes combined or trimmed)."""
    best = {}
    for n in notes:
        k = (n['p'], round(n['t'], 6))
        if k not in best or (n['d'], n['v']) > (best[k]['d'], best[k]['v']):
            best[k] = n
    changed = len(notes) - len(best)
    by_pitch = {}
    for n in sorted(best.values(), key=lambda n: n['t']):
        by_pitch.setdefault(n['p'], []).append(dict(n))
    out = []
    for seq in by_pitch.values():
        for a, b in zip(seq, seq[1:]):
            gap = round(b['t'] - a['t'], 6)
            if a['d'] > gap + 1e-6:
                a['d'] = gap; changed += 1
        out.extend(seq)
    return sorted(out, key=lambda n: (n['t'], n['p'])), changed


def fit(parsed, length, scale=1.0):
    """Pitched parts merged into one phrase, rescaled, and cut at `length` beats.

    Full-song generators return more than the task asked for; what was cut is counted in the
    returned meta rather than hidden. Drum-channel notes are excluded and counted.
    """
    notes = []
    beyond = drums = raw = 0
    for part in parsed['parts']:
        if part['drums']:
            drums += len(part['notes']); continue
        for n in part['notes']:
            raw += 1
            t, d = round(n['t'] * scale, 6), round(n['d'] * scale, 6)
            if t >= length - 1e-6:
                beyond += 1; continue
            d = round(min(d, length - t), 6)
            if d > 0:
                notes.append(dict(p=n['p'], t=t, d=d, v=n['v']))
    notes, collapsed = playable(notes)
    ends = [n['t'] + n['d'] for p in parsed['parts'] for n in p['notes']]
    meta = {'partsMerged': sum(not p['drums'] for p in parsed['parts']), 'rawPitchedNotes': raw,
            'samePitchNotesFixed': collapsed,
            'rawLengthBeats': round(max(ends) * scale, 3) if ends else 0,
            'notesBeyondLength': beyond, 'drumNotesExcluded': drums,
            'programs': sorted({p['program'] for p in parsed['parts'] if p.get('program') is not None})}
    return sorted(notes, key=lambda n: (n['t'], n['p'])), meta


class CommandCandidate(Candidate):
    """A model behind a runner process. stdin: the request JSON. stdout: JSON with one of
    `notes` ([{p,t,d,v}] in beats), `midi_base64`, or `midi_path`; optional `meta`.

    `requires` lists paths (checkpoint folders, runner venvs) that must exist before it counts as
    available, so an undownloaded model says so instead of failing mid-run.
    """

    def __init__(self, id, argv=None, kind='symbolic', requires=(), cwd=None, timeout=900, env=None,
                 settings=None, accepts_source=False, source_only=False):
        if kind not in KINDS:
            raise ValueError('Unknown candidate kind %r.' % kind)
        self.id, self.argv, self.kind = id, list(argv or []), kind
        self.requires, self.cwd, self.timeout, self.env = [str(p) for p in requires], cwd, timeout, env
        self.settings, self.accepts_source = dict(settings or {}), bool(accepts_source or source_only)
        self.source_only = bool(source_only)

    def status(self):
        if not self.argv:
            return {'available': False, 'reason': 'no runner configured (checkpoint not installed)'}
        exe = self.argv[0]
        if not (shutil.which(exe) or Path(exe).exists()):
            return {'available': False, 'reason': 'runner %s not found' % exe}
        missing = [p for p in self.requires if not Path(p).exists()]
        if missing:
            return {'available': False, 'reason': 'missing ' + ', '.join(missing)}
        return {'available': True, 'reason': ''}

    def generate(self, request):
        env = dict(os.environ, **(self.env or {}))
        done = subprocess.run(self.argv, input=json.dumps({**request, **self.settings}), capture_output=True, text=True,
                              timeout=self.timeout, cwd=self.cwd, env=env)
        if done.returncode:
            raise RuntimeError('%s exited %d: %s' % (self.id, done.returncode, done.stderr.strip()[-400:]))
        try:
            out = json.loads(done.stdout)
        except json.JSONDecodeError:
            raise RuntimeError('%s did not return JSON.' % self.id) from None
        meta = dict(out.get('meta') or {})
        if 'notes' in out:
            return {'notes': out['notes'], 'meta': meta}
        data = base64.b64decode(out['midi_base64']) if 'midi_base64' in out else Path(out['midi_path']).read_bytes()
        parsed = midi_file.read(data)
        # Seconds-based output (e.g. AMT) is placed on the requested tempo's beat grid.
        scale = request['tempo'] / parsed['tempo'] if out.get('timebase') == 'seconds' else 1.0
        notes, fitted = fit(parsed, request['bars'] * request['beatsPerBar'], scale)
        meta.update(fitted, timebase=out.get('timebase', 'beats'), fileTempo=parsed['tempo'])
        return {'notes': notes, 'meta': meta, 'raw': data}


# The installed symbolic model, chosen by John on 26 Sep 2026 from blind listening: MIDI-LLM writes new
# ideas from a brief (evidence/midi-model-decision-2026-09-26.md). Text2midi and Amadeus were dropped;
# AMT was retired on 27 Sep 2026 after failing the blind variation test
# (evidence/midi-variation-shootout-2026-09-27.md). Their configs are in midi_runners/candidates-retired.json.
SYMBOLIC = ('midi-llm',)


def configured(path=None):
    """Candidates from data/midi-bench/candidates.json, plus unconfigured symbolic placeholders.

    File format: [{"id", "kind", "argv", "requires", "cwd", "timeout", "settings", "env", "acceptsSource", "sourceOnly"}].
    Relative paths in argv/requires are resolved against the Scrunch folder.
    """
    here = Path(__file__).resolve().parent
    if path is None:  # a local override in data/, else the committed runner config
        path = default_root() / 'candidates.json'
        if not path.exists():
            path = here / 'midi_runners' / 'candidates.json'
    path = Path(path)
    rows = json.loads(path.read_text()) if path.exists() else []
    absolute = lambda x: str(here / x) if x and not Path(x).is_absolute() and ('/' in x) else x
    out = [CommandCandidate(r['id'], [absolute(a) for a in r.get('argv') or []], r.get('kind', 'symbolic'),
                            [absolute(x) for x in r.get('requires', ())], r.get('cwd'), r.get('timeout', 900),
                            env=r.get('env'), settings=r.get('settings'), accepts_source=r.get('acceptsSource', False),
                            source_only=r.get('sourceOnly', False)) for r in rows]
    have = {c.id for c in out}
    return out + [CommandCandidate(i) for i in SYMBOLIC if i not in have]


SCALES = {'major': (0, 2, 4, 5, 7, 9, 11), 'minor': (0, 2, 3, 5, 7, 8, 10)}


def euclid(pulses, steps, rotate=0):
    """Bjorklund-style even distribution of pulses over steps."""
    pattern = [((i * pulses) % steps) < pulses for i in range(steps)]
    return pattern[-rotate % steps:] + pattern[:-rotate % steps] if steps else []


def caption(task, beats_per_bar=4):
    """The text every text-conditioned candidate receives: the intent plus the structured controls,
    written the same way for every task and every model. No word in the intent changes anything."""
    tonic, mode = task['key']
    return '%s. Set in %s %s with a %d/4 time signature, at %s BPM, %d bars long.' % (
        task['intent'], midi_analysis.PC[tonic], mode, int(beats_per_bar), task['tempo'], task['bars'])


class DeterministicBaseline(Candidate):
    """Test baseline only (spec §47): Euclidean rhythm plus a seeded scale walk inside the register.
    Chords, when requested, are stacked scale thirds. It reads no words."""
    id, kind = 'deterministic-euclid-walk', 'deterministic-baseline'

    def generate(self, r):
        rng = random.Random(r['seed'])
        tonic, mode = r['key']; scale = SCALES[mode]
        lo, hi = r['register']
        pitches = [p for p in range(lo, hi + 1) if (p - tonic) % 12 in scale]
        bar = r['beatsPerBar']; steps = int(round(bar / 0.25))
        notes = []
        if r.get('polyphonic'):
            degree = 0
            for b in range(r['bars']):
                root_i = min(range(len(pitches)), key=lambda i: abs(pitches[i] - (lo + 12)) + 100 * ((pitches[i] - tonic) % 12 != scale[degree]))
                for k in (0, 2, 4):
                    if root_i + k < len(pitches):
                        notes.append(dict(p=pitches[root_i + k], t=b * bar, d=bar, v=90))
                degree = (degree + rng.choice((2, 3, 4, 5))) % len(scale)
            return {'notes': notes, 'meta': {}}
        pattern = euclid(rng.randint(steps // 4, steps // 2 + 1), steps, rng.randrange(steps))
        i = rng.randrange(len(pitches))
        for b in range(r['bars']):
            hits = [s for s, on in enumerate(pattern) if on]
            for j, s in enumerate(hits):
                nxt = hits[j + 1] if j + 1 < len(hits) else steps
                notes.append(dict(p=pitches[i], t=b * bar + s * 0.25, d=(nxt - s) * 0.25, v=100 if s % 4 == 0 else 84))
                i = max(0, min(len(pitches) - 1, i + rng.choice((-2, -1, -1, 0, 1, 1, 2))))
        return {'notes': notes, 'meta': {'pulses': sum(pattern)}}


class ProviderBaseline(Candidate):
    """The existing general-LLM path (providers.generate) as a baseline. Paid: needs allow_paid."""
    kind = 'general-llm-baseline'
    accepts_source = True

    def __init__(self, profile, allow_paid=False, call=None):
        self.profile, self.allow_paid = profile, allow_paid
        self.id = 'general-llm:%s' % profile.get('model', '?')
        self._call = call

    def status(self):
        if not self.allow_paid:
            return {'available': False, 'reason': 'paid provider call not approved for this run'}
        return {'available': True, 'reason': ''}

    def generate(self, r):
        import providers
        length = r['bars'] * r['beatsPerBar']
        src = r.get('source') or {'notes': [], 'length': length}
        context = {'instruction': r['intent'], 'source': {'notes': src['notes'], 'length': src['length'], 'tempo': r['tempo']}}
        result, usage = (self._call or providers.generate)(self.profile, context)
        return {'notes': result.get('notes', []), 'meta': {'usage': usage}}


# --- measures --------------------------------------------------------------------------------

def tokens(notes):
    """Interval + inter-onset tokens of the top voice: transposition-invariant, tempo-free."""
    line = midi_analysis.skyline(notes)
    return [(b['p'] - a['p'], round((b['t'] - a['t']) / 0.25)) for a, b in zip(line, line[1:])]


class CopyIndex:
    """N-gram index over reference material (library parts and any extra corpus of .mid files)."""

    def __init__(self, n=NGRAM):
        self.n, self.grams, self.seqs = n, defaultdict(set), {}

    def add(self, ref_id, notes):
        seq = tokens(notes); self.seqs[ref_id] = seq
        for i in range(len(seq) - self.n + 1):
            self.grams[tuple(seq[i:i + self.n])].add(ref_id)

    def add_library(self, library):
        for p in library.parts(notes=True):
            if not p['drums']:
                self.add('library:' + p['id'], p['notes'])
        return self

    def add_corpus(self, folder, label):
        for path in sorted(Path(folder).rglob('*.mid')):
            try:
                for i, part in enumerate(midi_file.read(path.read_bytes())['parts']):
                    if not part['drums']:
                        self.add('%s:%s:%d' % (label, path.name, i), part['notes'])
            except ValueError:
                continue
        return self

    def check(self, notes):
        seq = tokens(notes)
        grams = [tuple(seq[i:i + self.n]) for i in range(len(seq) - self.n + 1)]
        hits = [g for g in grams if g in self.grams]
        refs = set().union(*(self.grams[g] for g in hits)) if hits else set()
        best, best_ref = 0, None
        for ref in refs:  # longest common run, only against references sharing an n-gram
            other = self.seqs[ref]; prev = [0] * (len(other) + 1)
            for a in seq:
                cur = [0] * (len(other) + 1)
                for j, b in enumerate(other, 1):
                    if a == b:
                        cur[j] = prev[j - 1] + 1
                        if cur[j] > best:
                            best, best_ref = cur[j], ref
                prev = cur
        return {'tokens': len(seq), 'sharedNgramShare': round(len(hits) / len(grams), 4) if grams else 0.0,
                'longestSharedRun': best, 'closestReference': best_ref, 'referencesIndexed': len(self.seqs)}


def distance(a, b):
    """1 - overlap of (onset, pitch) pairs: 0 means the same notes, 1 means nothing shared."""
    sa = {(round(n['t'] * 48), n['p']) for n in a}; sb = {(round(n['t'] * 48), n['p']) for n in b}
    return round(1 - len(sa & sb) / len(sa | sb), 4) if sa | sb else 0.0


def measure(notes, request, index=None):
    length = request['bars'] * request['beatsPerBar']
    out = {}
    try:
        clean = music.validate(notes, length); out['valid'] = True; out['error'] = None
    except (ValueError, TypeError) as e:
        out['valid'] = False; out['error'] = str(e); return out, None
    lo, hi = request.get('register') or (0, 127)
    out['outsideRegisterShare'] = round(sum(not lo <= n['p'] <= hi for n in clean) / len(clean), 4)
    out['noteCount'] = len(clean)
    if request.get('source'):
        out['sourceDistance'] = distance(clean, request['source']['notes'])
    if index is not None:
        out['copy'] = index.check(clean)
    return out, clean


# --- run -------------------------------------------------------------------------------------

def resolve(task, accepted=None, reference=None):
    """A task plus its source material. Tasks that need material nobody supplied are skipped, not faked."""
    t = dict(task)
    need = t.get('needs')
    src = accepted if need == 'accepted-take' else reference if need == 'reference-part' else None
    if need and not src:
        return None, 'needs %s; none supplied' % need
    if src:
        t['source'] = {'notes': src['notes'], 'length': src['length']}
        t['bars'] = max(1, int(math.ceil(src['length'] / 4)))
        lows, highs = min(n['p'] for n in src['notes']), max(n['p'] for n in src['notes'])
        t.setdefault('register', [max(0, lows - 12), min(127, highs + 12)])
    return t, None


def _usage():
    s, c = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime, c.ru_utime + c.ru_stime, c.ru_maxrss


def run(candidates, tasks=TASKS, root=None, log=None, index=None, accepted=None, reference=None,
        takes=3, base_seed=1, beats_per_bar=4.0, pairing='across', screen=True):
    """Generate every task with every available candidate, measure, log, and write the blind sheet.
    pairing 'across' pits candidates against each other; 'within' pairs one candidate's own takes."""
    root = Path(root) if root else default_root()
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)
    out = root / run_id; (out / 'key').mkdir(parents=True); (out / 'listening').mkdir()
    log = log or TakeLog()
    report = {'run': run_id, 'started': _now(), 'candidates': {}, 'skippedTasks': {}, 'skippedForCandidate': {}, 'takes': []}
    avail, sources = [], {}
    for c in candidates:
        st = c.status(); report['candidates'][c.id] = {'kind': c.kind, **st}
        if st['available']:
            avail.append(c)
    for task in tasks:
        t, why = resolve(task, accepted, reference)
        if not t:
            report['skippedTasks'][task['id']] = why; continue
        if t.get('source'):
            sources[t['id']] = t['source']
        for c in avail:
            if t.get('source') and not c.accepts_source:
                report['skippedForCandidate'].setdefault(c.id, {})[t['id']] = 'candidate cannot take a source phrase'
                continue
            if not t.get('source') and getattr(c, 'source_only', False):
                report['skippedForCandidate'].setdefault(c.id, {})[t['id']] = 'candidate needs a source phrase (no text input)'
                continue
            for k in range(t.get('takes', takes)):
                req = {'intent': t['intent'], 'caption': caption(t, beats_per_bar), 'bars': t['bars'], 'beatsPerBar': beats_per_bar, 'tempo': t['tempo'],
                       'key': t['key'], 'register': t.get('register'), 'polyphonic': bool(t.get('polyphonic')),
                       'source': t.get('source'), 'seed': base_seed + k}
                cpu0, child0, _ = _usage(); wall0 = time.perf_counter()
                row = {'candidate': c.id, 'kind': c.kind, 'task': t['id'], 'seed': req['seed']}
                try:
                    gen = c.generate(req)
                except Exception as e:  # a failed generation is a result, recorded with its reason
                    row.update(generated=False, error='%s: %s' % (type(e).__name__, e)); report['takes'].append(row); continue
                wall = time.perf_counter() - wall0; cpu1, child1, rss = _usage()
                m, clean = measure(gen['notes'], req, index)
                compute = {'cpuSeconds': round(cpu1 - cpu0, 4), 'childCpuSeconds': round(child1 - child0, 4),
                           'childPeakRssMB_cumulative': round(rss / 1048576, 1)}
                take = log.record_take(t['intent'], c.id, gen['notes'] if clean is None else clean, req['bars'] * beats_per_bar,
                                       seed=req['seed'], settings={k2: req[k2] for k2 in ('bars', 'tempo', 'key', 'register', 'polyphonic')},
                                       source=t.get('source'), task=t['id'], origin='benchmark',
                                       latency=round(wall, 4), compute=compute)
                if gen.get('raw'):
                    (out / 'key' / 'raw').mkdir(exist_ok=True)
                    (out / 'key' / 'raw' / (take + '.mid')).write_bytes(gen['raw'])
                row.update(generated=True, take=take, latencySeconds=round(wall, 4), compute=compute, measures=m,
                           meta=gen.get('meta', {}), notes=clean, length=req['bars'] * beats_per_bar, tempo=t['tempo'])
                report['takes'].append(row)
    _distinctness(report)
    if screen:
        _screen(report, sources, beats_per_bar)
    _sheet(report, out, pairing)
    report['finished'] = _now()
    (out / 'key' / 'measures.json').write_text(json.dumps(
        {**report, 'takes': [{k: v for k, v in r.items() if k != 'notes'} for r in report['takes']]}, indent=1))
    return report, out


def _distinctness(report):
    groups = defaultdict(list)
    for r in report['takes']:
        if r.get('generated') and r['measures'].get('valid'):
            groups[(r['candidate'], r['task'])].append(r)
    for rows in groups.values():
        for r in rows:
            others = [distance(r['notes'], o['notes']) for o in rows if o is not r]
            r['measures']['distinctFromSiblings'] = {'min': min(others), 'mean': round(sum(others) / len(others), 4)} if others else None


# Dud screen: plain facts that make a take not worth a listen. Screened takes stay in the log and
# the key with their reasons; they only leave the listening sheet. These say nothing about taste.
DUD_EMPTY_BAR_SHARE = 0.5      # half or more of the bars silent
DUD_GAP_UNDER_SOURCE = True    # a bar left silent where the source phrase plays
DUD_SOURCE_COPY = 0.25         # sourceDistance below this: essentially the source played back
DUD_LIBRARY_RUN = 12           # interval/rhythm tokens shared in a row with a library phrase (no-source tasks)
DUD_OUTSIDE_REGISTER = 0.6     # most notes outside the requested register (0.6: no take John picked would have gone)


def dud_reasons(row, source=None, beats_per_bar=4.0):
    m, notes = row['measures'], row.get('notes') or []
    bars = max(1, int(round(row['length'] / beats_per_bar)))
    per_bar = lambda ns: [sum(1 for n in ns if b * beats_per_bar <= n['t'] < (b + 1) * beats_per_bar) for b in range(bars)]
    got, why = per_bar(notes), []
    if got.count(0) / bars >= DUD_EMPTY_BAR_SHARE:
        why.append('%d of %d bars silent' % (got.count(0), bars))
    if source and DUD_GAP_UNDER_SOURCE:
        gaps = [i + 1 for i, (g, s) in enumerate(zip(got, per_bar(source['notes']))) if s and not g]
        if gaps:
            why.append('silent in bar %s where the source plays' % ', '.join(map(str, gaps)))
    if source and m.get('sourceDistance') is not None and m['sourceDistance'] < DUD_SOURCE_COPY:
        why.append('plays the source back (distance %.2f)' % m['sourceDistance'])
    if not source and (m.get('copy') or {}).get('longestSharedRun', 0) >= DUD_LIBRARY_RUN:
        why.append('repeats %d tokens of a library phrase' % m['copy']['longestSharedRun'])
    if m.get('outsideRegisterShare', 0) > DUD_OUTSIDE_REGISTER:
        why.append('%d%% of notes outside the register' % round(100 * m['outsideRegisterShare']))
    return why


def _screen(report, sources, beats_per_bar=4.0):
    for r in report['takes']:
        if r.get('generated') and r['measures'].get('valid'):
            why = dud_reasons(r, sources.get(r['task']), beats_per_bar)
            if why:
                r['screenedOut'] = why


TASK_ORDER = {t['id']: i for i, t in enumerate(TASKS)}


def _grouped(pairs):
    """Pairs of the same task sit together (all basslines, then all leads...), so John can set one
    sound per category; order inside a category stays random."""
    return sorted(pairs, key=lambda p: TASK_ORDER.get(p['task'], len(TASK_ORDER)))


def _sheet(report, out, pairing='across'):
    """Pair valid takes of the same task - from different candidates ('across') or from the same candidate
    ('within', each take used once) - and hide identities behind random ids."""
    rng = random.Random(secrets.randbits(64))
    by_task = defaultdict(lambda: defaultdict(list))
    for r in report['takes']:
        if r.get('generated') and r['measures'].get('valid') and not r.get('screenedOut'):
            by_task[r['task']][r['candidate']].append(r)
    key, pairs, used = {}, [], set()

    def anon(r):
        if 'anon' not in r:
            while True:
                a = secrets.token_hex(3).upper()
                if a not in used:
                    used.add(a); break
            r['anon'] = a; key[a] = {'candidate': r['candidate'], 'take': r['take'], 'task': r['task'], 'seed': r['seed']}
            (out / 'listening' / (a + '.mid')).write_bytes(music.midi_bytes(r['notes'], r['length'], r['tempo']))
        return r['anon']

    for task, cands in by_task.items():
        if pairing == 'within':
            for rows in cands.values():
                rows = list(rows); rng.shuffle(rows)
                for x, y in zip(rows[0::2], rows[1::2]):
                    pairs.append({'task': task, 'A': anon(x), 'B': anon(y)})
            continue
        for c1, c2 in combinations(sorted(cands), 2):
            for x, y in zip(cands[c1], cands[c2]):
                a, b = (x, y) if rng.random() < .5 else (y, x)
                pairs.append({'task': task, 'A': anon(a), 'B': anon(b)})
    rng.shuffle(pairs)
    pairs = _grouped(pairs)
    intents = {t['id']: t['intent'] for t in TASKS}
    screened = [r for r in report['takes'] if r.get('screenedOut')]
    (out / 'key' / 'screened.json').write_text(json.dumps(
        [{'candidate': r['candidate'], 'take': r['take'], 'task': r['task'], 'seed': r['seed'], 'reasons': r['screenedOut']}
         for r in screened], indent=1))
    sheet = {'run': report['run'], 'instructions': 'For each pair choose A, B or neither. Notes optional.',
             'screenedOut': len(screened),
             'pairs': [{'pair': 'P%02d' % (i + 1), 'intent': intents.get(p['task'], p['task']), **p, 'choice': None, 'note': ''}
                       for i, p in enumerate(pairs)]}
    (out / 'listening' / 'sheet.json').write_text(json.dumps(sheet, indent=1))
    lines = ['# Blind listening sheet, run %s' % report['run'], '', 'Choose A, B or neither for each pair. Files are in this folder by id.', '',
             '| Pair | Intent | A | B | Choice | Notes |', '|---|---|---|---|---|---|']
    lines += ['| %s | %s | %s | %s | | |' % (p['pair'], p['intent'], p['A'], p['B']) for p in sheet['pairs']]
    (out / 'listening' / 'sheet.md').write_text('\n'.join(lines) + '\n')
    (out / 'key' / 'key.json').write_text(json.dumps(key, indent=1))
    report['pairs'] = len(pairs); report['screenedOut'] = len(screened)


def short_sheet(run_dir, per_task=2):
    """A listenable subset of the full sheet: `per_task` pairs per task, spreading model match-ups
    evenly and never using the same take twice (a repeat wastes a listen and biases the tally).
    The full sheet is kept as sheet-full.json."""
    run_dir = Path(run_dir); listening = run_dir / 'listening'
    full_path = listening / 'sheet-full.json'
    full = json.loads((full_path if full_path.exists() else listening / 'sheet.json').read_text())
    full_path.write_text(json.dumps(full, indent=1))
    key = json.loads((run_dir / 'key' / 'key.json').read_text())
    rng = random.Random(secrets.randbits(64))
    combo = lambda p: tuple(sorted((key[p['A']]['candidate'], key[p['B']]['candidate'])))
    use, used, chosen, by_task = defaultdict(int), set(), [], defaultdict(list)
    for p in full['pairs']:
        by_task[p['task']].append(dict(p))
    for task in sorted(by_task):
        pool = by_task[task]; rng.shuffle(pool)
        for _ in range(per_task):
            fresh = [p for p in pool if p['A'] not in used and p['B'] not in used]
            if not fresh:
                break
            fresh.sort(key=lambda p: use[combo(p)])
            p = fresh[0]; pool.remove(p); used.update((p['A'], p['B'])); use[combo(p)] += 1; chosen.append(p)
    rng.shuffle(chosen)
    chosen = _grouped(chosen)
    for i, p in enumerate(chosen):
        p['pair'] = 'P%02d' % (i + 1); p['choice'] = None; p['note'] = ''
    sheet = {**full, 'pairs': chosen, 'note': 'Short sheet: %d pairs, no take repeated (full sheet: sheet-full.json).' % len(chosen)}
    (listening / 'sheet.json').write_text(json.dumps(sheet, indent=1))
    lines = ['# Blind listening sheet, run %s (%d pairs)' % (sheet['run'], len(chosen)), '',
             'Choose A, B or neither for each pair.', '', '| Pair | Intent | A | B | Choice | Notes |', '|---|---|---|---|---|---|']
    lines += ['| %s | %s | %s | %s | | |' % (p['pair'], p['intent'], p['A'], p['B']) for p in chosen]
    (listening / 'sheet.md').write_text('\n'.join(lines) + '\n')
    return sheet


def score(run_dir, log=None):
    """Read the filled sheet, log each comparison, and reveal per-candidate results."""
    run_dir = Path(run_dir); log = log or TakeLog()
    sheet = json.loads((run_dir / 'listening' / 'sheet.json').read_text())
    key = json.loads((run_dir / 'key' / 'key.json').read_text())
    tally = defaultdict(lambda: {'wins': 0, 'losses': 0, 'neither': 0, 'byTask': defaultdict(lambda: [0, 0, 0])})
    answered = 0
    for p in sheet['pairs']:
        choice = p.get('choice')
        if choice is None:
            continue
        if choice not in ('A', 'B', 'neither'):
            raise ValueError('%s: choice must be A, B or neither.' % p['pair'])
        a, b = key[p['A']], key[p['B']]
        log.record_comparison(a['take'], b['take'], choice, p.get('note', ''), 'user', sheet=sheet['run'])
        answered += 1
        for side, info in (('A', a), ('B', b)):
            t = tally[info['candidate']]
            if choice == 'neither':
                t['neither'] += 1; t['byTask'][info['task']][2] += 1
            elif choice == side:
                t['wins'] += 1; t['byTask'][info['task']][0] += 1
            else:
                t['losses'] += 1; t['byTask'][info['task']][1] += 1
    result = {'run': sheet['run'], 'answered': answered, 'pairs': len(sheet['pairs']),
              'candidates': {c: {**{k: v for k, v in t.items() if k != 'byTask'},
                                 'byTask': {k: dict(zip(('wins', 'losses', 'neither'), v)) for k, v in t['byTask'].items()}}
                             for c, t in tally.items()}}
    (run_dir / 'key' / 'score.json').write_text(json.dumps(result, indent=1))
    return result


def main(argv=None):
    """python3 midi_bench.py status | run [--tasks a,b] [--takes N] [--only id,id] [--baseline] | score RUN_DIR"""
    import argparse
    ap = argparse.ArgumentParser(description='Scrunch MIDI foundation benchmark')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    r = sub.add_parser('run')
    r.add_argument('--tasks', default=''); r.add_argument('--takes', type=int, default=3)
    r.add_argument('--only', default=''); r.add_argument('--baseline', action='store_true')
    r.add_argument('--seed', type=int, default=1)
    r.add_argument('--accepted', help='MIDI file of a phrase John accepted, for the variation task')
    r.add_argument('--within', action='store_true', help="pair each candidate's own takes instead of candidates against each other")
    s = sub.add_parser('score'); s.add_argument('run_dir')
    a = ap.parse_args(argv)
    if a.cmd == 'status':
        for c in configured():
            print(json.dumps({'id': c.id, 'kind': c.kind, **c.status()}))
        return
    if a.cmd == 'score':
        print(json.dumps(score(a.run_dir), indent=1)); return
    cands = [c for c in configured() if not a.only or c.id in a.only.split(',')]
    if a.baseline:
        cands.append(DeterministicBaseline())
    tasks = [t for t in TASKS if not a.tasks or t['id'] in a.tasks.split(',')]
    import midi_library
    lib = midi_library.Library()
    index = CopyIndex().add_library(lib)
    accepted = None
    if a.accepted:
        parsed = midi_file.read(Path(a.accepted).read_bytes())
        notes, _ = fit(parsed, max(p['length'] for p in parsed['parts']))
        accepted = {'notes': notes, 'length': max(p['length'] for p in parsed['parts']), 'file': str(a.accepted)}
    report, out = run(cands, tasks, index=index, takes=a.takes, base_seed=a.seed, accepted=accepted,
                      pairing='within' if a.within else 'across')
    lib.close()
    print(json.dumps({'run': report['run'], 'folder': str(out), 'candidates': report['candidates'],
                      'skippedTasks': report['skippedTasks'], 'skippedForCandidate': report['skippedForCandidate'],
                      'pairs': report.get('pairs')}, indent=1))


if __name__ == '__main__':
    main()
