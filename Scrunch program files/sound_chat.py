"""Sound conversation, shared by typed, dictated and button requests.

Called under engine.LOCK. There is no word list: every typed or spoken message goes to the AI
engineer with the whole studio picture (the patch, what the Live Ears tap measured, what it did
last, the recent conversation and the producer's remembered taste), and the AI decides.

Buttons are explicit instructions, not words to interpret: Revert, Save and Keep preset act at
once with no model call, and are written into the conversation so the engineer knows about them.
"""
import threading
import time

import action_layer
import decisions
import engine
import lexicon
import audition_pair
import level_match
import match_reference
import ab_takes
import reference_target
import sound_audio
import preset_navigation
from text_safety import sanitize

try:
    import taste_memory
except ImportError:            # the memory module is optional; the engineer works without it
    taste_memory = None

CONVERSATION = 12              # recent sound messages the engineer sees
HEARD_MAX_AGE = 90             # seconds a measurement stays valid while no control has moved
BUTTONS = ('revert', 'save', 'keep-preset', 'stop', 'keep', 'more', 'less', 'play', 'original', 'candidate')


ENGINEER_ROUTES = ('sound-model', 'sound-button')


def conversation(log, rid, plugin=None):
    """Recent exchanges with this engineer about this synth. Replies from the old fixed-list engine are left
    out, so their stock sentences are never shown to the AI as its own way of talking; so are talks about
    another synth, which would otherwise read as replies to what it just did here."""
    ours = {m.get('requestId') for m in log
            if m.get('role') == 'assistant' and (m.get('timings') or {}).get('route') in ENGINEER_ROUTES
            and m.get('source') == 'user'                    # automated test turns are never his conversation
            and not m.get('failed')                          # a request that never got an answer is not pending work
            and (plugin is None or m.get('plugin') == plugin)}
    rows = [m for m in log if m.get('workspace') == 'sound' and m.get('requestId') in ours
            and m.get('requestId') != rid][-CONVERSATION:]
    return [{'who': 'producer' if m['role'] == 'user' else 'engineer',
             'text': str(m.get('text', ''))[:500]} for m in rows]


def summary(m):
    """The measurement in the form the engineer reads it."""
    env = m.get('envelope') or {}
    return {'loudnessLUFS': m.get('loudnessLUFS'), 'peakDbFS': m.get('peakDbFS'),
            'brightnessHz': m.get('brightnessHz'), 'highShare': m.get('highShare'),
            'widthShare': m.get('widthShare'),
            'notes': {k: env.get(k) for k in ('notes', 'attackMs', 'decayMs', 'sustainDb', 'holdRatio') if k in env},
            'bands': {b['name']: b['energyDbFS'] for b in m.get('bands') or []}}


def _key(knobs):
    return tuple((k['parameter'], round(k['value'], 6)) for k in knobs)


def heard_before(c, key):
    """A measurement of the controls exactly as they were at `key`, if one is recent enough."""
    found = (getattr(c, 'chat_heard', None) or {}).get(key)
    return found['m'] if found and time.time() - found['at'] < HEARD_MAX_AGE else None


def _keep_heard(c, key, m):
    heard = getattr(c, 'chat_heard', None)
    heard = dict(heard) if isinstance(heard, dict) and 'key' not in heard else {}
    heard[key] = {'at': time.time(), 'm': m}
    c.chat_heard = dict(list(heard.items())[-20:])


# A measurement started while he is still talking (listen_ahead), so his request does not wait for it.
# `run` numbers each capture; forget_ahead() retires the current one so nothing it measured is kept.
AHEAD = {'lock': threading.Lock(), 'done': None, 'coach': None, 'key': None, 'run': 0}


def forget_ahead():
    """Something is about to move a control: a capture still running would mix two sounds, so it is not kept."""
    with AHEAD['lock']:
        AHEAD.update(run=AHEAD['run'] + 1, coach=None, key=None)


def listen(c, knobs, fresh=False):
    """(raw measurement, reason) of the sound as it is now. Only while Live is playing."""
    key = _key(knobs)
    if not fresh:
        with AHEAD['lock']:
            done = AHEAD['done'] if AHEAD['coach'] is c and AHEAD['key'] == key else None
        if done is not None and not done.is_set():
            try:
                playing = c.a.transport().get('playing')
            except Exception:
                playing = False
            if not playing:
                return None, 'not measured: Live is stopped'
            done.wait(sound_audio.MIN_SECONDS + sound_audio.CAPTURE_TIMEOUT + 1)   # same sound: wait, not a second capture
        if heard_before(c, key):
            return heard_before(c, key), None
    try:
        if not c.a.transport().get('playing'):
            return None, 'not measured: Live is stopped'
        m = c.hear()                       # waits for any capture still holding the tap (sound_audio.CAPTURE_LOCK)
    except Exception as exc:
        return None, 'not measured: ' + str(exc)[:160]
    _keep_heard(c, key, m)
    return m, None


