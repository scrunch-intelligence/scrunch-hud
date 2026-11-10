"""Window-independent preset stepping and playback-coupled audition scans."""
import json
import re
import threading
from pathlib import Path
import preset_editor

SENDER_NAME = 'Scrunch Preset MIDI'
PLUGIN_WORDS = r'(?:diva|pigments|jup(?:-8000(?: v)?)?|serum(?: 2)?)'


def command(text):
    text = ' '.join(text.lower().strip(' .!?').split())
    # Spoken, not typed: "scroll through the diva bass presets", and dictation's "presets" -> "process".
    if (re.search(r'\b(?:scan|scanning|scroll|scrolling|brows(?:e|ing)|audition|cycle|flick|step|run|play|go|try|load)\b', text)
            and re.search(r'\b(?:presets?|process|patch(?:es)?|sounds)\b', text)
            and not re.fullmatch(r'(?:next|previous|back one)(?: preset| sound| patch| one)?', text)):
        return 'scan'
    if re.fullmatch(r'(?:next|skip)(?: preset| sound| patch| one)?(?: please)?', text):
        return 'next'
    if re.fullmatch(r'(?:previous|back one|last one)(?: preset| sound| patch| one)?(?: please)?', text):
        return 'previous'
    if text in ('stop scanning', 'stop browsing', 'keep this preset'):
        return 'keep'
    return None


def serum_controllers(path=None):
    path = path or Path.home() / 'Library/Preferences/Serum2Prefs.json'
    prefs = json.loads(Path(path).read_text())
    result = {direction: prefs.get('CCForRocker Preset ' + sign, -1)
              for direction, sign in (('next', '+'), ('previous', '-'))}
    if not prefs.get('Enable CCForRockers') or any(type(v) is not int or not 1 <= v <= 119 for v in result.values()):
        raise ValueError('Serum preset MIDI stepping is not configured in Serum2Prefs.json.')
    if result['next'] == result['previous']:
        raise ValueError('Serum next and previous need different MIDI controller numbers.')
    return result


