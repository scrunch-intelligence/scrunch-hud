"""Independent Serum studio operations; completed steps survive later failures."""
import json
import re
import time
import uuid
from pathlib import Path

import daw_adapter
import checkpoints
import engine
import preset_index
import serum_setup
import serum_window
import workflow_requirements

OPS = {'variation', 'load', 'preset', 'next', 'show', 'loop', 'stop'}
_STOP_EPOCH = 0


def interrupt_playback():
    """Called by the immediate Stop endpoint without waiting for the planner."""
    global _STOP_EPOCH
    _STOP_EPOCH += 1


def validate(spec):
    if not isinstance(spec, dict) or set(spec) != {'steps'}:
        raise ValueError('Serum workflow needs a list of steps.')
    steps = spec['steps']
    if not isinstance(steps, list) or not steps:
        raise ValueError('Serum workflow needs at least one step.')
    for step in steps:
        if not isinstance(step, dict) or step.get('op') not in OPS:
            raise ValueError('Unknown Serum workflow step.')
        if set(step) - {'op', 'track', 'query', 'prompt'}:
            raise ValueError('Unknown Serum workflow fields.')
        if any(not isinstance(v, str) or len(v) > 6000 for v in step.values()):
            raise ValueError('Invalid Serum workflow text.')
    return steps


def read(a, ident):
    return a.track(ident, clips=True, devices=True, mixer=True, routing=True)


