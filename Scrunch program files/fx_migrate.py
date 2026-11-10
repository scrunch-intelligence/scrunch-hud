"""Effects into Scrunch Host FX, as two separate, explicit capabilities. No Set save, no reload, no .als written.

1. load_new: a NEW effect in a new Scrunch Host FX on a track (Live's browser loads the wrapper; the wrapper loads the
   plug-in). The plug-in starts in its own default state. Nothing on the track is replaced.

2. migrate: an EXISTING effect instance, loaded directly on a track, moved inside Scrunch Host FX with its settings,
   in the same place in the chain. Live does not give a Remote Script a plug-in's current state, so the state comes
   from one of two places, and never silently from a stale one:
     - a preset file John saved from the plug-in's own menu (.vstpreset): its current state, exactly; or
     - the saved Set (.als), used only when every setting Live shows for that device equals the saved copy. When Live
       shows none of its settings (a plug-in with no configured parameters), the saved copy cannot be checked and it
       is refused, naming the preset alternative.
   Refused before anything changes: a device inside a rack, automation or macro mappings on its settings (they would
   stop reaching it), anything else the report lists as blocking. What Live does not expose (MIDI/key mappings, a
   plug-in's sidechain input) is named as unverified and must be accepted explicitly.
   Then: Scrunch Host FX goes in directly after the original, switched off; it loads the plug-in and the state; the
   state is read back (every control compared, the audio part of the state compared byte for byte); the track is
   captured over the given bars with the original, again with the original (its own pass-to-pass variation), and
   with the wrapper instead; the wrapper is kept only if it differs from the original no more than the original
   differs from itself (or the difference is below -40 dB). The original stays in the chain, switched off (nothing is
   deleted); the wrapper is on. Any failure takes the wrapper out and puts the original's on/off back.

Plug-in state bytes use the VST3 component (IComponent) and controller (IEditController) chunks, as Live stores them.
"""
import base64, math, re, struct, time
from pathlib import Path

import wrapper_client as wc

FX_URI = 'query:Plugins#VST3:Scrunch:Scrunch%20Host%20FX'
FX_NAME = 'Scrunch Host FX'
VST3_DIR = Path('/Library/Audio/Plug-Ins/VST3')
SOUND_FLOOR_DB = -40.0
SOUND_MARGIN_DB = 3.0
UNVERIFIABLE = ('midi-mappings', 'sidechain')


class MigrationRefused(ValueError):
    """Nothing was changed; the report says why."""
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


# ---------------------------------------------------------------- state sources
def parse_vstpreset(path):
    """(processor, controller, class id) from a VST3 .vstpreset file (Steinberg preset format: 'VST3' header, class id,
    chunk list with 'Comp' and 'Cont')."""
    data = Path(path).read_bytes()
    if len(data) < 48 or data[:4] != b'VST3':
        raise ValueError('%s is not a VST3 preset file.' % path)
    class_id = data[8:40].decode('ascii', 'replace')
    (list_at,) = struct.unpack('<q', data[40:48])
    if data[list_at:list_at + 4] != b'List':
        raise ValueError('%s has no chunk list.' % path)
    (count,) = struct.unpack('<i', data[list_at + 4:list_at + 8])
    chunks = {}
    for i in range(count):
        at = list_at + 8 + i * 20
        ident = data[at:at + 4].decode('ascii', 'replace')
        off, size = struct.unpack('<qq', data[at + 4:at + 20])
        chunks[ident] = data[off:off + size]
    if not chunks.get('Comp'):
        raise ValueError('%s holds no component state.' % path)
    return chunks['Comp'], chunks.get('Cont', b''), class_id


def saved_parameters(set_path, track_name, plugin, track_index=None):
    """{name: value} of the settings Live shows for `plugin` on that track, as the saved Set holds them (configured
    parameters only: ParameterId != -1), plus the device's saved (processor, controller) state. Read-only."""
    import gzip, xml.etree.ElementTree as ET
    import host_migrate, wrapper_migrate
    doc = ET.fromstring(gzip.decompress(Path(set_path).read_bytes()))
    try:
        device = wrapper_migrate.track_device(doc, track_name, plugin, track_index)
    except ValueError:
        device = wrapper_migrate.track_device(doc, track_name, plugin)
    values = {}
    for p in list(device.find('ParameterList') or []):
        pid, name, manual = p.find('ParameterId'), p.find('ParameterName'), p.find('ParameterValue/Manual')
        if pid is None or pid.get('Value') == '-1' or name is None or manual is None:
            continue
        try:
            values[name.get('Value')] = float(manual.get('Value'))
        except (TypeError, ValueError):
            continue
    processor, controller = host_migrate.saved_state(set_path, track_name, plugin, track_index)
    return values, (processor, controller)


