"""Evidence log for generated MIDI takes: Accept / Reject / Neither (master spec §43.5, §93).

Append-only JSONL. Every generated take is recorded with its intent, candidate id, seed and
settings, source phrase, output notes and validation result. Ableton placement readback and the
producer's choice are later events referring to the take, so nothing is ever rewritten and every
decision stays auditable. `takes()` folds the events into the current view of each take.

This is future adaptation data: accepted, rejected and neither choices are kept with who made them
(`source`: user, benchmark, software-test), so software tests never pass for musical approvals.
"""
import datetime, fcntl, json, os, uuid
from pathlib import Path

import music

CHOICES = ('accept', 'reject', 'neither')
SOURCES = ('user', 'benchmark', 'software-test')


def default_path():
    return Path(os.environ.get('SCRUNCH_DATA', str(Path(__file__).resolve().parent / 'data'))) / 'midi-takes.jsonl'


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class TakeLog:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, event):
        event = {'schema': 1, 'at': _now(), **event}
        line = json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n'
        with open(self.path, 'a', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line); f.flush(); os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return event

    def events(self):
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        raise ValueError('midi-takes.jsonl line %d is damaged; it was not rewritten.' % n) from None
        return out

    def record_take(self, intent, candidate, notes, length, seed=None, settings=None, source=None,
                    task=None, origin='user', latency=None, compute=None):
        """Record a generated take. Invalid notes are recorded too, with the reason, never dropped."""
        if origin not in SOURCES:
            raise ValueError('origin must be one of %s.' % ', '.join(SOURCES))
        try:
            clean = music.validate(notes, length); validation = {'valid': True, 'error': None}
        except (ValueError, TypeError) as e:
            clean = notes; validation = {'valid': False, 'error': str(e)}
        take = {'kind': 'take', 'take': uuid.uuid4().hex, 'intent': str(intent or ''), 'candidate': str(candidate),
                'seed': seed, 'settings': settings or {}, 'task': task, 'origin': origin,
                'source': source, 'length': length, 'notes': clean, 'validation': validation,
                'fingerprint': music.fingerprint(clean) if validation['valid'] else None,
                'latency': latency, 'compute': compute}
        return self._append(take)['take']

    def _known(self, take_id):
        if not any(e.get('kind') == 'take' and e['take'] == take_id for e in self.events()):
            raise KeyError('No logged take %r.' % take_id)

    def record_placement(self, take_id, readback):
        """Ableton placement readback: track/clip ids, position and the note readback comparison."""
        self._known(take_id)
        return self._append({'kind': 'placement', 'take': take_id, 'readback': readback})

    def record_decision(self, take_id, choice, note='', source='user'):
        if choice not in CHOICES:
            raise ValueError('A decision is accept, reject or neither.')
        if source not in SOURCES:
            raise ValueError('source must be one of %s.' % ', '.join(SOURCES))
        self._known(take_id)
        return self._append({'kind': 'decision', 'take': take_id, 'choice': choice, 'note': str(note or ''), 'by': source})

    def record_comparison(self, a, b, choice, note='', source='user', sheet=None):
        """A blind A/B listen: choice is 'A', 'B' or 'neither' between two logged takes."""
        if choice not in ('A', 'B', 'neither'):
            raise ValueError('A comparison is A, B or neither.')
        self._known(a); self._known(b)
        return self._append({'kind': 'comparison', 'a': a, 'b': b, 'choice': choice, 'note': str(note or ''),
                             'by': source, 'sheet': sheet})

    def takes(self):
        """Current view of every take: its record, placements, decisions (latest last) and comparisons."""
        view = {}
        for e in self.events():
            k = e.get('kind')
            if k == 'take':
                view[e['take']] = {**e, 'placements': [], 'decisions': [], 'comparisons': []}
            elif k in ('placement', 'decision') and e['take'] in view:
                view[e['take']][k + 's'].append(e)
            elif k == 'comparison':
                for side in ('a', 'b'):
                    if e[side] in view:
                        view[e[side]]['comparisons'].append(e)
        for t in view.values():
            t['choice'] = t['decisions'][-1]['choice'] if t['decisions'] else None
        return view
