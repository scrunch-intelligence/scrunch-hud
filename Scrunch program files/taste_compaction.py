"""Summarise the producer's recorded sound decisions with the engineer's own model.

Astra's `taste_memory` keeps every exchange losslessly and retrieves the relevant ones; its `compact`
turns a batch of up to 24 records into one source-linked summary. This module is the authorised
compactor: it calls the engineer provider John chose, once per batch, in the background after every
24 of his own (source=user) exchanges, so no reply waits for it. Test turns never trigger it.
"""
import threading, time

BATCH = 24
SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'text': {'type': 'string', 'description': 'concise summary, at most 1600 characters'},
    'evidence_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'every record id supplied'}},
    'required': ['text', 'evidence_ids']}
STATUS = {'pending': 0, 'running': False, 'last': None}
_LOCK = threading.Lock()


def compactor(profile, call=None):
    """The callback taste_memory.compact expects: payload -> {text, evidence_ids, usage}."""
    def run(payload):
        import providers
        reply, usage = (call or providers.structured)(profile, payload['guidance'], {'records': payload['records']}, SCHEMA)
        return {'text': str(reply.get('text', ''))[:1600], 'evidence_ids': reply.get('evidence_ids'), 'usage': usage}
    return run


def compact_now(store=None, profile=None, call=None):
    """One compaction batch. Returns taste_memory's result (compacted count, model calls, usage)."""
    import taste_memory
    if profile is None:
        import engine, sound_language
        profile = sound_language.selected(engine.STATE)
    if profile is None:
        return {'compacted': 0, 'model_calls': 0, 'reason': 'no engineer model set up'}
    store = store or taste_memory.MemoryStore()
    return store.compact(compactor(profile, call), model=profile['model'])


def after_record(source):
    """Count John's exchanges; every BATCH of them, summarise in the background."""
    if source != 'user':
        return
    with _LOCK:
        STATUS['pending'] += 1
        if STATUS['pending'] < BATCH or STATUS['running']:
            return
        STATUS['pending'] = 0
        STATUS['running'] = True

    def work():
        try:
            STATUS['last'] = dict(compact_now(), at=time.time())
        except Exception as exc:          # evidence stays intact; the next batch tries again
            STATUS['last'] = {'error': str(exc)[:300], 'at': time.time()}
        finally:
            STATUS['running'] = False
    threading.Thread(target=work, name='taste-compaction', daemon=True).start()