def _component(blob):
    """The IComponent (audio) part of a JUCE VST3 state blob, for byte comparison."""
    text = blob.decode('latin1', 'replace')
    m = re.search(r'<IComponent>(.*?)</IComponent>', text, re.S)
    return m.group(1) if m else None


# ---------------------------------------------------------------- reading the track
def _set_path(adapter):
    import host_migrate
    return host_migrate._set_path(adapter)


def _device_on(dev):
    return next((p for p in dev.get('parameters') or [] if p.get('name') == 'Device On'), None)


def _new_instance(before, wait, instances):
    deadline = time.time() + wait
    fresh = []
    while time.time() < deadline and not fresh:
        fresh = [r for r in instances() if r['instance'] not in before and r.get('host', 'Ableton Live') == 'Ableton Live']
        if not fresh:
            time.sleep(0.2)
    if len(fresh) != 1:
        raise RuntimeError('%d new Scrunch Host FX instances answered (expected 1).' % len(fresh))
    return fresh[0]


def inspect(adapter, track, device, preset=None):
    """Read-only migration report for one device: identity, position, on/off, what blocks a migration, what cannot be
    verified, and where its current state would come from."""
    tracks = {t['id']: t for t in adapter.session()['tracks']}
    if track not in tracks:
        raise ValueError('%s is not a track in the open Set.' % track)
    t = tracks[track]
    top = adapter.devices(track)
    ids = [d['id'] for d in top]
    report = {'track': track, 'trackName': t.get('name'), 'trackIndex': t.get('trackIndex'), 'device': device,
              'blocking': [], 'unverified': list(UNVERIFIABLE), 'stateSource': None}
    if device not in ids:
        report['blocking'].append('The device is not in the track\'s own chain (inside a rack, or gone); a rack chain cannot be migrated.')
        return report
    d = next(x for x in top if x['id'] == device)
    report.update(position=ids.index(device), name=d.get('name'), className=d.get('className'), plugin=d.get('deviceName') or d.get('name'))
    if d.get('className') != 'PluginDevice' or d.get('role') not in (None, 'audio-effect', 'audio_effect', 'effect'):
        if d.get('className') != 'PluginDevice':
            report['blocking'].append('%s is not a plug-in device (%s).' % (d.get('name'), d.get('className')))
    if FX_NAME in (d.get('name'), d.get('deviceName')):
        report['blocking'].append('This is already Scrunch Host FX.')
    path = VST3_DIR / ('%s.vst3' % report['plugin'])
    report['pluginPath'] = str(path)
    if not path.exists():
        report['blocking'].append('%s is not installed as VST3 at %s.' % (report['plugin'], path))
    full = adapter.device(device, parameters=True)
    params = [p for p in full.get('parameters') or [] if p.get('name') != 'Device On']
    on = _device_on(full)
    report['deviceOn'] = None if on is None else on.get('value')
    report['exposedSettings'] = len(params)
    locked = [p['name'] for p in params if p.get('enabled') is False or p.get('state') == 'disabled']
    if locked:
        report['blocking'].append('Settings controlled from elsewhere (macro or modulation mapping) would stop reaching it: %s.' % ', '.join(locked[:6]))
    automated = [p['name'] for p in params if p.get('automation') not in (None, 'none')]
    if automated:
        report['blocking'].append('Automation on %s would stop reaching the plug-in once it is inside Scrunch Host FX.' % ', '.join(automated[:6]))
    if any(p.get('automation') is None for p in params):
        report['unverified'].append('automation')
    if preset:
        try:
            proc, ctrl, class_id = parse_vstpreset(preset)
            report['stateSource'] = {'kind': 'preset', 'path': str(preset), 'classId': class_id, 'bytes': len(proc)}
        except (OSError, ValueError) as exc:
            report['blocking'].append('The preset could not be read: %s' % exc)
        return report
    set_path = _set_path(adapter)
    if set_path is None or not set_path.is_file():
        report['blocking'].append('The open Set has no saved file, so its saved plug-in state cannot be read. Save a preset from %s\'s own menu and give its path.' % report['plugin'])
        return report
    try:
        saved, _ = saved_parameters(set_path, t.get('name'), report['plugin'], t.get('trackIndex'))
    except (OSError, ValueError) as exc:
        report['blocking'].append('The saved Set does not hold %s on this track (%s). Save a preset from its own menu and give its path.' % (report['plugin'], exc))
        return report
    live = {p['name']: p.get('value') for p in params}
    if not live:
        report['blocking'].append('Live shows none of %s\'s settings, so Scrunch cannot check that the saved Set (%s) still holds its current '
                                  'settings. Supported alternative: save a preset from the plug-in\'s own menu (current state, exact) and '
                                  'give its path; or configure some of its parameters in Live and save the Set.' % (report['plugin'], time.ctime(set_path.stat().st_mtime)))
        return report
    differ = [n for n, v in live.items() if n not in saved or v is None or abs(float(v) - saved[n]) > 1e-4]
    if differ:
        report['blocking'].append('The saved Set (%s) is older than what %s shows now (%s differ). Save the Set, or save a preset from the '
                                  'plug-in\'s own menu and give its path; the stale settings are never used.' % (
                                      time.ctime(set_path.stat().st_mtime), report['plugin'], ', '.join(differ[:6])))
        return report
    report['stateSource'] = {'kind': 'saved Set', 'path': str(set_path), 'savedAt': time.ctime(set_path.stat().st_mtime),
                             'checkedAgainst': len(live), 'note': 'only the %d settings Live shows could be compared with the saved copy' % len(live)}
    return report


