"""Level-matched changes: after a sound change, the track fader makes up the loudness the change added or took
away, so he judges the tone, not the volume (24 Sep: 'more' on a pluck only sounded quieter).

Measured, not guessed: the before/after loudness is the engineer's own listen (EBU R128 at the synth tap, before
the fader), and the fader is set in exact dB by the bridge. The trim is part of the change: putting the change back
puts the fader back. A request that is ABOUT level (louder, quieter, volume ...) is never compensated.
"""
import re

LEVEL_WORDS = re.compile(r"\b(loud(er|ness)?|quiet(er)?|soft(er)?|volume|level|gain|fader|turn (it )?(up|down)|"
                         r"boost|attenuate|lower it|raise it)\b", re.I)
TOLERANCE_DB = .5          # below this the two already sound level
MAX_DB = 6.                # the Live fader's top
PEAK_CEILING_DB = -1.      # a make-up boost never pushes the measured peak above this


def intended(text):
    """He asked about level itself: the loudness change is the point, so it is left alone."""
    return bool(LEVEL_WORDS.search(text or ''))


def gap(before, after):
    """After minus before, in LU, from two listens; None when either was too quiet to measure."""
    b, a = (before or {}).get('loudnessLUFS'), (after or {}).get('loudnessLUFS')
    if b is None or a is None:
        return None
    return a - b


def match(c, before, after, text=''):
    """Set the synth's track fader so the changed sound plays as loud as the sound before the request.
    Returns what it did (for the reply and the record) or None.

    c.move['levelBase'] is the loudness and fader before the first change of a chain; 'more' / 'less' carry it
    over (carry()), so every step is matched to the ORIGINAL sound, never to the previous step. c.move['levelTrim']
    ['from'] is the fader when THIS change started: putting this change back restores exactly that."""
    if intended(text) or not getattr(c, 'move', None):
        return None
    now = c.a.track(c.track, mixer=True).get('gainDb')
    if now is None:
        return {'matched': False, 'gapDb': None, 'note': 'the track fader could not be read'}
    base = c.move.get('levelBase')
    if base is None:
        if (before or {}).get('loudnessLUFS') is None:
            return None
        base = c.move['levelBase'] = {'lufs': before['loudnessLUFS'], 'fader': now}
    after_lufs = (after or {}).get('loudnessLUFS')
    if after_lufs is None:
        return None
    g = after_lufs - base['lufs']
    trim = c.move.get('levelTrim') or {'track': c.track, 'from': now}
    want = base['fader'] - g
    if abs(want - now) < .05 or abs(g) <= TOLERANCE_DB and abs(now - base['fader']) < .05:
        return None
    peak = (after or {}).get('peakDbFS')
    if want > base['fader'] and peak is not None:
        want = min(want, base['fader'] + max(0., PEAK_CEILING_DB - peak))    # never boost into clipping
    want = max(-70., min(MAX_DB, want))
    c.a.update_track(c.track, gainDb=round(want, 2))
    c.move['levelTrim'] = dict(trim, to=round(want, 2))
    left = g + (want - base['fader'])
    return {'matched': abs(left) <= TOLERANCE_DB, 'gapDb': round(g, 2), 'faderDb': round(want, 2),
            'remainingDb': round(left, 2), **({} if abs(left) <= TOLERANCE_DB else {'note': 'no headroom on the fader'})}


def carry(previous, move):
    """'More' / 'less' make a new change from the last one: it keeps the original loudness to match against."""
    if previous and move is not previous and previous.get('levelBase') and move is not None:
        move['levelBase'] = previous['levelBase']


def undo(c, move=None):
    """Put the fader back where the change found it (called whenever the change is put back)."""
    trim = (move or getattr(c, 'move', None) or {}).get('levelTrim')
    if not trim:
        return None
    c.a.update_track(trim['track'], gainDb=trim['from'])
    return trim['from']


def english(done):
    if not done:
        return ''
    if done.get('gapDb') is None:
        return 'Not level-matched: %s.' % (done.get('note') or 'the level could not be measured')
    if done.get('matched'):
        return 'Level-matched: the change was %.1f dB %s, so the fader makes up the difference.' % (
            abs(done['gapDb']), 'louder' if done['gapDb'] > 0 else 'quieter')
    return 'Not level-matched: the change is %.1f dB %s (%s).' % (
        abs(done['gapDb']), 'louder' if done['gapDb'] > 0 else 'quieter', done.get('note') or
        '%.1f dB still left' % abs(done.get('remainingDb') or 0))
