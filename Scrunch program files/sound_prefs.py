"""What the journal has taught Scrunch about this producer: how far they push a move, which
knobs they keep, and the words they use for it.

Only deliberate signals count. Keeping a move is strong evidence; turning it around ("wrong
direction") is strong evidence against those knobs. A bare revert is weak: it may mean "not
that", but it is also what a test or a change of mind looks like, so it only counts when the
producer steered first. Nothing is learned from a single event: every preference needs at
least MIN_EVIDENCE occurrences, so one odd session cannot bend the engine.

Preferences never introduce a knob the patch does not justify. They re-rank and re-scale what
`sound_moves` already proposes for the patch in front of them.
"""
import json,math
from pathlib import Path

SOURCES=('user','test')     # every journal entry declares which it is
LEARNS_FROM='user'          # and only one of them teaches

MIN_EVIDENCE=2          # occurrences before a preference is applied
RECENCY_HALFLIFE=40     # entries; older evidence counts less
LEVER_BONUS=.35         # most a learned knob preference can add to a lever's score
MAX_ENTRIES=5000

def read(path):
    """Journal entries, oldest first; unreadable lines are skipped rather than failing."""
    p=Path(path)
    if not p.is_file():return []
    out=[]
    for line in p.read_text(errors='replace').splitlines()[-MAX_ENTRIES:]:
        try:
            entry=json.loads(line)
            if isinstance(entry,dict):out.append(entry)
        except ValueError:continue
    return out

def _weight(index,total):
    return .5**((total-1-index)/RECENCY_HALFLIFE)

def _steered(entry):
    """True when the producer pushed the move around before deciding, rather than dropping it at once."""
    amounts={round(float(step.get('amount',0)),3) for step in (entry.get('trajectory') or [])}
    return len(amounts)>2

def teaches(entry):
    """Only an entry that says it came from the producer teaches anything.

    This is deliberately fail-closed. It used to read `source` defaulting to `user`, so an entry
    that simply never got labelled counted as the producer's own taste - which is how Scrunch's
    automated runs got into what it had "learned" the first time. An unlabelled entry is now
    treated as unknown provenance and teaches nothing: a test that forgets to say so is silent
    rather than persuasive. 71 unlabelled entries were sitting in the quarantined test journals
    when this was tightened.
    """
    return isinstance(entry,dict) and entry.get('source')==LEARNS_FROM

def learn(entries):
    """Preferences from a journal: {amounts, knobs, phrases, evidence}.

    Only entries labelled `source: user` teach. Automated runs (`source: test`) and anything
    unlabelled are ignored - see `teaches`.
    """
    entries=[e for e in entries if teaches(e)]
    total=len(entries)
    amounts={};knobs={};phrases={};kept={}
    for i,e in enumerate(entries):
        move=e.get('move');kind=e.get('kind')
        if not move:continue
        w=_weight(i,total)
        settings=[s.get('setting') for s in (e.get('steps') or []) if s.get('setting')]
        if kind=='move':                                   # kept: the amount they settled at, and the knobs used
            amounts.setdefault(move,[]).append((float(e.get('amount') or 0),w))
            kept[move]=kept.get(move,0)+1
            for s in settings:knobs.setdefault(move,{}).setdefault(s,0.);knobs[move][s]+=w
            words=str(e.get('words') or '').strip().lower()
            if words and len(words)<60:
                seen=phrases.setdefault(words,{}).setdefault(move,[0,0.])
                seen[0]+=1;seen[1]+=w
        elif kind=='wrong-direction':                      # they said it went the wrong way: count against those knobs
            for s in settings:knobs.setdefault(move,{}).setdefault(s,0.);knobs[move][s]-=w
        elif kind=='revert' and _steered(e):               # steered, then dropped it: mild evidence against
            for s in settings:knobs.setdefault(move,{}).setdefault(s,0.);knobs[move][s]-=w*.5
    out_amounts={}
    for move,pairs in amounts.items():
        if len(pairs)<MIN_EVIDENCE:continue
        weight=sum(w for _,w in pairs) or 1
        out_amounts[move]=round(sum(a*w for a,w in pairs)/weight,3)
    out_knobs={m:{s:round(v,3) for s,v in d.items() if abs(v)>=.5} for m,d in knobs.items()}
    out_knobs={m:d for m,d in out_knobs.items() if d}
    out_phrases={}
    for words,moves in phrases.items():
        move,(count,_)=max(moves.items(),key=lambda kv:kv[1][1])
        if count>=MIN_EVIDENCE and len(moves)==1:out_phrases[words]=move   # said more than once, and never for another move
    return {'amounts':out_amounts,'knobs':out_knobs,'phrases':out_phrases,
            'evidence':{'entries':total,'keptByMove':kept}}

def preferred_amount(prefs,move,default=.5):
    return float((prefs or {}).get('amounts',{}).get(move,default))

def lever_bonus(prefs,move):
    """{setting: bonus} to add to a lever's score, bounded so the patch still decides."""
    scores=(prefs or {}).get('knobs',{}).get(move,{})
    if not scores:return {}
    peak=max(abs(v) for v in scores.values()) or 1
    return {s:LEVER_BONUS*max(-1.,min(1.,v/peak)) for s,v in scores.items()}

def phrase_move(prefs,words):
    """A move this producer's own wording has meant before, or None."""
    text=' '.join(str(words).lower().split())
    return (prefs or {}).get('phrases',{}).get(text)

def summary(prefs):
    """One line per thing learned, for showing the producer what it has picked up."""
    lines=[]
    for move,amount in sorted((prefs or {}).get('amounts',{}).items()):
        lines.append('%s: you usually settle around %d%%'%(move,round(amount*100)))
    for move,scores in sorted((prefs or {}).get('knobs',{}).items()):
        liked=[s for s,v in sorted(scores.items(),key=lambda kv:-kv[1]) if v>0][:3]
        disliked=[s for s,v in sorted(scores.items(),key=lambda kv:kv[1]) if v<0][:2]
        if liked:lines.append('%s: you keep %s'%(move,', '.join(liked)))
        if disliked:lines.append('%s: you have turned down %s'%(move,', '.join(disliked)))
    for words,move in sorted((prefs or {}).get('phrases',{}).items()):
        lines.append('"%s" has meant %s'%(words,move))
    return lines
