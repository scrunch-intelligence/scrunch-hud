"""Audio clip in the Arrangement -> MIDI track beneath it, transcribed by MuScriptor large.

John chose MuScriptor large by ear (evidence/audio-to-midi-decision-2026-09-27.md). The model's output
is evidence, not material to improve: the raw MIDI is kept, and the only processing is the fixed
normalization every audio-to-MIDI contender got, plus what a Live clip can physically hold:
  1. every part the model wrote goes onto one track;
  2. a note of the same pitch starting within 50 ms of a kept one is dropped (parts doubling a line);
  3. midi_bench.playable: one note per pitch per onset, a note ends where the next of its pitch starts;
  4. geometry: seconds -> the clip's own beats through Live's warp markers, laid out the way the clip
     plays (start offset, loop), and a note is cut at the clip end or loop point, as the audio is.
Counts for 2 and 3 are reported with the result. No quantizing, no velocity change, no repair.

Placement follows engine.import_attempt's discipline: intent written before each write, the clip read
back and compared with notes_equal, a mismatch keeps the track for inspection, nothing retried blindly.
New MIDI track directly below the audio track, gain -12 dB, not armed, Arrangement View only.
"""
import base64, json, os, subprocess
from datetime import datetime, timezone
from pathlib import Path

import engine
from midi_bench import playable

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / 'data' / 'midi-models'


class Runner:
    """The MuScriptor runner process (midi_runners/muscriptor_runner.py in its own venv)."""
    argv = [str(MODELS / 'venvs' / 'muscriptor' / 'bin' / 'python'), str(ROOT / 'midi_runners' / 'muscriptor_runner.py')]
    requires = [MODELS / 'checkpoints' / 'muscriptor-large' / 'model.safetensors',
                MODELS / 'torch' / 'hub' / 'checkpoints' / 'beat_this-final0.ckpt']
    timeout = 900

    def status(self):
        missing = [str(p) for p in [Path(self.argv[0])] + self.requires if not p.exists()]
        return {'available': not missing, 'reason': ('missing ' + ', '.join(missing)) if missing else ''}


RUNNER = Runner()


class HumRunner:
    """Humming / one sung or whistled line: pYIN pitch tracking (midi_runners/hum_runner.py), no model files."""
    # Scrunch's own Python (uv) on John's Mac; an installed copy has no .venv-scrunch and uses the MIDI venv setup made
    # (torch + librosa + torchcrepe sit there beside MuScriptor).
    argv = [str(ROOT / '.venv-scrunch' / 'bin' / 'python') if (ROOT / '.venv-scrunch').exists() else str(MODELS / 'venvs' / 'muscriptor' / 'bin' / 'python'),
            str(ROOT / 'midi_runners' / 'hum_runner.py')]
    requires = []
    timeout = 300
    name, slug = 'pYIN humming', 'hum'

    def status(self):
        ok = Path(self.argv[0]).exists()
        return {'available': ok, 'reason': '' if ok else 'missing ' + self.argv[0]}


Runner.name, Runner.slug = 'MuScriptor large', 'muscriptor_large'
HUM = HumRunner()
RUNNERS = {'muscriptor': RUNNER, 'hum': HUM}
GRIDS = {'': 0, '1/4': 1.0, '1/8': 0.5, '1/16': 0.25}


def correct(notes, octave=0, quantize=''):
    """Optional corrections the user asked for, applied after the raw transcription is saved and listed in
    the result: shift every note by whole octaves; snap note starts (and ends) to a grid. Nothing else."""
    if octave not in range(-3, 4) or quantize not in GRIDS:
        raise ValueError('Octave shift must be -3..3 and quantize one of none, 1/4, 1/8, 1/16.')
    g, out, moved = GRIDS[quantize], [], 0
    for n in notes:
        m = dict(n, p=n['p'] + 12 * octave)
        if g:
            t = round(round(n['t'] / g) * g, 6); e = max(t + g, round(round((n['t'] + n['d']) / g) * g, 6))
            moved += (t != n['t']) or (round(e - t, 6) != n['d'])
            m.update(t=t, d=round(e - t, 6))
        if not 0 <= m['p'] <= 127:
            raise ValueError('An octave shift of %+d puts notes outside the MIDI range.' % octave)
        out.append(m)
    out, _ = playable(sorted(out, key=lambda n: (n['t'], n['p'])))
    return out, {'octaveShift': octave, 'quantize': quantize or 'none', 'notesMovedByQuantize': moved}
