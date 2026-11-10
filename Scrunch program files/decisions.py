"""Keep / reject / neither, recorded as he decides (build spec section 7, step 7).

Every verified change the engineer makes for his words is a case; what he does next is its outcome:
keep (he keeps it), reject (he has it put back), neither (neither version). Only his own messages
(source=user) count; test and evaluation runs never become his decisions. Records follow
schemas/keep-reject-decision.json and go to data/decisions.jsonl, one per line, so a baseline and any
later model comparison are scored from what he actually kept, not from a descriptor number.
"""
import json
import time
import uuid

import engine
from schemas import validate

NEITHER = ('neither', 'neither of them', 'neither one', 'none of them')


def path():
    return engine.DATA / 'decisions.jsonl'


def remember_change(c, request_id, result, move_id=None, audition=False):
    """The change a later decision is about."""
    c.decision_case = {'request_id': str(request_id), 'result_id': result['result_id'], 'move_id': move_id,
                       'auditioned': True, 'audition': bool(audition), 'at': time.time()}


def record(c, decision, words, request_id, source, reason=None):
    """Write his decision about the engineer's last change. Returns the record, or None when there is
    no verified change of the engineer's to decide about or the message is not his."""
    case = getattr(c, 'decision_case', None)
    if source != 'user' or not isinstance(case, dict):
        return None
    item = {'schema_version': 1, 'decision_id': str(uuid.uuid4()), 'request_id': case['request_id'],
            'result_id': case['result_id'], 'move_id': case['move_id'], 'decision': decision, 'source': 'user',
            'user_words': str(words)[:500], 'evidence_ids': ['sound-chat:' + str(request_id)],
            'auditioned': case['auditioned'], 'reason': reason,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    validate('keep-reject-decision', item)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a') as f:
        f.write(json.dumps(item) + '\n')
    c.decision_case = None                      # one decision per change
    return item


def load():
    p = path()
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def summary():
    """Keep rate over his decided changes so far (decisions only; see eval/ for the full baseline score)."""
    rows = load()
    n = len(rows)
    count = {k: sum(r['decision'] == k for r in rows) for k in ('keep', 'reject', 'neither')}
    return {'decided': n, **count, 'keepRate': round(count['keep'] / n, 3) if n else None}
