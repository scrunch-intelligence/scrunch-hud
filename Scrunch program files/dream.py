"""Dream: hum a melody on a phone, get MIDI back, send it to Scrunch's inbox, place it in Live on request.

Phone side is dream_server.py (its own port, own key, nothing else of Scrunch exposed). This module owns the
inbox (data/dream/inbox/<id>/) and is also used by server.py for the Scrunch panel and Live placement.

Per take, kept: the phone's original upload (audio.<ext>), the WAV the transcriber read (take.wav), the raw MIDI
the hum runner wrote (raw_hum.mid) and take.json (raw notes in seconds, tempo estimate, status). Octave/quantize
are applied only when asked, on export or placement, and named; the raw notes are never rewritten.

Timing. Keith plays live, so free time is the default: tempo and beat phase are estimated from the take
(librosa beat tracker on the audio, phase refined on the note onsets). With the optional phone click, the
click's bpm is used and only the phase is estimated (phone record latency is unknown, so the onsets decide).
Placement modes: 'pulse' maps his estimated beats onto Set beats (the melody conforms to the Set tempo);
'time' keeps his literal timing at the Set tempo (lines up with the audio when it plays unwarped).
"""
import base64, json, re, secrets, shutil, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / 'data' / 'dream' / 'inbox'
KEYFILE = ROOT / 'data' / 'dream' / 'key'
def _ffmpeg():
    for f in (shutil.which('ffmpeg'), str(Path.home() / '.local/bin/ffmpeg')):
        if f and Path(f).exists(): return f
    try:   # an installed copy: the ffmpeg binary that ships in the imageio-ffmpeg wheel (setup installs it)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


FFMPEG = _ffmpeg()
MAX_BYTES = 40_000_000          # about 10 minutes of phone audio
MAX_SECONDS = 600
TYPES = {'audio/webm': 'webm', 'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a', 'audio/aac': 'aac', 'audio/ogg': 'ogg',
         'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/mpeg': 'mp3'}
GRIDS = {'': 0, '1/4': 1.0, '1/8': 0.5, '1/16': 0.25}


def key():
    """The pairing key the phone must send (X-Dream-Key). Made once; delete the file to revoke every phone."""
    if not KEYFILE.exists():
        KEYFILE.parent.mkdir(parents=True, exist_ok=True)
        KEYFILE.write_text(secrets.token_urlsafe(18)); KEYFILE.chmod(0o600)
    return KEYFILE.read_text().strip()


def _folder(tid):
    if not re.fullmatch(r'[0-9TZ]{16}-[0-9a-f]{6}', str(tid)):
        raise ValueError('Unknown take.')
    f = INBOX / tid
    if not (f / 'take.json').exists():
        raise ValueError('Unknown take.')
    return f


def load(tid):
    return with_key(json.loads((_folder(tid) / 'take.json').read_text()))


def _save(take):
    (INBOX / take['id'] / 'take.json').write_text(json.dumps(take, indent=1))


def estimate_tempo(wav, notes, click_bpm=None):
    """Beats per minute + phase (seconds of the first beat) + how well the note onsets sit on the 1/8 grid."""
    import numpy as np, librosa
    onsets = np.array([n['s'] for n in notes], float)
    if click_bpm:
        bpm, source = float(click_bpm), 'click'
    else:
        y, sr = librosa.load(wav, sr=22050, mono=True)
        t, _ = librosa.beat.beat_track(y=y, sr=sr, start_bpm=110, units='time')
        bpm, source = float(np.atleast_1d(t)[0]), 'estimated'
        if not bpm > 0 and len(onsets) > 2:   # silent-ish take: fall back to note onsets
            bpm = 60.0 / float(np.median(np.diff(onsets)))
        while bpm < 70: bpm *= 2       # fold into the range a band counts in
        while bpm > 160: bpm /= 2
    beat = 60.0 / bpm
    if len(onsets):
        ang = 2 * np.pi * (onsets / (beat / 2))      # phase on the 1/8 grid, circular mean of onsets
        phase = (np.angle(np.mean(np.exp(1j * ang))) / (2 * np.pi)) * (beat / 2)
        phase = phase % (beat / 2)
        err = np.abs(((onsets - phase) / (beat / 2) + 0.5) % 1 - 0.5) * (beat / 2)
        fit = float(np.mean(err < 0.06))
    else:
        phase, fit = 0.0, 0.0
    return {'bpm': round(bpm, 2), 'source': source, 'firstBeatSeconds': round(float(phase), 4),
            'onsetsOnEighthGrid': round(fit, 2)}