DUPLICATE_SECONDS = 0.05
DEFAULT_INSTRUMENT = 'Grand Piano.adg'
EVIDENCE = ROOT / 'data' / 'audio-to-midi'


class Warp:
    """Clip beat <-> seconds in the file, piecewise linear through Live's warp markers. Outside the markers
    the nearest segment's rate continues, as Live plays it."""

    def __init__(self, markers):
        pts = sorted({(float(m['beat']), float(m['seconds'])) for m in markers or []})
        if len(pts) < 2 or any(b1 <= b0 or s1 <= s0 for (b0, s0), (b1, s1) in zip(pts, pts[1:])):
            raise ValueError('The clip has no usable warp markers (need two or more, rising in beat and time).')
        self.pts = pts

    @staticmethod
    def _interp(x, xs, ys):
        i = 1
        while i < len(xs) - 1 and x > xs[i]:
            i += 1
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        return y0 + (x - x0) * (y1 - y0) / (x1 - x0)

    def seconds(self, beat):
        return self._interp(beat, [p[0] for p in self.pts], [p[1] for p in self.pts])

    def beat(self, seconds):
        return self._interp(seconds, [p[1] for p in self.pts], [p[0] for p in self.pts])


def segments(clip):
    """How the clip plays: [(beat in the clip as placed, clip beat from, clip beat to)], covering its span."""
    span, offset = float(clip['span']), float(clip.get('offset') or 0)
    if not span > 0:
        raise ValueError('The clip has no length.')
    if not clip.get('looping'):
        return [(0.0, offset, offset + span)]
    left, right = float(clip['loopStart']), float(clip['loopEnd'])
    if not right > left:
        raise ValueError('The clip loop markers are unreadable.')
    cursor = offset if offset < right else left + (offset - left) % (right - left)
    out, at = [], 0.0
    while at < span - 1e-9:
        take = min(right - cursor, span - at)
        out.append((at, cursor, cursor + take))
        at += take; cursor = left
    return out


def merge(parts):
    """Normalization steps 1-2. Returns (notes in seconds, number dropped)."""
    notes = sorted((n for p in parts for n in p['notes']), key=lambda n: (n['p'], n['s'], -(n['e'] - n['s'])))
    kept, dropped = [], 0
    for n in notes:
        if kept and kept[-1]['p'] == n['p'] and n['s'] - kept[-1]['s'] < DUPLICATE_SECONDS:
            dropped += 1
            continue
        kept.append(n)
    return sorted(kept, key=lambda n: (n['s'], n['p'])), dropped


def lay_out(notes, warp, segs, span_start):
    """Normalization step 4: notes (seconds from span_start) -> clip-relative beats as the clip plays them."""
    out = []
    for n in notes:
        b0, b1 = warp.beat(span_start + n['s']), warp.beat(span_start + n['e'])
        for at, lo, hi in segs:
            if lo <= b0 < hi:
                d = min(b1, hi) - b0
                if d > 1e-6:
                    out.append({'p': n['p'], 't': round(at + b0 - lo, 6), 'd': round(d, 6), 'v': n['v']})
    return sorted(out, key=lambda n: (n['t'], n['p']))


def find_clip(adapter, track_id, clip_id=''):
    track = adapter.track(track_id, clips=True)
    if track.get('kind') != 'audio':
        raise ValueError('%s is not an audio track.' % track.get('name'))
    clips = track.get('clips') or []
    if clip_id:
        clips = [c for c in clips if c['id'] == clip_id]
        if not clips:
            raise ValueError('That clip is not on %s.' % track['name'])
    if len(clips) != 1:
        raise ValueError('%s has %d arrangement clips; say which one to transcribe (%s).' % (
            track['name'], len(clips), ', '.join('%s at bar %g' % (c['name'], c['start'] / 4 + 1) for c in clips) or 'none'))
    clip = adapter.clip(clips[0]['id'], notes=False)
    if clip.get('kind') != 'audio' or clip.get('view') != 'arrangement':
        raise ValueError('Expected an Arrangement audio clip.')
    return track, clip