# ---------------------------------------------------------------- capability 1: a new effect
def load_new(adapter, track, plugin, after_device=None, wait=30.0, instances=None, wrapper=None):
    """Put `plugin` (a VST3 effect name as installed, e.g. 'FabFilter Pro-Q 4') into a new Scrunch Host FX on `track`,
    after `after_device` (default: end of the chain). Verified: Live placed the wrapper there, the wrapper hosts that
    plug-in from that path. Returns the device id and the wrapper record."""
    instances = instances or wc.instances
    wrapper = wrapper or wc.Wrapper
    path = VST3_DIR / ('%s.vst3' % plugin)
    if not path.exists():
        raise ValueError('%s is not installed as VST3 at %s. Nothing was changed.' % (plugin, path))
    before_ids = [d['id'] for d in adapter.devices(track)]
    if after_device is not None and after_device not in before_ids:
        raise ValueError('%s is not in that track\'s chain. Nothing was changed.' % after_device)
    known = {r['instance'] for r in instances()}
    loaded = adapter.load_device(track, FX_NAME, shelf='plugins', uri=FX_URI, **({'after_device': after_device} if after_device else {}))
    try:
        rec = _new_instance(known, wait, instances)
        with wrapper(rec, timeout=90) as w:
            hello = w.load(str(path))
        if Path(str(hello.get('childPath', ''))).resolve() != path.resolve():
            raise RuntimeError('The wrapper hosts %r, not %s.' % (hello.get('childPath'), path))
        now = [d['id'] for d in adapter.devices(track)]
        want = (before_ids.index(after_device) + 1) if after_device else len(before_ids)
        if now.index(loaded['id']) != want or [i for i in now if i != loaded['id']] != before_ids:
            raise RuntimeError('Live placed it at position %d, not %d.' % (now.index(loaded['id']), want))
    except Exception as exc:
        adapter.delete_device(loaded['id'])
        raise RuntimeError('%s; the new Scrunch Host FX was taken out again.' % exc) from exc
    return {'device': loaded['id'], 'instance': rec['instance'], 'plugin': plugin, 'child': hello.get('child'),
            'controls': hello.get('paramCount'), 'position': want,
            'say': '%s is on the track inside Scrunch Host FX, in its default state.' % plugin}


# ---------------------------------------------------------------- capability 2: move an existing instance
def _residual_db(a, b):
    import numpy as np
    n = min(len(a), len(b))
    a, b = np.asarray(a[:n], float), np.asarray(b[:n], float)
    ref = float(np.sqrt(np.mean(a ** 2))) if n else 0.0
    diff = float(np.sqrt(np.mean((a - b) ** 2))) if n else 0.0
    if ref <= 1e-9:
        return None
    return 20 * math.log10(max(diff, 1e-12) / ref)