class Navigator:
    def __init__(self):
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.state = {'scanning': False, 'steps': 0}
        self.patches = {}

    def stop(self):
        self.cancel.set()
        with self.lock:
            self.state['scanning'] = False
            return dict(self.state)

    def target(self, adapter, text=''):
        ident = adapter.selected_track()
        if isinstance(ident, dict):ident = ident['id']
        named = re.search(r'\b('+PLUGIN_WORDS+r')\b', text.lower())
        if named:
            word = named[0]
            plugin = 'Jup-8000 V' if word.startswith('jup') else 'Serum 2' if word.startswith('serum') else word.title()
            selected_matches = [d for d in adapter.devices(ident) if d.get('deviceName') == plugin]
            if not selected_matches:
                matches = [(t, d) for t in adapter.session()['tracks'] for d in adapter.devices(t['id']) if d.get('deviceName') == plugin]
                if len(matches) != 1:
                    raise ValueError('Found %d %s instances. Select the track you want to browse.' % (len(matches), plugin))
                ident = matches[0][0]['id']
        track = adapter.track(ident, clips=True, devices=True)
        devices = adapter.devices(ident, parameters=True)
        instruments = [d for d in devices if d.get('role') == 'instrument']
        if len(instruments) != 1:
            raise ValueError('The selected track has no single directly addressable instrument; nested-rack preset navigation is not implemented yet.')
        synth = instruments[0]
        if synth.get('deviceName') in preset_editor.SUPPORTED:
            return track, synth, None, {}
        if synth.get('deviceName') != 'Serum 2':
            raise ValueError('%s preset stepping is not connected yet; its host preset list is not its library.' % synth['name'])
        controllers = serum_controllers()
        senders = [d for d in devices if d.get('className') == 'MidiCcControl' and d['name'] == SENDER_NAME]
        if len(senders) > 1:
            raise ValueError('This track contains multiple Scrunch preset senders; their identities need resolving.')
        if senders:
            sender = senders[0]
            if devices.index(sender) >= devices.index(synth):
                raise ValueError('The preset MIDI sender is after the instrument, so it cannot reach it.')
        else:
            sender = adapter.create_native_effect(track['trackIndex'], 'CC Control', SENDER_NAME, {}, at=devices.index(synth))
        return track, synth, sender, controllers

    def _step(self, adapter, target, direction, scanning=False):
        track, synth, sender, controllers = target
        devices = adapter.devices(track['id'])
        ids = [d['id'] for d in devices]
        if synth['id'] not in ids or (sender is not None and
                (sender['id'] not in ids or ids.index(sender['id']) >= ids.index(synth['id']))):
            raise ValueError('The instrument or its preset MIDI path changed during scanning.')
        if sender is None:
            if scanning and (self.cancel.is_set() or not adapter.preset_clock()['playing']):
                return {'skipped': 'scan-stopped'}
            result = preset_editor.step(adapter, adapter.track(track['id']), synth, direction,
                                        cancelled=self.cancel.is_set if scanning else None)
            if result.get('skipped'):return result
            self.state['steps'] += 1
            self.state['preset'] = result['preset']
            old = self.patches.get(synth['id'], {})
            self.patches[synth['id']] = {'revision': old.get('revision', 0) + 1,
                                        'name': result['preset']}
            return result
        controller = controllers[direction]
        adapter.set_cc_control(sender['id'], controller, 0, only_while_playing=scanning)
        result = adapter.set_cc_control(sender['id'], controller, 127, only_while_playing=scanning)
        if not result.get('skipped'):self.state['steps'] += 1
        return result

    def step(self, adapter, direction, text=''):
        self.stop()
        with self.lock:
            target = self.target(adapter, text)
            result = self._step(adapter, target, direction)
            return {'say': result.get('preset') or ('Next preset.' if direction == 'next' else 'Previous preset.'),
                    'presetReadback': result, 'presetNameVerified': bool(result.get('preset')),
                    'playing': adapter.preset_clock()['playing']}

    def start(self, adapter, text=''):
        self.stop()
        old = self.thread
        if old and old.is_alive():
            old.join(timeout=2)
            if old.is_alive():
                raise ValueError('The previous preset operation has not returned from Live yet.')
        with self.lock:
            target = self.target(adapter, text)
            track = target[0]
            clock = adapter.preset_clock()
            clips = [c for c in track.get('clips', []) if c.get('start') is not None]
            if clips:
                clip = min(clips, key=lambda c: c['start'])
                start = clip['start']
                length = clock['barBeats']
            else:
                start, length = 0, clock['barBeats']
                adapter.create_arrangement_clip(track['trackIndex'], length,
                    [{'p': 48, 't': 0, 'd': length * .75, 'v': 60}], name='Preset audition')
            self.cancel = threading.Event()
            self.state = {'scanning': True, 'steps': 0, 'track': track['name'], 'error': None}
            adapter.play_arrangement(start, loop=(start, start + length))
            self.thread = threading.Thread(target=self._run, args=(adapter, target, self.cancel), daemon=True)
            self.thread.start()
            return {'say': 'Scanning presets over a repeating MIDI phrase. Stop or keep retains the current sound.',
                    'playing': True, **self.state}

    def _run(self, adapter, target, cancel):
        previous = None
        try:
            while not cancel.wait(.05):
                clock = adapter.preset_clock()
                if not clock['playing']:
                    break
                beat = clock['beat']
                crossed = previous is not None and (beat < previous or int(beat / clock['barBeats']) > int(previous / clock['barBeats']))
                if crossed:
                    with self.lock:
                        if cancel.is_set() or not adapter.preset_clock()['playing']:
                            break
                        result = self._step(adapter, target, 'next', scanning=True)
                        if result.get('skipped'):break
                previous = beat
        except Exception as exc:
            with self.lock:
                self.state['error'] = str(exc)
            adapter.stop_playback()
        finally:
            with self.lock:
                self.state['scanning'] = False


NAVIGATOR = Navigator()