def check_source(track, clip):
    """Refuse, before anything runs or is written, audio that can't be placed on the beat grid truthfully."""
    where = '"%s" on track "%s"' % (clip.get('name'), track.get('name'))
    if not clip.get('filePath') or not Path(clip['filePath']).exists():
        raise ValueError('The audio file for %s is not available on this Mac (%s). Nothing was changed.' % (where, clip.get('filePath')))
    if 'warping' not in clip:
        raise ValueError('Live did not report whether %s is warped, so MIDI was not extracted. Nothing was changed.' % where)
    if clip['warping'] is not True:
        raise ValueError('The clip %s is not warped. Warp it to the Set tempo first (turn Warp on and check the warp markers '
                         'sit on the beat), then ask again. MIDI is only extracted from warped audio so the notes land on '
                         'the right bars. Nothing was changed.' % where)
    Warp(clip.get('warpMarkers'))


def transcribe(clip, runner=RUNNER):
    """Run MuScriptor on exactly the audio the clip plays. Returns the notes to place and the evidence."""
    st = runner.status()
    if not st['available']:
        raise ValueError('%s is not available: %s' % (getattr(runner, 'name', 'The transcriber'), st['reason']))
    warp, segs = Warp(clip.get('warpMarkers')), segments(clip)
    lo, hi = min(s[1] for s in segs), max(s[2] for s in segs)
    start, end = max(0.0, warp.seconds(lo)), warp.seconds(hi)
    req = {'audio': clip['filePath'], 'startSeconds': start, 'endSeconds': end}
    if runner is HUM:   # shortest hummed note = one 1/16 at the clip's own tempo
        req['minNoteSeconds'] = round(warp.seconds(lo + 0.25) - warp.seconds(lo), 4)
    done = subprocess.run(runner.argv, input=json.dumps(req),
                          capture_output=True, text=True, timeout=runner.timeout, env=dict(os.environ))
    if done.returncode:
        raise RuntimeError('%s exited %d: %s' % (getattr(runner, 'name', 'Transcriber'), done.returncode, done.stderr.strip()[-400:]))
    out = json.loads(done.stdout)
    meta = out['meta']
    merged, dropped = merge(meta['parts'])
    notes, fitted = playable(lay_out(merged, warp, segs, meta['spanSeconds'][0]))
    report = {'model': meta['model'], 'device': meta['device'], 'spanSeconds': meta['spanSeconds'],
              'loadSeconds': meta['loadSeconds'], 'transcribeSeconds': meta['transcribeSeconds'],
              'parts': [{'name': p['name'], 'program': p['program'], 'drum': p['drum'], 'notes': len(p['notes'])} for p in meta['parts']],
              'rawNotes': sum(len(p['notes']) for p in meta['parts']), 'duplicatesDropped': dropped,
              'livePlayableAdjusted': fitted, 'placedNotes': len(notes),
              **{k: meta[k] for k in ('shortNotesDropped', 'voicedFraction', 'hummerTuningCents') if k in meta},
              **({'centsOffPitch': [n.get('cents') for p in meta['parts'] for n in p['notes']]} if any('cents' in n for p in meta['parts'] for n in p['notes']) else {})}
    return notes, report, base64.b64decode(out['midi_base64'])


def _save(folder, state):
    (folder / 'result.json').write_text(json.dumps(state, indent=1))


