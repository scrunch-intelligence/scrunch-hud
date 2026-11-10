"""Reference-track map in Live: analyse a warped Arrangement audio clip (tools/reference_structure.py: All-In-One bars +
stem kick/bass rules) and add a named locator at each section start - lead-in, breakdown, build-up, first/main drop,
groove, mix-out, dropouts. Positions go from seconds in the file to Arrangement beats through the clip's own warp
markers (audio_to_midi.Warp/segments), so they land where that moment plays. Existing locators are never renamed or
removed. The analysis is cached per file (path, size, modified time) in data/reference-structure/."""
import hashlib, json, subprocess
from pathlib import Path

import audio_to_midi

ROOT = Path(__file__).resolve().parent
PY = ROOT / '.venv-structure/bin/python'
TOOL = ROOT / 'tools/reference_structure.py'
CACHE = ROOT / 'data/reference-structure'


def analyse(path):
    p = Path(path); st = p.stat()
    key = hashlib.sha1(f'{p.resolve()}|{st.st_size}|{st.st_mtime_ns}'.encode()).hexdigest()[:16]
    CACHE.mkdir(parents=True, exist_ok=True)
    hit = CACHE / f'{key}.json'
    if hit.exists():
        return json.loads(hit.read_text())
    if not PY.exists():
        raise ValueError('The reference analyser is not installed (.venv-structure). Nothing was changed.')
    out = CACHE / f'run-{key}'
    subprocess.run([str(PY), str(TOOL), str(p), '--out', str(out)], check=True, capture_output=True, timeout=900)
    r = json.loads((out / (p.stem + '.json')).read_text())
    r.pop('perBar', None)   # the cache keeps the sections; per-bar detail stays in the run folder
    hit.write_text(json.dumps(r, indent=1))
    return r


def arrangement_beat(clip, warp, segs, seconds):
    b = warp.beat(seconds)
    for at, lo, hi in segs:
        if lo - 1e-6 <= b < hi:
            return round(float(clip['start']) + at + b - lo, 4)
    at, lo, hi = segs[0]
    if lo - 8 <= b < lo:   # starts up to 2 bars before the clip's start marker (Live trims leading silence): mark the clip start
        return round(float(clip['start']) + at, 4)
    return None   # that moment is outside the part of the file the clip plays


def plan(clip, r):
    warp, segs = audio_to_midi.Warp(clip.get('warpMarkers')), audio_to_midi.segments(clip)
    marks = [(s['startSeconds'], s['label']) for s in r['sections']] + [(d['startSeconds'], 'dropout') for d in r['dropouts']]
    items, skipped = [], []
    for sec, label in sorted(marks):
        beat = arrangement_beat(clip, warp, segs, sec)
        name = 'Ref ' + label[0].upper() + label[1:]
        (items.append({'beat': beat, 'name': name}) if beat is not None else skipped.append(name))
    return items, skipped


def mark(track_id, clip_id='', adapter=None):
    adapter = adapter or audio_to_midi.engine.host()
    track, clip = audio_to_midi.find_clip(adapter, track_id, clip_id)
    audio_to_midi.check_source(track, clip)
    r = analyse(clip['filePath'])
    items, skipped = plan(clip, r)
    done = adapter.add_locators(items)
    return {'track': track['name'], 'clip': clip['name'], 'bpm': r['bpm'], 'sections': r['sections'], 'dropouts': r['dropouts'],
            'locators': items, 'added': done.get('added', []), 'alreadyThere': done.get('alreadyThere', []), 'outsideClip': skipped}


def summary(m):
    main = [s for s in m['sections'] if s['label'] == 'main drop']
    lines = ['Marked %s (%s, about %s BPM): %d locators added%s.' % (
        m['clip'], m['track'], m['bpm'], len(m['added']),
        ', %d already there and left alone' % len(m['alreadyThere']) if m['alreadyThere'] else '')]
    lines.append('Sections: ' + '; '.join('%s bars %s' % (s['label'], s['bars']) for s in m['sections']) + '.')
    if main: lines.append('Main drop at %s in the track.' % main[0]['start'])
    if m['outsideClip']: lines.append('Not marked (outside the part of the file the clip plays): ' + ', '.join(m['outsideClip']) + '.')
    lines.append('Bars are counted from the start of the reference file, not the Set.')
    return ' '.join(lines)
