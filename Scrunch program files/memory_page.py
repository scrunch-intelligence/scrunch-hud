"""The producer's view of his memory (build spec section 5, required product UI): see what Scrunch
keeps, correct his words, forget any record, export and restore a backup. Everything here reads and
writes the one taste_memory store; nothing is hidden in a second place.
"""
import time
import uuid
from pathlib import Path

import decisions
import lexicon
import taste_memory

BACKUPS = Path.home() / 'Documents' / 'Scrunch Memory Backups'


def _store():
    return taste_memory.MemoryStore()


def view(limit=150):
    s = _store()
    events = []
    for row in s.list_events(limit):
        e = row['event']
        events.append({'id': row['id'], 'at': e.get('at'), 'kind': e.get('kind'), 'words': e.get('words') or e.get('instruction'),
                       'plugin': e.get('plugin'), 'reason': e.get('ai_rationale')})
    words = [{'id': r['id'], 'kind': 'pronunciation', 'term': r['term'], 'detail': 'his word' if r['heard'] == r['term'] else 'speech-to-text may write "%s"' % r['heard'],
              'status': r['status'], 'confidence': r['confidence'], 'evidence': r['evidence_ids']}
             for r in s.vocabulary('pronunciation')]
    words += [{'id': r['id'], 'kind': 'meaning', 'term': r['term'], 'detail': r['interpretation'], 'status': r['status'],
               'confidence': r['confidence'], 'context': r['context'], 'evidence': r['evidence_ids']}
              for r in s.vocabulary('meaning')]
    return {'profile': s.user_id() if callable(getattr(s, 'user_id', None)) else None, 'events': events, 'words': words,
            'moves': len(s.moves()), 'decisions': decisions.summary()}


def forget(body):
    ident = str(body.get('id') or '')
    if not ident:
        raise ValueError('Choose a record to forget.')
    s = _store()
    if body.get('kind') == 'event':
        s.forget(ident)                     # and everything derived from it
    else:
        s.forget_derived(ident)             # the interpretation only; his original words stay
    return {'forgotten': ident}


def add_word(body):
    """His own correction: 'this word is mine' (and what it gets misheard as)."""
    term = ' '.join(str(body.get('term') or '').split())[:60]
    heard = ' '.join(str(body.get('heard') or term).split())[:60]
    if not term:
        raise ValueError('Type the word as you say it.')
    s = _store()
    event_id = 'lexicon-edit:' + uuid.uuid4().hex
    s.record({'eventId': event_id, 'source': 'user', 'at': time.time(), 'kind': 'vocabulary-edit',
              'words': 'My word: %s%s' % (term, (' (sometimes written "%s")' % heard) if heard != term else '')})
    s.record_vocabulary({'id': 'pron:%s:%s:%s' % (term.lower(), heard.lower(), event_id[-6:]), 'kind': 'pronunciation',
                         'term': term.lower(), 'heard': heard.lower(), 'pronunciation': term.lower(),
                         'context': {'source': 'added by him on the memory page'}, 'confidence': 1.0,
                         'status': 'user_confirmed', 'evidence_ids': [event_id]})
    return {'added': term, 'voice': lexicon.for_voice(s)}


def export():
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / ('scrunch-memory-%s.zip' % time.strftime('%Y%m%d-%H%M%S'))
    _store().backup(dest)
    return {'path': str(dest)}


def restore(body):
    p = Path(str(body.get('path') or '')).expanduser()
    if not p.is_file() or p.suffix != '.zip':
        raise ValueError('Choose a Scrunch memory backup (.zip).')
    _store().restore(p)
    return {'restored': str(p)}


def usage(days=14):
    """AI use per day from the engineer log: calls, tokens in and out (thinking included), failures.
    Tokens are what the provider reported; no price is guessed here."""
    import collections, datetime, engine
    rows = collections.OrderedDict()
    for m in engine.STATE.get('engineerChat', []):
        if m.get('role') != 'assistant':
            continue
        at = m.get('at') or (m.get('timings') or {}).get('at')
        day = datetime.date.fromtimestamp(at).isoformat() if at else 'undated'
        r = rows.setdefault(day, {'day': day, 'requests': 0, 'modelCalls': 0, 'tokensIn': 0, 'tokensOut': 0, 'failed': 0})
        r['requests'] += 1
        r['failed'] += bool(m.get('failed'))
        for u in m.get('modelUsage') or []:
            if not isinstance(u, dict):
                continue
            r['modelCalls'] += 1
            r['tokensIn'] += int(u.get('promptTokenCount') or u.get('input_tokens') or u.get('prompt_tokens') or 0)
            r['tokensOut'] += int((u.get('candidatesTokenCount') or u.get('output_tokens') or u.get('completion_tokens') or 0)
                                  + (u.get('thoughtsTokenCount') or 0))
    out = list(rows.values())
    return {'days': out[-days:], 'note': 'Tokens as reported by the AI provider. Check the provider console for money spent.'}