def listen_ahead(c):
    """He has started talking: measure the synth already being worked on (the Live Ears tap on its track, never
    the microphone), so the engineer request that follows finds it measured. `c` is the coach already held, or
    None: this never looks for or switches synth, because his words are not known yet. Call with engine.LOCK
    held; the capture runs afterwards on its own thread and only reads the tap. Returns what it did."""
    try:
        with AHEAD['lock']:
            if AHEAD['done'] is not None and not AHEAD['done'].is_set():
                return {'measuring': False, 'reason': 'already measuring'}
        if c is None:
            return {'measuring': False, 'reason': 'no synth being worked on yet'}
        if not c.a.transport().get('playing'):
            return {'measuring': False, 'reason': 'Live is stopped'}
        key = _key(c.knobs_view())
        if heard_before(c, key):
            return {'measuring': False, 'reason': 'already measured'}
        measure = c.hear_later()
        if measure is None:
            return {'measuring': False, 'reason': 'synth is heard through Scrunch Host when he asks'}
    except Exception as exc:
        return {'measuring': False, 'reason': str(exc)[:160]}
    done = threading.Event()
    with AHEAD['lock']:
        run = AHEAD['run'] + 1
        AHEAD.update(done=done, coach=c, key=key, run=run)

    def work():
        try:
            m = measure()
            with AHEAD['lock']:
                keep = AHEAD['run'] == run
            if keep:
                _keep_heard(c, key, m)
        except Exception:
            pass                           # his request measures it itself, as before
        finally:
            done.set()
    try:
        threading.Thread(target=work, name='scrunch-listen-ahead', daemon=True).start()
    except Exception as exc:
        done.set()
        return {'measuring': False, 'reason': str(exc)[:160]}
    return {'measuring': True}


def starting_point(c, knobs):
    """The control positions the current change started from (after any revert it made first)."""
    origin = {s['parameter']: s['origin'] for s in (c.move or {}).get('plan', {}).get('steps', [])}
    return tuple((k['parameter'], round(origin.get(k['parameter'], k['value']), 6)) for k in knobs)


def taste(c, text):
    if taste_memory is None:
        return None
    try:
        return taste_memory.context(c.m.get('plugin'), c.m.get('trackName'), text,
                                    preset=(c.m.get('preset') or {}).get('name'), max_chars=6000)
    except Exception as exc:
        return {'unavailable': str(exc)[:200]}


def remember(c, rid, text, kind, reply, out, audio=None, journal=None):
    """One record of this exchange in the producer's musical memory. Returns a problem, or None."""
    if taste_memory is None or c is None:
        return None
    event = {'eventId': 'sound-chat:' + rid, 'source': c.source, 'at': time.time(), 'kind': kind,
             'plugin': c.m.get('plugin'), 'preset': (c.m.get('preset') or {}).get('name'),
             'trackName': c.m.get('trackName'), 'words': text, 'ai_rationale': reply,
             'steps': [{'knob': k['label'], 'from': k.get('was'), 'to': k.get('now'), 'why': k.get('why')}
                       for k in out.get('knobs') or []]}
    if out.get('automation'):
        event['automation'] = out['automation']
    if audio:
        event['audio'] = audio
    if journal:
        event['journal'] = journal
    try:
        taste_memory.record(event)
    except Exception as exc:
        return 'Not saved to your musical memory: %s' % str(exc)[:200]
    import taste_compaction
    taste_compaction.after_record(c.source)
    return None


def changes(out):
    if out.get('automation'):
        return '; '.join('%s automated: %d points, beats %s to %s' % (r['label'], r['points'], r['fromBeat'], r['toBeat'])
                         if r['points'] else '%s automation removed' % r['label'] for r in out['automation'])
    if out.get('automationReverted'):
        return 'automation put back as it was'
    rows = out.get('knobs') or []
    if out.get('reverted'):
        return '; '.join('%s back to %s' % (k['label'], k['now']) for k in rows)
    return '; '.join('%s: %s to %s' % (k['label'], k['was'], k['now']) for k in rows)


def perform(c, reply, text, host):
    """Carry out what the engineer decided. Returns (out, kind, journal event or None)."""
    action = reply['action']
    if action in ('automate', 'patch', 'host', 'revert', 'save', 'play', 'effect', 'preset'):
        allowed, _, why = action_layer.aar_state()      # the kill switch voids anything still queued
        if not allowed:
            raise ValueError(why)
    if action == 'change':
        return c.change(reply, text), ('rejected-and-changed' if reply.get('revertFirst') else 'change'), None
    if action == 'automate':
        return c.automate(reply), 'automate', None
    if action == 'patch':
        return c.patch(reply), 'patch', None
    if action == 'host':
        return c.host_in_wrapper(), 'host', None
    if action == 'revert':
        if audition_pair.get(c):
            done = audition_pair.toggle(c)
            return {'say': audition_pair.english(done), 'audition': done}, 'revert', None
        if not c.move:
            if getattr(c, 'search_undo', None):
                import brooklyn_rose
                done = brooklyn_rose.put_back(c)
                return {'say': 'Search moves %s.' % done['status']}, 'revert', None
            if getattr(c, 'undo_patch', None) is not None:
                return c.revert_patch(), 'revert', None
            if getattr(c, 'lanes_undo', None) is not None:
                return c.revert_automation(), 'revert', None
            return {}, 'revert', None
        out = c.steer('revert')
        return out, 'revert', getattr(c, 'last_journal', None)
    if action == 'keep':
        journal = c.keep_move(text) if c.move else None
        return {}, 'keep', journal
    if action == 'save':
        return c.save_sound(reply['saveName']), 'save', None
    if action == 'play':
        return c.play(), 'play', None
    if action == 'stop':
        engine.stop()
        return {}, 'stop', None
    if action == 'effect':
        # Swapping an effect slot needs the Set rewritten and reopened; the live engineer never does that.
        return {'say': 'Changing an effect slot needs your Set saved and reopened, which I never do while you work. '
                       'I can shape the effect that is there through its controls instead.'}, 'effect', None
    if action == 'preset':
        navigator = preset_navigation.NAVIGATOR
        if reply['preset'] == 'scan':
            return navigator.start(host, text), 'preset', None
        if reply['preset'] == 'keep':
            navigator.stop()
            return {}, 'preset', None
        return navigator.step(host, reply['preset'], text), 'preset', None
    return {}, action, None