def ingest(raw, content_type, name='', click_bpm=None, who='', runner_argv=None, timeout=300):
    """Save an upload, transcribe it with the hum runner, estimate tempo. Returns the take (status 'phone')."""
    import audio_to_midi as a2m
    ext = TYPES.get((content_type or '').split(';')[0].strip().lower())
    if not ext:
        raise ValueError('Unsupported audio type %r.' % content_type)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Recording is empty or too long.')
    if click_bpm is not None:
        click_bpm = float(click_bpm)
        if not 40 <= click_bpm <= 240: raise ValueError('Click tempo must be 40-240 bpm.')
    tid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)
    folder = INBOX / tid; folder.mkdir(parents=True)
    try:
        (folder / ('audio.' + ext)).write_bytes(raw)
        wav = folder / 'take.wav'
        done = subprocess.run([FFMPEG, '-v', 'error', '-y', '-i', str(folder / ('audio.' + ext)), '-t', str(MAX_SECONDS),
                               '-ac', '1', '-ar', '44100', '-c:a', 'pcm_s16le', str(wav)], capture_output=True, text=True, timeout=120)
        if done.returncode or not wav.exists():
            raise ValueError('Could not read the recording: ' + done.stderr.strip()[-200:])
        import soundfile
        dur = soundfile.info(str(wav)).duration
        req = {'audio': str(wav)}
        if click_bpm: req['minNoteSeconds'] = round(15.0 / click_bpm, 4)   # one 1/16 at the click
        t0 = time.time()
        out = subprocess.run(runner_argv or a2m.HUM.argv, input=json.dumps(req), capture_output=True, text=True, timeout=timeout)
        if out.returncode:
            raise RuntimeError('Transcriber failed: ' + out.stderr.strip()[-300:])
        res = json.loads(out.stdout); meta = res['meta']
        (folder / 'raw_hum.mid').write_bytes(base64.b64decode(res['midi_base64']))
        notes = meta['parts'][0]['notes']
        take = {'id': tid, 'name': (str(name).strip() or 'Dream ' + tid[9:13])[:40], 'by': str(who)[:40],
                'created': datetime.now(timezone.utc).isoformat(), 'status': 'phone', 'seconds': round(dur, 2),
                'upload': 'audio.' + ext, 'notes': notes, 'tempo': estimate_tempo(str(wav), notes, click_bpm),
                'transcriber': {k: meta.get(k) for k in ('model', 'hummerTuningCents', 'voicedFraction', 'shortNotesDropped', 'transcribeSeconds')},
                'wallSeconds': round(time.time() - t0, 2)}
        _save(take)
        return take
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def takes(status=None):
    out = []
    for f in sorted(INBOX.glob('*/take.json'), reverse=True):
        t = json.loads(f.read_text())
        if status is None or t['status'] in status:
            out.append({k: t[k] for k in ('id', 'name', 'by', 'created', 'status', 'seconds', 'tempo')} | {'noteCount': len(t['notes']), 'kind': t.get('kind', 'take'), 'key': with_key(t)['key']} | ({'suggested': t['suggested']} if t.get('suggested') else {}) | ({'placed': t['placed']} if t.get('placed') else {}))
    return out


def send(tid, octave=0, quantize='', note='', snap=False):
    """Phone -> Scrunch inbox, with the player's own octave/quantize choice as a suggestion. Only marks it."""
    t = load(tid)
    if int(octave) not in range(-3, 4) or quantize not in GRIDS:
        raise ValueError('Octave must be -3..3 and quantize one of none, 1/4, 1/8, 1/16.')
    if t['status'] == 'phone':
        t.update(status='inbox', sent=datetime.now(timezone.utc).isoformat(),
                 suggested={'octave': int(octave), 'quantize': quantize, 'snap': bool(snap)}, note=str(note)[:500])
        _save(t)
    return t