def migrate(adapter, track, device, start_beat, end_beat, preset=None, accept_unverified=(), wait=30.0,
            instances=None, wrapper=None, rig=None):
    """Move one existing effect into Scrunch Host FX (module docstring). `start_beat`/`end_beat`: bars on that track
    where the effect has audio to compare. `accept_unverified`: the unverifiable items (inspect()['unverified']) John
    accepts. Returns the report; raises MigrationRefused (nothing changed) or RuntimeError (after taking it back)."""
    instances = instances or wc.instances
    wrapper = wrapper or wc.Wrapper
    report = inspect(adapter, track, device, preset)
    if report['blocking']:
        raise MigrationRefused('Not migrated: ' + ' '.join(report['blocking']), report)
    missing = [u for u in report['unverified'] if u not in accept_unverified]
    if missing:
        raise MigrationRefused('Not migrated: Scrunch cannot see %s for this device; accept them explicitly if you have checked them.' % ', '.join(missing), report)
    if not (isinstance(start_beat, (int, float)) and isinstance(end_beat, (int, float)) and end_beat > start_beat):
        raise MigrationRefused('Not migrated: give the bars (beats) where this effect has audio, for the sound check.', report)
    if preset:
        processor, controller, _ = parse_vstpreset(preset)
    else:
        _, (processor, controller) = saved_parameters(report['stateSource']['path'], report['trackName'], report['plugin'], report['trackIndex'])
    original = adapter.device(device, parameters=True)
    orig_on = _device_on(original)
    was_on = None if orig_on is None else float(orig_on.get('value', 1.0)) >= 0.5      # kept now: reads later see the switch
    live = {p['name']: p.get('value') for p in original.get('parameters') or [] if p.get('name') != 'Device On'}
    known = {r['instance'] for r in instances()}
    loaded = adapter.load_device(track, FX_NAME, shelf='plugins', uri=FX_URI, after_device=device)
    new_id = loaded['id']

    def switch(dev_id, on):
        dev = adapter.device(dev_id, parameters=True)
        p = _device_on(dev)
        if p is None:
            raise RuntimeError('%s shows no Device On switch.' % dev.get('name'))
        adapter.set_device_parameter(dev_id, p['id'], 'Device On', value=1.0 if on else 0.0)
        back = _device_on(adapter.device(dev_id, parameters=True)).get('value')
        if (back >= 0.5) != on:
            raise RuntimeError('%s did not switch %s.' % (dev.get('name'), 'on' if on else 'off'))

    try:
        switch(new_id, False)                                  # nothing heard from it until the check passes
        rec = _new_instance(known, wait, instances)
        blob = wc.juce_vst3_state(processor, controller)
        with wrapper(rec, timeout=90) as w:
            hello = w.load(report['pluginPath'])
            w.set_state(base64.b64encode(blob).decode())
            back = base64.b64decode(w.state()['data'])
            rows = w.params(text=False)
        by_name = {r['name']: r['value'] for r in rows}
        off = [n for n, v in live.items() if n in by_name and v is not None and abs(by_name[n] - float(v)) > 1e-4]
        unmatched = [n for n in live if n not in by_name]
        comp_same = _component(back) == _component(blob) and _component(blob) is not None
        report['stateCheck'] = {'controlsInWrapper': len(rows), 'exposedCompared': len(live) - len(unmatched), 'differ': off,
                                'notFoundByName': unmatched, 'audioStateBytesIdentical': comp_same}
        if off or not comp_same:
            raise RuntimeError('The state did not come back exactly inside Scrunch Host FX (%s).' % (
                ', '.join(off[:6]) if off else 'audio state bytes differ'))
        import stem_capture
        r = (rig or stem_capture.Rig)(adapter, {track: track})
        try:
            a1, _ = r.capture(start_beat, end_beat)
            a2, _ = r.capture(start_beat, end_beat)
            switch(device, False)
            switch(new_id, True)
            b, _ = r.capture(start_beat, end_beat)
        finally:
            cleanup = r.close()
        if isinstance(cleanup, dict) and cleanup.get('setTracksRestored') is False:
            raise RuntimeError('The capture tracks were not all removed (%s).' % cleanup)
        own, moved = _residual_db(a1[track], a2[track]), _residual_db(a1[track], b[track])
        if own is None or moved is None:
            raise RuntimeError('The track was silent over those beats, so the sound could not be compared.')
        limit = max(SOUND_FLOOR_DB, own + SOUND_MARGIN_DB)
        report['soundCheck'] = {'originalAgainstItselfDb': round(own, 1), 'wrapperAgainstOriginalDb': round(moved, 1), 'limitDb': round(limit, 1),
                                'beats': [start_beat, end_beat], 'passes': moved <= limit}
        if moved > limit:
            raise RuntimeError('Inside Scrunch Host FX it sounds different: %.1f dB against the original, where the original differs from '
                               'itself by %.1f dB.' % (moved, own))
    except Exception as exc:
        errors = []
        try:
            adapter.delete_device(new_id)
        except Exception as again:
            errors.append('the new Scrunch Host FX could not be taken out (%s)' % again)
        try:
            if was_on is not None:
                switch(device, was_on)
        except Exception as again:
            errors.append('the original\'s on/off could not be put back (%s)' % again)
        tail = ('; ' + '; '.join(errors) + '. Inspect the track.') if errors else '; the new Scrunch Host FX was taken out and the original is as it was.'
        raise RuntimeError('Not migrated: %s%s' % (exc, tail)) from exc
    report.update(wrapperDevice=new_id, instance=rec['instance'], child=hello.get('child'),
                  say=('%s is now inside Scrunch Host FX directly after the original, which stays in the chain switched off. State from %s; '
                       'sound checked over beats %g-%g (%.1f dB against the original; the original against itself %.1f dB).') % (
                      report['plugin'], report['stateSource']['kind'], start_beat, end_beat, moved, own))
    return report