FAST_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'action': {'type': 'string', 'enum': ['steer', 'revert', 'keep', 'handoff']},
    'amount': {'type': 'number', 'description': 'for steer: 0 = where it started, 1 = the engineer\'s targets, up to 2 = further the same way'},
    'say': {'type': 'string', 'description': 'one short line to the producer'}},
    'required': ['action', 'amount', 'say']}
FAST_SYSTEM = ('You are the quick hands of the studio engineer. The engineer just made lastChange: it moved these controls '
               'from where they were towards its targets; amount is how far (0 = where it started, 1 = the targets). '
               'The producer has replied. If he wants more or less of the same thing, answer steer with the new '
               'amount. If he rejects it, '
               'answer revert. If he accepts it as it is, answer keep. Anything else - a new idea, another control, a question, anything unclear - answer '
               'handoff and the engineer will take it. Never invent controls. Reply as JSON in the schema.')
FAST_WORDS = 10


def fast_profile(st, body):
    """The fast steering model: the engineer's own provider with the fast model ID the producer set, or None."""
    model = str(body.get('engineerFastModel') or st.get('engineerFastModel') or '').strip()
    import sound_language
    profile = sound_language.selected(st)
    return dict(profile, model=model) if model and profile else None


def fast_steer(c, text, st, body, timings, usage):
    """Short follow-ups on the last change go to the fast model. Returns (out, kind, said) or None to hand back."""
    if not action_layer.aar_state()[0]:
        return None                    # switched off: the engineer path reports it and writes nothing
    profile = fast_profile(st, body)
    other = getattr(c, 'last_patch', None) if getattr(c, 'undo_patch', None) is not None else \
        getattr(c, 'last_automation', None) if getattr(c, 'lanes_undo', None) is not None else None
    if profile is None or len(text.split()) > FAST_WORDS or not (c.move or other):
        return None
    move = c.move
    if move:
        ctx = {'producer': text, 'lastChange': {'instruction': move['instruction'], 'amount': move['amount'],
               'moved': [{'setting': s['setting'], 'from': s['display'], 'why': s['why']} for s in move['plan']['steps']]}}
    else:
        # Its last change was patch data or automation: it can only be kept or put back as a whole.
        ctx = {'producer': text, 'lastChange': dict(other, amount=1.0, note='this change can only be kept or put back; '
                                                    'steer is not possible, answer revert, keep or handoff')}
    tick = time.perf_counter()
    timings['modelCalls'] += 1
    try:
        import providers
        answer, used = providers.structured(profile, FAST_SYSTEM, ctx, FAST_SCHEMA)
    except Exception:
        return None                    # any trouble: the engineer takes it
    finally:
        timings['modelMs'] += round((time.perf_counter() - tick) * 1000)
    usage.extend(used if isinstance(used, list) else [used])
    action = answer.get('action') if isinstance(answer, dict) else None
    say = str(answer.get('say') or '')[:300] if isinstance(answer, dict) else ''
    if action in ('revert', 'steer'):
        forget_ahead()                     # controls may move now: a capture still running would mix two sounds
    if action == 'revert':
        timings['route'] = 'sound-fast'
        if audition_pair.get(c):
            done = audition_pair.toggle(c)
            return {'say': audition_pair.english(done), 'audition': done}, 'revert', say
        if move:
            return c.steer('revert'), 'revert', say
        return (c.revert_patch() if getattr(c, 'undo_patch', None) is not None else c.revert_automation()), 'revert', say
    if action == 'keep':
        timings['route'] = 'sound-fast'
        if move:
            c.keep_move(text)
        return {}, 'keep', say
    if action == 'steer' and not move:
        return None
    if action == 'steer':
        try:
            amount = max(0., min(2., float(answer.get('amount'))))
        except (TypeError, ValueError):
            return None
        if abs(amount - move['amount']) < 1e-3:
            return None
        if not isinstance(getattr(c, 'last_verified', None), dict):
            # A move with no verified contract record (the older coach page): steered from its own origin.
            timings['route'] = 'sound-fast'
            return c._apply(amount), 'change', say
        # More / less of the last change: repeated from where it is now, through the same checks and readback.
        proposal, result, out, limits = action_layer.further(c, amount - move['amount'], text, str(body.get('requestId', '')),
                                                            action_layer.aar_state()[1])
        if proposal is None or result['status'] != 'verified':
            return None                # conflict or nothing verified: the engineer takes it and says why
        if c.move:
            c.move['eventId'] = 'sound-chat:' + str(body.get('requestId', ''))
        timings['route'] = 'sound-fast'
        if limits:
            say = (say + ' ' + '%s reached the end of its range.' % ', '.join(limits[:4])).strip()
        return dict(out, verifiedResult={'status': result['status'], 'resultId': result['result_id'],
                                         'writes': len(result['writes'])}), 'change', say
    return None