def rename(tid, name):
    t = load(tid); t['name'] = str(name).strip()[:40] or t['name']; _save(t); return t


def remove(tid):
    shutil.rmtree(_folder(tid)); return {'deleted': tid}


NOTE_NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]   # Krumhansl-Kessler key profiles
MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def key_of(notes):
    """Most likely key of the hummed notes (Krumhansl-Schmuckler, weighted by note length). A guess, reported as one."""
    import numpy as np
    h = np.zeros(12)
    for n in notes: h[n['p'] % 12] += max(0.05, n['e'] - n['s'])
    if len(notes) < 3 or not h.any() or np.count_nonzero(h) < 2:
        return None
    r, tonic, major = max(((float(np.corrcoef(np.roll(prof, k), h)[0, 1]), k, prof is MAJOR) for k in range(12) for prof in (MAJOR, MINOR)),
                          key=lambda x: x[0])
    # minor: keep the raised 7th (harmonic minor, common in trance) when the hum itself uses it for 10%+ of its length;
    # measured on 440 hums: no loss vs plain minor (0.570 vs 0.572), and it protects a sung leading note
    harmonic = not major and h[(tonic + 11) % 12] / h.sum() >= 0.10
    steps = [0, 2, 4, 5, 7, 9, 11] if major else [0, 2, 3, 5, 7, 8, 10] + ([11] if harmonic else [])
    return {'name': '%s %s' % (NOTE_NAMES[tonic], 'major' if major else 'harmonic minor' if harmonic else 'minor'), 'tonic': tonic, 'major': major,
            'scale': sorted((tonic + x) % 12 for x in steps), 'fit': round(r, 2)}


def with_key(take):
    if 'key' not in take: take['key'] = key_of(take['notes'])
    return take


def snap_pitch(p, cents, scale):
    """An out-of-key note moves one semitone into the key, towards the side the singer was leaning (cents)."""
    if p % 12 in scale: return p
    up, dn = (p + 1) % 12 in scale, (p - 1) % 12 in scale
    return p + 1 if up and (not dn or (cents or 0) > 0) else p - 1 if dn else p


def beats(take, mode='pulse', set_tempo=None):
    """Raw notes -> [{p,t,d,v}] in beats. pulse: his own beats (first beat containing a note = beat 0).
    time: literal seconds at `set_tempo` (from the start of the recording)."""
    import math
    notes = take['notes']
    if mode == 'time':
        k = float(set_tempo or take['tempo']['bpm']) / 60.0
        return [{'p': n['p'], 't': round(n['s'] * k, 6), 'd': round((n['e'] - n['s']) * k, 6), 'v': n['v'], 'c': n.get('cents', 0)} for n in notes]
    if mode != 'pulse':
        raise ValueError('Timing must be pulse or time.')
    tp = take['tempo']; k = tp['bpm'] / 60.0; ph = tp['firstBeatSeconds']
    b = [((n['s'] - ph) * k, (n['e'] - n['s']) * k) for n in notes]
    zero = math.floor(min(x for x, _ in b)) if b else 0
    return [{'p': n['p'], 't': round(x - zero, 6), 'd': round(d, 6), 'v': n['v'], 'c': n.get('cents', 0)} for n, (x, d) in zip(notes, b)]


def shaped(take, mode='pulse', octave=0, quantize='', set_tempo=None, snap=False):
    import audio_to_midi as a2m
    from midi_bench import playable
    b = beats(take, mode, set_tempo); moved = 0
    if snap:   # optional, named: out-of-key notes into the detected key
        k = with_key(take)['key']
        if not k: raise ValueError('Too few notes to find a key.')
        for n in b:
            q = snap_pitch(n['p'], n['c'], set(k['scale'])); moved += q != n['p']; n['p'] = q
    for n in b: n.pop('c', None)
    notes, _ = playable(sorted(b, key=lambda n: (n['t'], n['p'])))
    fix = {'octaveShift': 0, 'quantize': 'none', 'notesMovedByQuantize': 0}
    if octave or quantize:
        notes, fix = a2m.correct(notes, int(octave), quantize)
    if snap: fix.update(snapToKey=take['key']['name'], notesMovedToKey=moved)
    return notes, fix