def extract(track_id, clip_id='', instrument=DEFAULT_INSTRUMENT, adapter=None, runner=RUNNER, octave=0, quantize=''):
    """Transcribe the audio clip on `track_id` and place it on a new MIDI track right below. Returns the state."""
    adapter = adapter or engine.host()
    track, clip = find_clip(adapter, track_id, clip_id)
    check_source(track, clip)
    folder = EVIDENCE / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + clip['id'].split(':')[-1][-6:])
    folder.mkdir(parents=True, exist_ok=True)
    state = {'stage': 'transcribing', 'source': {'trackId': track['id'], 'trackName': track['name'], 'clipId': clip['id'],
             'clipName': clip['name'], 'filePath': clip['filePath'], 'start': clip['start'], 'span': clip['span'],
             'offset': clip.get('offset'), 'looping': clip.get('looping'), 'loopStart': clip.get('loopStart'),
             'loopEnd': clip.get('loopEnd'), 'warpMarkers': clip.get('warpMarkers')}}
    _save(folder, state)
    notes, report, raw = transcribe(clip, runner)
    (folder / ('raw_%s.mid' % getattr(runner, 'slug', 'muscriptor_large'))).write_bytes(raw)
    (folder / 'raw_notes_beats.json').write_text(json.dumps(notes))
    if octave or quantize:
        notes, report['corrections'] = correct(notes, octave, quantize)
        report['placedNotes'] = len(notes)
    state.update(stage='creating-track', transcription=report); _save(folder, state)
    if not notes:
        state['stage'] = 'no-notes'; _save(folder, state)
        raise ValueError('%s found no notes in %s. Nothing was added to the Set.' % (getattr(runner, 'name', 'MuScriptor'), clip['name']))
    tag = 'Hum' if runner is HUM else 'MuScriptor'
    fix = report.get('corrections')
    if fix:
        tag += ''.join([' oct%+d' % octave if octave else '', ' q' + quantize if quantize else ''])
    name = ('%s MIDI - %s' % (clip['name'], tag))[:60]
    # Fresh read immediately before the mutation: indexes can change after edits.
    tracks = adapter.session()['tracks']
    source = [t for t in tracks if t['id'] == track['id']]
    if not source:
        raise ValueError('The audio track moved or was deleted while transcribing. Nothing was added.')
    before = {t['id'] for t in tracks}
    adapter.create_midi_track(name, index=source[0]['trackIndex'] + 1, mute=False, arm=False)
    new = [t for t in adapter.session()['tracks'] if t['id'] not in before]
    if len(new) != 1 or new[0]['name'] != name:
        state['stage'] = 'track-unidentified'; _save(folder, state)
        raise ValueError('Could not identify the new MIDI track; stopped. Inspect Live.')
    new = new[0]
    state.update(stage='track-created', trackId=new['id'], trackName=name); _save(folder, state)
    adapter.update_track(new['id'], gainDb=-12.0, arm=False)
    if instrument:
        adapter.load_device(new['id'], instrument, shelf='instruments')
        state['instrument'] = instrument
    state['stage'] = 'creating-clip'; _save(folder, state)
    created = adapter.create_arrangement_clip(new['trackIndex'], float(clip['span']), notes, start=float(clip['start']),
                                              name=name, looping=False)
    state.update(stage='clip-created', clipId=created['id']); _save(folder, state)
    got = engine.clip_notes(adapter.clip(created['id']), float(clip['span']))
    if not engine.notes_equal(got, notes):
        state['stage'] = 'readback-mismatch'; _save(folder, state)
        raise ValueError('The MIDI clip did not read back identically. The track is kept for inspection.')
    state.update(stage='placed', verified=True, bar=clip['start'] / 4 + 1, bars=clip['span'] / 4,
                 placedAt=datetime.now(timezone.utc).isoformat(), evidence=str(folder))
    _save(folder, state)
    return state


def summary(state):
    r = state['transcription']
    c = r.get('corrections')
    fixes = '' if not c else ' Corrections you asked for: %s%s.' % (
        'octave %+d' % c['octaveShift'] if c['octaveShift'] else '',
        (', ' if c['octaveShift'] and c['quantize'] != 'none' else '') + ('quantized to %s (%d notes moved)' % (c['quantize'], c['notesMovedByQuantize']) if c['quantize'] != 'none' else ''))
    return ('Transcribed %s with %s: %d notes on "%s" (new track below it%s), bar %g, %g bars, read back note for note.%s The original audio is untouched above it.'
            % (state['source']['clipName'], r.get('model', 'MuScriptor large'), r['placedNotes'], state['trackName'],
               ', ' + state['instrument'].replace('.adg', '') if state.get('instrument') else '', state['bar'], state['bars'], fixes))