def match_turn(c, text, rid, history, st, timings, usage):
    """Match Reference is ON: this message runs Brooklyn Rose's full search. State-based, never word-based.

    The model is asked once, for two things only: its plain-words explanation (say) and candidate controls the
    search may include (validated against this synth's real controls). What it decides cannot replace the search.
    Its play / stop / revert keep their existing paths, and a change confined to the synth's output-level controls
    is a level request and keeps the ordinary verified change path. Returns (out, kind, said)."""
    import brooklyn_rose
    refused = match_reference.missing(st, c)
    if refused:
        return {'matchReference': match_reference.view(st), 'refused': refused}, 'no_action', refused
    knobs = c.knobs_view()
    by_id = {k['parameter']: k for k in knobs}
    decision, explanation, suggested = None, '', []
    try:
        decision, used = c.decide(text, conversation=history, heard='not listened yet: the search measures the sound itself',
                                  taste=taste(c, text), knobs=knobs,
                                  extra={'matchReference': match_reference.for_engineer(st), 'questionsLeft': 0})
        usage.extend(used if isinstance(used, list) else [used])
        explanation = decision.get('say') or ''
        suggested = list(decision.get('searchControls') or []) + \
            [t['knob']['parameter'] for t in (decision.get('targets') or []) + (decision.get('targetsB') or [])]
    except Exception as exc:
        explanation = ''
        timings['engineerUnavailable'] = str(exc)[:200]        # the search still runs; only the explanation is missing
    action = (decision or {}).get('action')
    if action in ('play', 'stop'):
        out, kind, _ = perform(c, decision, text, engine.host())
        return out, kind, explanation or ('Playing.' if action == 'play' else 'Stopped.')
    if action == 'revert':
        out, kind, _ = perform(c, decision, text, engine.host())
        return out, kind, (explanation + ' ' + (out.get('say') or '')).strip()
    targets = (decision or {}).get('targets') or []
    if action == 'change' and targets and not decision.get('revertFirst') and \
            all((by_id.get(t['knob']['parameter']) or {}).get('group') == 'Main Output' for t in targets):
        _, generation, _ = action_layer.aar_state()
        _, result, out = action_layer.execute(c, decision, text, rid, generation)
        said = action_layer.explain(result, out, explanation, c.knobs_view())
        return dict(out, verifiedResult={'status': result['status'], 'resultId': result['result_id'],
                                         'writes': len(result['writes'])}), 'change', said
    _, generation, _ = action_layer.aar_state()
    tick = time.perf_counter()
    try:
        done = brooklyn_rose.run(c, st['referenceTarget'], None, rid, generation, suggested=suggested or None)
    except ValueError as exc:
        return {'matchReference': match_reference.view(st)}, 'no_action', 'Search not run: %s' % str(exc)[:300]
    finally:
        timings['executionMs'] += round((time.perf_counter() - tick) * 1000)
    out = {'searchPlan': done['plan'], 'searchResult': done['result'], 'matchReference': match_reference.view(st)}
    said = (('Engineer: ' + explanation + '\n') if explanation else '') + done['say']
    return out, 'search', said