def midi_file(take, mode='pulse', octave=0, quantize='', bpm=None, snap=False):
    """Standard MIDI file at the take's own tempo, or at `bpm` if chosen (same notes, faster or slower)."""
    import io, mido
    notes, _ = shaped(take, mode, octave, quantize, set_tempo=take['tempo']['bpm'], snap=snap)
    f = mido.MidiFile(ticks_per_beat=480); tr = mido.MidiTrack(); f.tracks.append(tr)
    bpm = float(bpm or take['tempo']['bpm'])
    if not 30 <= bpm <= 300: raise ValueError('Tempo must be 30-300 bpm.')
    tr.append(mido.MetaMessage('set_tempo', tempo=int(round(60_000_000 / bpm))))
    tr.append(mido.MetaMessage('track_name', name=take['name']))
    ev = sorted([(n['t'], 1, n) for n in notes] + [(n['t'] + n['d'], 0, n) for n in notes], key=lambda x: (x[0], x[1]))
    last = 0
    for t, on, n in ev:
        tick = int(round(t * 480))
        tr.append(mido.Message('note_on' if on else 'note_off', note=n['p'], velocity=n['v'] if on else 0, time=tick - last)); last = tick
    b = io.BytesIO(); f.save(file=b); return b.getvalue()


def place(tid, bar=1, mode='pulse', octave=0, quantize='', instrument='Grand Piano.adg', adapter=None, snap=False):
    """Put the take in Live's Arrangement on two new tracks at the end of the Set: the original audio, and the
    MIDI below it (-12 dB, not armed). Transport, tempo and existing tracks are not touched. Read back note for note."""
    import engine
    t = load(tid)
    if t['status'] == 'phone':
        raise ValueError('Send the take to Scrunch from the phone first.')
    bar = int(bar)
    if bar < 1: raise ValueError('Bar must be 1 or later.')
    adapter = adapter or engine.host()
    folder = _folder(tid)
    s = adapter.session(); tempo = float(s['tempo'])
    notes, fix = shaped(t, mode, octave, quantize, set_tempo=tempo, snap=snap)
    if not notes: raise ValueError('The take has no notes to place.')
    start = (bar - 1) * 4.0
    length = max(4.0, -(-max(n['t'] + n['d'] for n in notes) // 4) * 4)
    tag = ''.join([' oct%+d' % int(octave) if octave else '', ' q' + quantize if quantize else '', ' in key' if snap else ''])
    base = ('Dream %s' % t['name'])[:44]
    log = {'stage': 'audio-track', 'bar': bar, 'mode': mode, 'setTempo': tempo, 'corrections': fix, 'at': datetime.now(timezone.utc).isoformat()}
    t['placing'] = log; _save(t)
    before = {x['id'] for x in s['tracks']}
    a = {'id': None}
    if (folder / 'take.wav').exists():   # a combined take has no recording of its own
        a = adapter._request('mix_create_audio_track', {'name': base + ' audio', 'mute': False}, write=True)
        log.update(audioTrackId=a['id']); _save(t)
        log['audioClip'] = adapter.import_audio(a['id'], str(folder / 'take.wav'), start)
    log['stage'] = 'midi-track'
    name = (base + ' MIDI' + tag)[:60]
    adapter.create_midi_track(name, index=None, mute=False, arm=False)
    new = [x for x in adapter.session()['tracks'] if x['id'] not in before and x['id'] != a['id']]
    if len(new) != 1 or new[0]['name'] != name:
        log['stage'] = 'track-unidentified'; _save(t)
        raise ValueError('Could not identify the new MIDI track; stopped. Inspect Live.')
    m = new[0]; log.update(midiTrackId=m['id'])
    adapter.update_track(m['id'], gainDb=-12.0, arm=False)
    if instrument:
        adapter.load_device(m['id'], instrument, shelf='instruments'); log['instrument'] = instrument
    clip = adapter.create_arrangement_clip(m['trackIndex'], length, notes, start=start, name=name, looping=False)
    got = engine.clip_notes(adapter.clip(clip['id']), length)
    if not engine.notes_equal(got, notes):
        log['stage'] = 'readback-mismatch'; _save(t)
        raise ValueError('The MIDI clip did not read back identically. Tracks kept for inspection.')
    log.update(stage='placed', verified=True, midiClipId=clip['id'], notes=len(notes))
    t['status'] = 'placed'; t['placed'] = log; t.pop('placing', None); _save(t)
    return t


def summary(t):
    p = t['placed']
    what = 'original audio and %d MIDI notes on two new tracks' % p['notes'] if p.get('audioTrackId') else '%d MIDI notes on a new track' % p['notes']
    return ('Placed "%s": %s at bar %d (%s timing, take at %g bpm, Set %g bpm), read back note for note.'
            % (t['name'], what, p['bar'], p['mode'], t['tempo']['bpm'], p['setTempo']))


# ---- Combine: several hums of the same tune (separate takes, or repeats inside one take) -> one best guess ----
# Fixed, reported method; the source takes are never changed. Each version is aligned to the most typical one
# (Needleman-Wunsch on notes, one transposition per version, time mapped by a straight-line fit of matched onsets);
# a note is kept when at least half the versions have it, its pitch is the most common one (ties: the typical
# version), its timing the median. 'support' per note = share of versions that had it.

def _align(ref, other, shift):
    """Global alignment of two note lists [(p,t,d)] with `other` transposed by shift. Returns (score, pairs)."""
    n, m, gap = len(ref), len(other), -0.8
    S = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1): S[i][0] = i * gap
    for j in range(1, m + 1): S[0][j] = j * gap

    def sc(a, b):
        d = abs(a[0] - (b[0] + shift))
        return 2.0 if d == 0 else 0.5 if d == 1 else -1.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            S[i][j] = max(S[i - 1][j - 1] + sc(ref[i - 1], other[j - 1]), S[i - 1][j] + gap, S[i][j - 1] + gap)
    i, j, pairs = n, m, []
    while i and j:
        if S[i][j] == S[i - 1][j - 1] + sc(ref[i - 1], other[j - 1]):
            if sc(ref[i - 1], other[j - 1]) > 0: pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif S[i][j] == S[i - 1][j] + gap: i -= 1
        else: j -= 1
    return S[n][m], pairs[::-1]


def _best(ref, other, shifts=range(-24, 25)):
    return max((_align(ref, other, k) + (k,) for k in shifts), key=lambda x: x[0])


def consensus(versions, shifts=range(-24, 25)):
    """versions: list of [(p, t_beats, d_beats)] -> (notes [(p,t,d,support)], report)."""
    import statistics as st
    versions = [v for v in versions if v]
    if len(versions) < 2:
        raise ValueError('Need at least two versions to combine.')
    n = len(versions)
    total = [sum(_best(a, b, shifts)[0] for b in versions if b is not a) for a in versions]
    r = max(range(n), key=lambda i: total[i]); ref = versions[r]
    votes = [[(p, t, d)] for p, t, d in ref]; extra = []; used_shifts = []
    for k, v in enumerate(versions):
        if k == r: continue
        score, pairs, shift = _best(ref, v, shifts); used_shifts.append(shift)
        if len(pairs) >= 2:   # map this version's time onto the reference: t_ref = a*t + b
            xs, ys = [v[j][1] for _, j in pairs], [ref[i][1] for i, _ in pairs]
            mx, my = st.fmean(xs), st.fmean(ys); vx = sum((x - mx) ** 2 for x in xs)
            a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx > 1e-9 else 1.0
            a = a if 0.5 < a < 2 else 1.0; b = my - a * mx
        else:
            a, b = 1.0, (ref[0][1] - v[0][1])
        used = set()
        for i, j in pairs:
            p, t, d = v[j]; votes[i].append((p + shift, a * t + b, d * a)); used.add(j)
        extra += [(k, p + shift, a * t + b, d * a) for j, (p, t, d) in enumerate(v) if j not in used]
    need = n / 2.0
    out = []
    for (p0, _, _), vs in zip(ref, votes):
        if len(vs) < need: continue
        ps = [x[0] for x in vs]; best = max(set(ps), key=lambda q: (ps.count(q), q == p0))
        out.append((best, round(st.median(x[1] for x in vs), 4), round(st.median(x[2] for x in vs), 4), round(len(vs) / n, 2)))
    # notes the typical version missed but most others agree on (same pitch, within 0.35 beat)
    extra.sort(key=lambda x: x[2]); taken = set()
    for i, (k, p, t, d) in enumerate(extra):
        if i in taken: continue
        group = [(k, p, t, d)] + [e for j, e in enumerate(extra) if j > i and j not in taken and e[1] == p and abs(e[2] - t) < 0.35 and e[0] != k]
        if len({g[0] for g in group}) >= need:
            taken.update(j for j, e in enumerate(extra) if e in group)
            out.append((p, round(st.median(g[2] for g in group), 4), round(st.median(g[3] for g in group), 4), round(len(group) / n, 2)))
    out.sort(key=lambda x: x[1])
    return out, {'versions': n, 'typicalVersion': r, 'transpositions': used_shifts}


def repeats(notes_beats):
    """Find a repeating phrase inside one take: the period (whole beats, 2-16) at which the most notes recur with the
    same pitch about one period later. Returns (period, [version note lists]) or (None, [])."""
    nb = [(n['p'], n['t'], n['d']) for n in notes_beats]
    if len(nb) < 4: return None, []
    end = max(t for _, t, _ in nb)
    scores = {}
    for P in range(2, 17):
        if end < P * 1.5: break
        cand = [(p, t) for p, t, _ in nb if t + P <= end + 0.5]
        hit = sum(any(q == p and abs(u - (t + P)) < 0.35 for q, u, _ in nb) for p, t in cand)
        scores[P] = hit / max(1, len(cand))
    if not scores or max(scores.values()) < 0.3: return None, []
    top = max(scores.values()); P = min(k for k, s in scores.items() if s >= 0.85 * top)
    t0 = __import__('math').floor(min(t for _, t, _ in nb))
    segs = {}
    for p, t, d in nb:
        segs.setdefault(int((t - t0) // P), []).append((p, round((t - t0) % P, 4), d))
    vs = [segs[k] for k in sorted(segs) if len(segs[k]) >= 2]
    return (P, vs) if len(vs) >= 2 else (None, [])


def combine(ids, name=''):
    """Make a new take from several takes of the same tune, or from the repeats inside one take."""
    srcs = [load(i) for i in ids]
    if not srcs: raise ValueError('Choose takes to combine.')
    if len(srcs) == 1:
        period, versions = repeats(beats(srcs[0], 'pulse'))
        if not period:
            raise ValueError('No repeating phrase found in this take. Hum the tune in separate takes and combine those.')
        how = 'repeats of a %d-beat phrase inside one take' % period
    else:
        versions = [[(n['p'], n['t'], n['d']) for n in beats(s, 'pulse')] for s in srcs]
        how = '%d separate takes' % len(srcs)
    notes, rep = consensus(versions, (0, -12, 12) if len(srcs) == 1 else range(-24, 25))   # one take: same key, octave slips only
    ref = srcs[0] if len(srcs) == 1 else srcs[rep['typicalVersion']]
    bpm, ph = ref['tempo']['bpm'], ref['tempo']['firstBeatSeconds']; spb = 60.0 / bpm
    secs = [{'p': p, 's': round(ph + t * spb, 4), 'e': round(ph + (t + max(d, 0.125)) * spb, 4), 'v': 90, 'support': s}
            for p, t, d, s in notes]
    tid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)
    (INBOX / tid).mkdir(parents=True)
    take = {'id': tid, 'name': (str(name).strip() or 'Combined ' + ref['name'])[:40], 'by': ref.get('by', ''),
            'created': datetime.now(timezone.utc).isoformat(), 'status': 'phone', 'kind': 'combined',
            'seconds': round(max([n['e'] for n in secs] + [0]), 2), 'notes': secs,
            'tempo': {'bpm': bpm, 'source': ref['tempo']['source'], 'firstBeatSeconds': ph, 'onsetsOnEighthGrid': None},
            'combined': {'from': [s['id'] for s in srcs], 'method': how, **rep,
                         'meanSupport': round(sum(n['support'] for n in secs) / max(1, len(secs)), 2)}}
    _save(take)
    return take