def track(label='', adapter=None):
    a = adapter or daw_adapter.DEFAULT
    tracks = engine.lab_session(a).get('tracks', [])
    if label:
        words = dict(zip('one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split(), range(1,21)))
        spoken = re.fullmatch(r'(?:track|channel)\s+([a-z]+)', label.strip(), re.I)
        if spoken and spoken[1].lower() in words:
            label = 'track '+str(words[spoken[1].lower()])
        number = re.fullmatch(r'(?:track\s+)?(\d+)', label.strip(), re.I)
        matches = [t for t in tracks if (t['trackIndex'] == int(number[1])-1 if number
                   else t['name'].casefold() == label.strip().casefold())]
    else:
        saved = engine.STATE.get('serumWorkflow', {})
        matches = []
        if saved.get('scope') == a.identity_scope():
            matches = [t for t in tracks if t['id'] == saved.get('trackId')]
        if not matches:
            selected = a.selected_track()
            matches = [t for t in tracks if t['id'] == selected]
    if len(matches) != 1:
        raise ValueError('Choose a displayed track number so I can target that sound.')
    return read(a, matches[0]['id'])


def remember(t, a):
    scope = a.identity_scope()
    old = engine.STATE.get('serumWorkflow', {})
    if old.get('scope') != scope or old.get('trackId') != t['id']:
        engine.STATE['serumWorkflow'] = {'scope':scope, 'trackId':t['id'], 'tried':[]}
    engine.save()


def instrument(t, a):
    found = []
    for device in t.get('devices', []):
        if (device.get('role'), device.get('deviceName')) == ('instrument', 'Serum 2'):
            found.append((None, a.read_device(device['id'])))
        elif device.get('role') == 'instrument-rack':
            rack = a.read_rack(device['id'])
            for chain in rack.get('chains', []):
                for child in chain.get('devices', []):
                    if child.get('type') == 'instrument: Serum 2':
                        found.append((rack, a.read_device(child['id'])))
    if len(found) != 1:
        raise ValueError('This track needs one Serum instance. Say load Serum on this track.')
    return found[0]


def new_track(after, a):
    engine.lab_session(a)
    source = read(a, after['id'])
    created = a.create_midi_track('Scrunch Serum '+uuid.uuid4().hex[:6], index=source['trackIndex']+1, mute=True, arm=False)
    a.update_track(created['id'], gainDb=-18, mute=True, arm=False)
    return read(a, created['id'])


def first_clip(t, a):
    clips = [c for c in t.get('clips', []) if c.get('start') is not None]
    if not clips:
        raise ValueError('There is no Arrangement MIDI on this track yet. The instrument remains available.')
    clip = min(clips, key=lambda c:c['start'])
    raw = a.clip(clip['id'])
    if raw.get('kind') != 'midi':
        raise ValueError('Choose a MIDI track for this phrase.')
    return raw


def variation(step, a, provider, generation_mode):
    source = track(step.get('track', ''), a)
    raw = first_clip(source, a)
    engine.load_source(raw['id'])
    take = engine.attempt({'provider':provider, 'mode':generation_mode,
                          'prompt':step.get('prompt') or 'Make a variation of the original.'})
    dest = new_track(source, a)
    take['live'] = {'trackId':dest['id'], 'name':dest['name'], 'stage':'track-created'}
    engine.save()
    clip = a.create_arrangement_clip(dest['trackIndex'], take['source']['length'], take['notes'], start=raw['start'],
        name='MIDI variation')
    take['live'].update(clipId=clip['id'], stage='clip-created'); engine.save()
    actual = a.clip(clip['id'])
    if actual.get('start') is None or abs(actual['start']-raw['start']) > .0001 or not engine.notes_equal(
            engine.clip_notes(actual, take['source']['length']), take['notes']):
        raise ValueError('The variation is saved, but its Live placement needs inspection.')
    take['live'].update(stage='verified', verified=True, trackIndex=dest['trackIndex'])
    remember(dest, a)
    engine.STATE['serumWorkflow']['takeId'] = take['id']; engine.save()
    return dest, f"Created the variation on track {dest['trackIndex']+1}, directly below the original."


def load(t, a):
    try:
        instrument(t, a)
        remember(t, a)
        return t, f"Serum is ready on track {t['trackIndex']+1}."
    except ValueError:
        pass
    if t.get('kind') != 'midi' or t.get('devices'):
        source = t
        t = new_track(t, a)
        remember(t, a)
        if source.get('kind') == 'midi' and source.get('clips'):
            raw = first_clip(source, a)
            copied = a.create_arrangement_clip(t['trackIndex'], raw['length'], raw['notes'] or [], start=raw['start'],
                name='Original phrase - Serum copy', looping=raw.get('looping', False))
            actual = a.clip(copied['id'])
            if not engine.notes_equal(actual['notes'] or [], raw['notes'] or []):
                raise ValueError('The copied phrase needs inspection; the original remains untouched.')
    remember(t, a)
    # MIDI may already exist from the preceding variation step.
    serum_setup.load_rack(t['id'], allow_clips=True)
    t = read(a, t['id']); instrument(t, a); remember(t, a)
    return t, f"Loaded Serum on track {t['trackIndex']+1}."


def preset(t, step, a):
    _, child = instrument(t, a)
    remember(t, a)
    saved = engine.STATE['serumWorkflow']
    query = step.get('query') or saved.get('query') or 'lead'
    found = []
    for product in ('Serum 2', 'Serum'):
        found.extend(preset_index.search(query, limit=50, product=product)['matches'])
    found.sort(key=lambda row:row['score'], reverse=True)
    choices = [r for r in found if r['presetId'] not in saved['tried'] and Path(r['sourcePath']).is_file()]
    if not choices:
        raise ValueError('No untried matching Serum files are available. Try a different sound description; the current sound is retained.')
    record = choices[0]
    saved['query'] = query; saved['tried'].append(record['presetId']); engine.save()
    serum_window.show(t['name'], child['id'])
    verified = serum_window.load_preset(t['name'], record)
    saved['preset'] = verified; engine.save()
    return 'Loaded '+verified['name']+'.'


def loop(t, a):
    clip = first_clip(t, a)
    start = clip['start']
    length = clip.get('span')
    if not length:
        raise ValueError('This clip needs a supported loop length; MIDI and Serum are retained.')
    if engine.AUDITION:
        engine.stop()
    for other in engine.lab_session(a)['tracks']:
        a.update_track(other['id'], solo=other['id']==t['id'])
    a.update_track(t['id'], mute=False, arm=False)
    result = a.play_arrangement(start, loop=(start, start+length))
    if not result.get('playing') or not result.get('loop'):
        raise ValueError('Loop playback did not start; check for an open Live dialog.')
    return f"Looping track {t['trackIndex']+1} until Stop or Ableton's spacebar."


def execute(spec, provider='', adapter=None, generation_mode='provider'):
    steps = validate(spec); a = adapter or daw_adapter.DEFAULT
    daw_adapter.require(a, workflow_requirements.serum_plan(steps), 'run this sound workflow')
    results = []; current = None; target_failed = False
    with engine.LOCK:
        stop_epoch = _STOP_EPOCH
        was_playing = bool(a.playback_state().get('playing'))
        for step in steps:
            op = step['op']
            try:
                if op == 'stop':
                    engine.stop(); message = 'Playback stopped; current sound retained.'
                elif op == 'variation':
                    current, message = variation(step, a, provider, generation_mode)
                else:
                    if target_failed and not step.get('track'):
                        raise ValueError('The new target was not created; existing tracks were left untouched.')
                    if op == 'load' and current is None and not step.get('track'):
                        engine.lab_session(a)
                        t = read(a, a.selected_track())
                    else:
                        t = track(step.get('track',''), a) if step.get('track') or current is None else read(a, current['id'])
                    if op == 'load':
                        current, message = load(t, a)
                    else:
                        current = t
                        if op in ('preset','next'): message = preset(t, step, a)
                        elif op == 'loop':
                            if stop_epoch != _STOP_EPOCH or (was_playing and not a.playback_state().get('playing')):
                                message = 'Playback stays stopped; the prepared sound is retained.'
                            else:
                                message = loop(t, a)
                        elif op == 'show':
                            _, child = instrument(t, a)
                            serum_window.show(t['name'], child['id']); message = 'Serum is open on screen.'
                        remember(t, a)
                results.append({'op':op, 'ok':True, 'text':message})
            except Exception as exc:
                if op == 'variation': target_failed = True
                results.append({'op':op, 'ok':False, 'text':str(exc)[:400]})
            checkpoints.event('serum-workflow-step', **results[-1])
    selected = engine.STATE.get('serumWorkflow',{}).get('takeId') if any(r['op']=='variation' and r['ok'] for r in results) else None
    return {'steps':results, 'failed':any(not r['ok'] for r in results), 'takeId':selected,
            'text':'\n'.join(r['text'] if r['ok'] else r['op'].title()+': '+r['text'] for r in results)}