def chat(body, coach_factory):
    started = time.perf_counter()
    st = engine.STATE
    text = str(body.get('text', '')).strip()
    rid = str(body.get('requestId', ''))
    button = body.get('button')
    mode = 'act'
    if not 1 <= len(text) <= 6000 or not 1 <= len(rid) <= 100:
        raise ValueError('Enter a message and request ID.')
    if button is not None and button not in BUTTONS:
        raise ValueError('Unknown sound button.')
    log = st.setdefault('engineerChat', [])
    previous = next((m for m in log if m.get('requestId') == rid and m['role'] == 'assistant'), None)
    if previous:
        return previous
    if any(m.get('requestId') == rid for m in log):
        raise ValueError('This message was already started. Inspect the sound before retrying.')
    history = []
    if button is not None:
        forget_ahead()                     # a button moves controls without listening: a running capture is not kept
    log.append({'role': 'user', 'text': text, 'requestId': rid, 'mode': mode, 'workspace': 'sound'})
    engine.save()
    timings = {'route': 'sound-button' if button else 'sound-model', 'interpretationMs': 0, 'executionMs': 0,
               'contextMs': 0, 'modelMs': 0, 'listenMs': 0, 'modelCalls': 0}
    failed = False
    out = {}
    speech = ''
    notes = []
    usage = []
    c = None
    original_interpreter = None
    navigator = preset_navigation.NAVIGATOR
    # Whatever he says while presets are scanning, the scan stops on the preset he heard.
    if navigator.state['scanning']:
        navigator.stop()
    try:
        if button == 'keep-preset':
            navigator.stop()
            engine.stop()
            reply = 'Kept this preset.'
        elif button == 'stop':
            engine.stop()
            reply = 'Stopped.'
        else:
            context_started = time.perf_counter()
            c = coach_factory()
            timings['coachMs'] = round((time.perf_counter() - context_started) * 1000)   # finding the synth in Live
            history = conversation(log, rid, c.m.get('plugin'))
            original_interpreter = c.interpreter
            c.source = body.get('source', 'user')
            chosen = body.get('engineerProvider')
            if chosen:
                if not any(p['id'] == chosen for p in st.get('providers', [])):
                    raise ValueError('Choose a saved engineer model.')
                st['engineerProvider'] = chosen
            if original_interpreter:
                def interpret(words, ctx):
                    tick = time.perf_counter()
                    timings['modelCalls'] += 1
                    try:
                        return original_interpreter(words, ctx)
                    finally:
                        timings['modelMs'] += round((time.perf_counter() - tick) * 1000)
                c.interpreter = interpret
            c.ensure_device()
            if button in ('revert', 'original', 'candidate') and (audition_pair.get(c) or button != 'revert'):
                # Stored before/after: host writes with readback only, no model call, no render, no listen.
                timings['contextMs'] = round((time.perf_counter() - context_started) * 1000)
                tick = time.perf_counter()
                if not audition_pair.get(c):
                    reply = 'No before/after is stored for this synth yet.'
                else:
                    if button == 'revert' and audition_pair.get(c)['playing'] == 'candidate' and c.move:
                        decisions.record(c, 'reject', text, rid, c.source)
                    try:
                        done = audition_pair.toggle(c, None if button == 'revert' else button)
                        out = dict(done, audition={k: done[k] for k in ('playing', 'writes', 'exact', 'ms')})
                        reply = audition_pair.english(done)
                        if not done.get('already'):
                            notes.append(remember(c, rid, text, 'revert' if done['playing'] == 'original' else 'change',
                                                  reply, out))
                    except audition_pair.Moved as exc:
                        reply = str(exc)
                    except Exception as exc:
                        failed, reply = True, 'Not switched: %s' % str(exc)[:200]
                timings['executionMs'] = round((time.perf_counter() - tick) * 1000)
            elif button == 'revert':
                timings['contextMs'] = round((time.perf_counter() - context_started) * 1000)
                if not c.move and getattr(c, 'search_undo', None):
                    import brooklyn_rose
                    out = {}
                    reply = 'Search moves %s.' % brooklyn_rose.put_back(c)['status']
                elif not c.move and getattr(c, 'undo_patch', None) is not None:
                    out = c.revert_patch()
                    reply = 'Put back: ' + changes(out) + '.'
                elif not c.move and getattr(c, 'lanes_undo', None) is not None:
                    out = c.revert_automation()
                    reply = 'Put back: ' + changes(out) + '.'
                elif not c.move:
                    reply = 'Nothing to put back.'
                elif action_layer.revert_conflicts(c):
                    reply = ('You have moved %s since my change, so I did not put it back over your edit.'
                             % ', '.join(action_layer.revert_conflicts(c)[:4]))
                else:
                    out = c.steer('revert')
                    reply = 'Put back: ' + changes(out) + '.'
                    notes.append(remember(c, rid, text, 'revert', reply, out, journal=getattr(c, 'last_journal', None)))
                    decisions.record(c, 'reject', text, rid, c.source)
            elif button in ('more', 'less'):
                # Instant, no model call: the last verified change again, half a step up or down.
                timings['contextMs'] = round((time.perf_counter() - context_started) * 1000)
                previous = c.move
                pair_before = audition_pair.snapshot(c)
                proposal, result, out, limits = action_layer.further(c, .5 if button == 'more' else -.5, text, rid,
                                                                    action_layer.aar_state()[1])
                if proposal is None:
                    reply = result['failure_reason']
                else:
                    if c.move:
                        c.move['eventId'] = 'sound-chat:' + rid
                    reply = action_layer.explain(result, out, '', c.knobs_view())
                    if limits:
                        reply += '\n%s reached the end of its range.' % ', '.join(limits[:4])
                    level_match.carry(previous, c.move)
                    if result['status'] == 'verified' and c.move and c.move.get('levelBase'):
                        tick = time.perf_counter()
                        after, _ = listen(c, c.knobs_view(), fresh=True)
                        timings['listenMs'] += round((time.perf_counter() - tick) * 1000)
                        done = level_match.match(c, None, after, text) if after else None
                        if done:
                            out = dict(out, levelMatch=done)
                            reply += '\n' + level_match.english(done)
                    out = dict(out, verifiedResult={'status': result['status'], 'resultId': result['result_id'],
                                                    'writes': len(result['writes'])})
                    if result['status'] == 'verified':
                        audition_pair.commit(c, pair_before, button, text)
                    notes.append(remember(c, rid, text, 'change', reply, out))
            elif button == 'keep':
                if not c.move:
                    reply = 'Nothing new to keep.'
                else:
                    kept = dict(c.move)
                    journal = c.keep_move(text)
                    reply = 'Kept.'
                    notes.append(remember(c, rid, text, 'keep', reply, {}, journal=journal))
                    decisions.record(c, 'keep', text, rid, c.source)
                    if c.source == 'user':
                        try:
                            lexicon.learn_from_keep(kept['instruction'], kept.get('eventId'), c.m.get('plugin'),
                                                    [{'label': s_['label'], 'direction': 'up' if s_['direction'] > 0 else 'down'}
                                                     for s_ in kept['plan']['steps']])
                        except Exception:
                            pass
            elif button == 'play':
                out = c.play()
                reply = 'Playing.'
            elif button == 'save':
                name = text.split(' as ', 1)[-1].strip()
                out = c.save_sound(name)
                reply = out['say']
                notes.append(remember(c, rid, text, 'save', reply, {}))
            else:
                before_fast = dict(c.move) if c.move else None
                # An answer to the engineer's own question goes to the engineer, who asked it and sees the conversation.
                first_line = history[-1]['text'].split('\n')[0].strip() if history else ''
                awaiting = bool(history) and history[-1]['who'] == 'engineer' and first_line.endswith('?') \
                    and not first_line.startswith('Audition')     # keep/reject after an audition stays quick
                answer = ab_takes.reply_to(c, text)
                panel_ab = body.get('abPanel')
                if panel_ab is not None:          # a button on the panel over Live: same path as his voice
                    if not ab_takes.pending(c):
                        raise ValueError('No A/B comparison is waiting.')
                    answer = ab_takes.panel_answer(c, panel_ab)
                if answer is None and ab_takes.pending(c):
                    ab_takes.drop(c)               # a new request instead of a pick: what is playing stays
                matching = match_reference.is_on(st) and not answer      # his mode, never his words
                fast = None if awaiting or answer or matching else fast_steer(c, text, st, body, timings, usage)
                if answer:
                    timings['route'] = 'sound-ab'
                    what, side = answer
                    try:
                        _, generation_ab, _ = action_layer.aar_state()
                        out, reply = ab_takes.switch(c, side, rid, generation_ab) if what == 'switch' else \
                            ab_takes.choose(c, side, text, rid, generation_ab, 'panel' if panel_ab is not None else c.source)
                    except Exception as exc:
                        out, reply = {}, 'Could not do that: %s' % str(exc)[:200]
                    notes.append(remember(c, rid, text, 'ab-' + what, reply, {}))
                elif fast is not None:
                    out, kind, said = fast
                    moved = changes(out)
                    reply = '\n'.join(x for x in (said, moved + '.' if moved else '') if x) or 'Done.'
                    notes.append(remember(c, rid, text, kind, said, out))
                    if kind in ('keep', 'revert'):
                        decisions.record(c, 'keep' if kind == 'keep' else 'reject', text, rid, c.source)
                    if kind == 'keep' and before_fast and c.source == 'user':
                        try:
                            lexicon.learn_from_keep(before_fast['instruction'], before_fast.get('eventId'), c.m.get('plugin'),
                                                    [{'label': s_['label'], 'direction': 'up' if s_['direction'] > 0 else 'down'}
                                                     for s_ in before_fast['plan']['steps']])
                        except Exception as exc:
                            notes.append('Not added to your words: %s' % str(exc)[:200])
                elif matching:
                    timings['route'] = 'sound-match-reference'
                    out, kind, said = match_turn(c, text, rid, history, st, timings, usage)
                    reply = said or 'Done.'
                    notes.append(remember(c, rid, text, kind, said, out))
                else:
                    tick = time.perf_counter()
                    knobs = c.knobs_view()
                    timings['knobsMs'] = round((time.perf_counter() - tick) * 1000)          # reading its controls
                    tick = time.perf_counter()
                    heard, why_not = listen(c, knobs)
                    timings['listenMs'] += round((time.perf_counter() - tick) * 1000)
                    memory = taste(c, text)
                    timings['contextMs'] = round((time.perf_counter() - context_started) * 1000) - timings['listenMs']
                    qleft = action_layer.questions_left(st)
                    _, generation, _ = action_layer.aar_state()
                    ref_before = None
                    ref_ctx = None
                    ref = st.get('referenceTarget')
                    if ref:
                        if heard:          # Live is playing: measure the synth against his reference passage
                            tick = time.perf_counter()
                            try:
                                ref_before = reference_target.measure(c, ref, engine.DATA / 'reference-measures')
                                ref_ctx = reference_target.for_engineer(ref_before)
                            except Exception as exc:
                                ref_ctx = reference_target.not_measured(ref, str(exc)[:200])
                            timings['listenMs'] += round((time.perf_counter() - tick) * 1000)
                        else:
                            ref_ctx = reference_target.not_measured(ref, 'Live is stopped')
                    try:
                        words_known = lexicon.for_engineer(text)
                    except Exception as exc:
                        words_known = {'unavailable': str(exc)[:200]}
                    decision, used = c.decide(text, conversation=history, heard=summary(heard) if heard else why_not,
                                              taste=memory, knobs=knobs,
                                              extra=dict({'questionsLeft': qleft, 'producerLexicon': words_known},
                                                         **({'referenceTarget': ref_ctx} if ref_ctx else {})))
                    if ref_ctx is not None and decision['action'] == 'change':
                        decision = dict(decision, referenceTarget=reference_target.proposal_reference(ref, ref_before))
                    usage.extend(used if isinstance(used, list) else [used])
                    if decision['action'] == 'question':
                        if qleft <= 0:       # ceiling reached: never a third question, never a guess dressed as sure
                            decision = dict(decision, action='no_action', question=None,
                                            say="I can't identify what to change from that, so I changed nothing.")
                        else:
                            action_layer.asked(st)
                    host = engine.host()
                    kept = dict(c.move) if decision['action'] == 'keep' and c.move else None
                    result = proposal = None
                    rationale = None       # the engineer's own reason, kept in memory apart from the readback
                    if decision['action'] in ('change', 'revert') and (decision.get('revertFirst') or decision['action'] == 'revert'):
                        blocked = action_layer.revert_conflicts(c)
                        if blocked:
                            decision = dict(decision, action='talk', say='You have moved %s since my change, so I did not put '
                                            'it back over your edit.' % ', '.join(blocked[:4]))
                    reverted_first = False
                    pair_before = audition_pair.snapshot(c) if decision['action'] in ('change', 'further') else None
                    if decision['action'] == 'change':
                        if decision.get('revertFirst') and c.move:
                            clear, why = action_layer.cancel_clear(rid, generation)
                            if clear:                      # never put anything back once Scrunch is switched off
                                reverted_first = True      # the reference "before" was measured with that change in
                                c.steer('revert')
                        proposal, result, out = action_layer.execute(c, decision, text, rid, generation)
                        if c.move:
                            c.move['eventId'] = 'sound-chat:' + rid      # what a later keep teaches the lexicon
                        # 'rejected' only when something was actually put back, never from the flag alone
                        kind, journal = ('rejected-and-changed' if reverted_first else 'change'), None
                        said = action_layer.explain(result, out, decision.get('say') or '', c.knobs_view(),
                                                    audition=decision.get('audition'))
                        rationale = decision.get('say') or ''
                        out = dict(out, verifiedResult={'status': result['status'], 'resultId': result['result_id'],
                                                        'writes': len(result['writes'])})
                    elif decision['action'] == 'further':
                        previous = c.move
                        proposal, result, out, limits = action_layer.further(c, decision['amount'], text, rid, generation)
                        level_match.carry(previous, c.move)
                        if proposal is None:
                            said, kind, journal, result = result['failure_reason'], 'no_action', None, None
                        else:
                            if c.move:
                                c.move['eventId'] = 'sound-chat:' + rid
                            kind, journal = 'change', None
                            said = action_layer.explain(result, out, decision.get('say') or '', c.knobs_view())
                            if limits:
                                said += '\n%s reached the end of its range.' % ', '.join(limits[:4])
                            rationale = decision.get('say') or ''
                            out = dict(out, verifiedResult={'status': result['status'], 'resultId': result['result_id'],
                                                            'writes': len(result['writes'])})
                    elif decision['action'] == 'ab':
                        kind, journal = 'ab', None
                        try:
                            out, said = ab_takes.offer(c, decision, text, rid, generation, heard,
                                                       lambda c_: listen(c_, c_.knobs_view(), fresh=True)[0])
                        except Exception as exc:
                            ab_takes.PENDING.pop(ab_takes._key(c), None)
                            if c.move:
                                c.steer('revert')
                            out, kind, said = {}, 'no_action', 'Could not set up A and B: %s' % str(exc)[:200]
                    elif decision['action'] == 'no_action':
                        out, kind, journal = {}, 'no_action', None
                        said = decision.get('say') or "I can't tell which part of the sound you mean, so I changed nothing."
                    else:
                        out, kind, journal = perform(c, decision, text, host)
                        said = decision.get('question') or decision.get('say') or ''
                    audio = None
                    if kind in ('change', 'rejected-and-changed', 'patch') and out.get('knobs') and heard and \
                            (result is None or result['status'] == 'verified'):
                        tick = time.perf_counter()
                        now_knobs = c.knobs_view()
                        before = heard_before(c, starting_point(c, now_knobs))
                        after, _ = listen(c, now_knobs, fresh=True)
                        timings['listenMs'] += round((time.perf_counter() - tick) * 1000)
                        if after and before is None:
                            # Its change started from a sound nobody measured (it put something back first):
                            # the result is reported, never compared against the wrong "before".
                            audio = {'before': None, 'after': summary(after), 'result': 'not compared'}
                        elif after:
                            heard = before
                            import sound_audio
                            verdict = sound_audio.verdict(out.get('move'), heard, after, expect=c.move.get('expect')) \
                                if c.move and c.move.get('expect') else {'result': 'not measurable'}
                            audio = {'before': summary(heard), 'after': summary(after), 'expected': c.move.get('expect') if c.move else None,
                                     'result': verdict.get('result'), 'measured': verdict.get('measured')}
                            if verdict.get('result') in ('no change', 'wrong way') and kind != 'patch':
                                # It missed by the engineer's own measure: one look at the result, one correction,
                                # through the same checks and readback as the first change.
                                second, used = c.decide(text, conversation=history, heard=summary(after),
                                                        taste=memory, after=audio, extra={'questionsLeft': 0})
                                usage.extend(used if isinstance(used, list) else [used])
                                if second['action'] == 'change' and not action_layer.revert_conflicts(c):
                                    c.steer('revert')
                                    proposal, result, again = action_layer.execute(c, dict(second, revertFirst=False), text, rid, generation)
                                    out = dict(again, correctedAfterListening=True,
                                               verifiedResult={'status': result['status'], 'resultId': result['result_id'],
                                                               'writes': len(result['writes'])})
                                    # kind stays as it was: correcting its own first try is not his reject
                                    # of anything (a 'rejected-and-changed' here recorded a false reject)
                                    rationale = second.get('say') or rationale      # memory keeps the kept attempt's reason
                                    audio = dict(audio, firstAttempt=True, correctedAfterListening=True)
                                    said = action_layer.explain(result, again, second.get('say') or '', c.knobs_view(),
                                                                audition=second.get('audition'))
                            if not (audio or {}).get('correctedAfterListening') and result is not None \
                                    and result['status'] == 'verified':
                                try:
                                    done = level_match.match(c, heard, after, text)
                                except Exception as exc:
                                    done = {'matched': False, 'gapDb': None, 'note': str(exc)[:120]}
                                if done:
                                    out = dict(out, levelMatch=done)
                                    said = (said + '\n' + level_match.english(done)).strip()
                    if ref_before is not None and result is not None and result['status'] == 'verified' and reverted_first:
                        said = (said + '\nNot compared with the reference: I put my previous change back first, so the '
                                'starting sound was never measured.').strip()
                    elif ref_before is not None and result is not None and result['status'] == 'verified':
                        tick = time.perf_counter()
                        try:            # measured again against the same passage: closer is a measurement, not an opinion
                            ref_after = reference_target.measure(c, ref, engine.DATA / 'reference-measures')
                            ref_verdict = reference_target.verdict(ref_before, ref_after)
                            said = (said + '\n' + reference_target.english(ref_verdict)).strip()
                            out = dict(out, reference=ref_verdict)
                        except Exception as exc:
                            said = (said + '\nNot measured against the reference afterwards: %s' % str(exc)[:160]).strip()
                        timings['listenMs'] += round((time.perf_counter() - tick) * 1000)
                    if pair_before is not None and result is not None and result['status'] == 'verified' \
                            and kind in ('change', 'rejected-and-changed'):
                        audition_pair.commit(c, pair_before, kind, text)
                    elif kind in ('patch', 'automate', 'effect', 'preset', 'host', 'ab'):
                        audition_pair.forget(c)        # something else is live now: the stored pair no longer applies
                    c.chat_reverted = kind == 'revert'
                    if decision['action'] in ('effect', 'host') and out.get('say'):
                        said = out['say']
                    elif decision['action'] == 'revert' and out.get('say'):
                        said = (said + ' ' + out['say']).strip()
                    elif decision['action'] == 'preset' and out.get('say'):
                        said = (said + ' ' + out['say']).strip()
                    moved = '' if result is not None else changes(out)
                    reply = '\n'.join(x for x in (said, moved + '.' if moved else '') if x) or 'Done.'
                    if body.get('learn') is True:
                        speech = reply
                    notes.append(remember(c, rid, text, kind, said if rationale is None else rationale, out,
                                          audio=audio, journal=journal))
                    if c.source == 'user' and getattr(c, 'decision_case', None) and kind in ('keep', 'revert', 'rejected-and-changed') \
                            or (c.source == 'user' and text.strip().lower().rstrip('.!') in decisions.NEITHER):
                        verdict_ = 'neither' if text.strip().lower().rstrip('.!') in decisions.NEITHER else \
                            'keep' if kind == 'keep' else 'reject'
                        try:
                            decisions.record(c, verdict_, text, rid, c.source)
                        except Exception as exc:
                            notes.append('Decision not recorded: %s' % str(exc)[:200])
                    if kept and c.source == 'user':
                        try:
                            lexicon.learn_from_keep(kept['instruction'], kept.get('eventId'), c.m.get('plugin'),
                                                    [{'label': st_['label'], 'direction': 'up' if st_['direction'] > 0 else 'down'}
                                                     for st_ in kept['plan']['steps']])
                        except Exception as exc:
                            notes.append('Not added to your words: %s' % str(exc)[:200])
                    if c.source == 'user' and decision.get('misheard'):
                        try:
                            pair = lexicon.confirmed_mishearing(decision, history, text)
                            if pair and lexicon.learn_mishearing(pair[0], pair[1], 'sound-chat:' + rid):
                                notes.append('Noted: when "%s" is written, you mean "%s".' % (pair[1], pair[0]))
                        except Exception as exc:
                            notes.append('Not added to your words: %s' % str(exc)[:200])
                    if result is not None and c.source == 'user':
                        move_id = None
                        try:
                            move_id = action_layer.record_move(proposal, result, 'sound-chat:' + rid)
                        except Exception as exc:
                            notes.append('Change not added to move history: %s' % str(exc)[:200])
                        if result['status'] == 'verified':
                            decisions.remember_change(c, rid, result, move_id, audition=decision.get('audition'))
        notes = [n for n in notes if n]
        if notes:
            reply += '\n' + '\n'.join(notes)
    except Exception as exc:
        failed = True
        reply = sanitize(str(exc)[:600])
    finally:
        if c is not None:
            c.interpreter = original_interpreter
    total = round((time.perf_counter() - started) * 1000)
    timings.update(totalMs=total, interpretationMs=timings['contextMs'] + timings['modelMs'],
                   executionMs=max(0, total - timings['contextMs'] - timings['modelMs'] - timings['listenMs']))
    message = {'role': 'assistant', 'requestId': rid, 'workspace': 'sound', 'mode': mode, 'at': time.time(),
               'text': reply, 'speechText': speech, 'failed': failed, 'timings': timings,
               'modelUsage': [u for u in usage if u], 'selected': body.get('selected'),
               'midiProvider': st.get('midiProvider', ''), 'playing': bool(out.get('playing')),
               'seconds': 0, 'soundChange': out, 'source': body.get('source', 'user'),
               'plugin': c.m.get('plugin') if c is not None else None}
    log.append(message)
    engine.save()
    return message
