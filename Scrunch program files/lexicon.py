"""The producer's own words, from the one memory store (build spec step 4 and section 5).

Voice transcription and the engineer read the same vocabulary records in taste_memory: pronunciation
records ("heard" -> what he says) resolve speech-recognition slips; meaning records are evidence of what a
word did on a given synth, with context and confidence, never a word-to-control recipe. A word he kept a
change for is learned as an unconfirmed meaning; he can inspect, correct or forget every record.
"""
import difflib
import re
import time

import acoustic_terms
import taste_memory

WORD = re.compile(r"[a-z][a-z' -]{0,38}[a-z]")      # one word or a short phrase; nothing else is learned

def _store(store=None):
    return store or taste_memory.MemoryStore()


def words(store=None):
    """Every term in his lexicon, with the mishearings known for it."""
    s = _store(store)
    out = {}
    for r in s.vocabulary('pronunciation'):
        out.setdefault(r['term'], set())
        if r['heard'] != r['term']:
            out[r['term']].add(r['heard'])
    for r in s.vocabulary('meaning'):
        out.setdefault(r['term'], set())
    return out


def for_voice(store=None):
    """The sentence the transcriber gets about his words; '' when none are known."""
    try:
        known = words(store)
    except Exception:
        return ''
    if not known:
        return ''
    parts = []
    for term, heard in sorted(known.items()):
        parts.append('"%s"%s' % (term, (' (not "%s" unless he clearly says it)' % '", "'.join(sorted(heard))) if heard else ''))
    return 'Words this producer uses: ' + ', '.join(parts) + '. Use them to resolve homophones. '


def for_engineer(text, store=None, limit=40):
    """His lexicon as the engineer sees it: which words are his, what they did before and how sure that is."""
    s = _store(store)
    known = words(s)
    meanings = {}
    for r in s.vocabulary('meaning'):
        meanings.setdefault(r['term'], []).append({'did': r['interpretation'], 'context': r['context'],
                                                   'confidence': r['confidence'], 'status': r['status']})
    lowered = str(text).lower()
    rows = [{'term': t, 'misheardAs': sorted(h), 'meanings': meanings.get(t, [])[-5:], 'inThisMessage': t.lower() in lowered}
            for t, h in sorted(known.items())]
    rows.sort(key=lambda r: not r['inThisMessage'])
    return {'note': 'Words already his: act on them without asking. Meanings are what they did before on a given '
                    'synth, not rules. A word not listed here is new: follow the asking rules.', 'words': rows[:limit]}


def _says(text, word):
    """The word (or short phrase) appears in the text as whole words."""
    return re.search(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])', str(text).lower()) is not None


NEGATION = {'no', 'not', 'nope', 'neither', 'nor', 'wrong', "don't", 'dont', "didn't", 'didnt', 'never'}
FILLER = {'i', 'said', 'meant', 'mean', 'it', "it's", 'its', 'was', 'is', 'yes', 'yeah', 'yep', 'the', 'word', 'say',
          'make', 'more', 'a', 'bit', 'please', 'that', 'one', 'like'}
STOP = {'make', 'it', 'the', 'this', 'that', 'more', 'less', 'bit', 'some', 'with', 'just', 'like', 'sound', 'again'}


def _plain(word):
    """A word that could be a speech-to-text slip: one or two words, at least 4 letters, not an everyday word."""
    parts = word.split()
    return bool(WORD.fullmatch(word)) and len(parts) <= 2 and len(word.replace(' ', '')) >= 4 \
        and not any(p in STOP for p in parts)


def confirmed_mishearing(decision, history, text, store=None):
    """He has just answered the engineer's question about a word written wrongly: (meant, heard), or None.
    The engineer only proposes the pair; it is kept only when all of these hold:
    - the engineer's last line was a question naming the written word or offering the meant one;
    - the written word is in what he said just before that question;
    - his answer is essentially just the meant word, with no "no" or "not";
    - the two words look like a transcription slip of each other (e.g. transit / trancier);
    - the written word is not already one of his words, already stands for another word, or a sound term."""
    pair = decision.get('misheard') if isinstance(decision, dict) else None
    if not pair or len(history or []) < 2:
        return None
    heard, meant = pair
    if heard == meant or not _plain(heard) or not _plain(meant):
        return None
    asked, before = history[-1], history[-2]
    question = str(asked.get('text', '')).split('\n')[0].rstrip()   # notes added after a reply are not the question
    if asked.get('who') != 'engineer' or before.get('who') != 'producer' or not question.endswith('?'):
        return None
    if not (_says(question, heard) or _says(question, meant)) or not _says(before.get('text', ''), heard):
        return None
    said = re.findall(r"[a-z']+", str(text).lower())
    if any(w in NEGATION for w in said) or not _says(text, meant):
        return None
    rest = [w for w in said if w not in FILLER and w not in meant.split()]
    if rest:
        return None                                   # he said more than the word: let the engineer ask again
    if difflib.SequenceMatcher(None, heard.replace(' ', ''), meant.replace(' ', '')).ratio() < .5:
        return None
    if heard in acoustic_terms.MEASURES:
        return None
    known = words(store)
    if heard in known or any(heard in h for t, h in known.items() if t != meant) or heard in known.get(meant, set()) \
            or meant in known.get(heard, set()):
        return None
    return meant, heard


def learn_mishearing(term, heard, event_id, store=None):
    """He said which word he meant: speech-to-text writing `heard` means `term` from now on. Returns the
    record id, or None when it was already known."""
    s = _store(store)
    if heard in words(s).get(term, set()):
        return None
    item = {'id': 'pron:%s:%s' % (term, heard), 'kind': 'pronunciation', 'term': term, 'heard': heard,
            'pronunciation': term, 'context': {'source': "his answer to the engineer's question", 'at': time.time()},
            'confidence': 1.0, 'status': 'user_confirmed', 'evidence_ids': [event_id]}
    return item['id'] if s.record_vocabulary(item) else None


def learn_from_keep(words_said, event_id, plugin, changes, store=None):
    """He kept a change made for these words: keep that as an unconfirmed meaning, in its context."""
    phrase = ' '.join(str(words_said).split())[:80]
    if not phrase or not changes or not event_id:
        return None
    s = _store(store)
    summary = '; '.join('%s %s' % (c.get('label') or c.get('control_id'), c.get('direction') or ('%s to %s' % (c.get('was'), c.get('now'))))
                        for c in changes[:6])
    item = {'id': 'meaning:%s' % event_id, 'kind': 'meaning', 'term': phrase.lower(),
            'interpretation': 'kept after: ' + summary, 'context': {'plugin': plugin or 'unknown', 'at': time.time()},
            'confidence': .5, 'status': 'ai_interpretation_unconfirmed', 'evidence_ids': [event_id]}
    s.record_vocabulary(item)
    return item['id']
