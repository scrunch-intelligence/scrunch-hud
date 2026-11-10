"""One-time migration: put a track's synth inside Scrunch Host in the running Set. No save, no reload.

This is a migration tool, not part of the live engineer path. It is the only place outside offline
analysis that reads the saved .als, and it only reads it: the synth's last saved state is the one
thing Live does not expose to a Remote Script, so it is taken from the file.

1. The track and synth are found in the open Set through the ScrunchBridge remote script.
2. The synth's saved component/controller state is read from the Set file (read-only).
3. The bridge loads Scrunch Host (VST3) onto the track through Live's browser.
4. The new wrapper loads the synth and receives that state; it is read back from the wrapper.
5. The original synth device is removed from the track (Live's Undo brings it back).

The Set is never saved and never reopened. Changes made to the synth after the last save are not in
the file, so they are not carried; the report says when the file was last saved.

    python3 host_migrate.py "6-Diva"            # track name (or --index N, 1-based; none = selected track)
"""
import base64, json, sys, time
from pathlib import Path
import wrapper_client as wc

HOST_URI = 'query:Plugins#VST3:Scrunch:Scrunch%20Host'


def _set_path(adapter):
    from urllib.parse import unquote, urlparse
    value = adapter.document_path() or ''
    return Path(unquote(urlparse(value).path) if value.startswith('file:') else value).resolve() if value else None


def saved_state(set_path, track, plugin, track_index=None):
    """(processor, controller) bytes of `plugin` on `track` in the saved Set. Read-only."""
    import binascii, gzip, xml.etree.ElementTree as ET
    import wrapper_migrate
    doc = ET.fromstring(gzip.decompress(Path(set_path).read_bytes()))
    try:
        device = wrapper_migrate.track_device(doc, track, plugin, track_index)
    except ValueError:
        device = wrapper_migrate.track_device(doc, track, plugin)
    preset = device.find('PluginDesc/Vst3PluginInfo/Preset/Vst3Preset')
    if preset is None:
        raise ValueError('%s on %s is not a VST3 device in the saved Set; only VST3 synths can be hosted.' % (plugin, track))
    blob = lambda tag: binascii.unhexlify(''.join((preset.find(tag).text or '').split())) if preset.find(tag) is not None else b''
    processor = blob('ProcessorState')
    if not processor:
        raise ValueError('%s on %s has no saved state in the Set file.' % (plugin, track))
    return processor, blob('ControllerState')


def _values(synth, blob):
    import synth_state
    try:
        return synth_state.open_state(synth, blob).values()
    except ValueError:
        return None


def host(adapter, track_name=None, track_index=None, wait=30.0, log=print):
    started = time.time()
    tracks = adapter.session()['tracks']
    if track_name is None and track_index is None:          # the track selected in Live
        sel = adapter.selected_track()
        sel = sel.get('id') if isinstance(sel, dict) else sel
        track_index = next((t.get('trackIndex') for t in tracks if t['id'] == sel), None)
    hits = [t for t in tracks if (track_index is not None and t.get('trackIndex') == track_index)
            or (track_index is None and t.get('name') == track_name)]
    if len(hits) != 1:
        raise ValueError('Found %d tracks matching %r in the open Set.' % (len(hits), track_name or track_index))
    track = hits[0]
    devices = adapter.devices(track['id'])
    if any('Scrunch Host' in (d.get('name'), d.get('deviceName')) for d in devices):
        return {'already': True, 'say': 'This track already has Scrunch Host.'}
    synths = [d for d in devices if d.get('role') == 'instrument']
    if len(synths) != 1 or synths[0].get('className') != 'PluginDevice':
        raise ValueError('Track %s needs exactly one VST3 synth to host (found %d instruments).' % (track['name'], len(synths)))
    synth = synths[0]
    plugin = synth.get('deviceName') or synth['name']
    child = '/Library/Audio/Plug-Ins/VST3/%s.vst3' % plugin
    if not Path(child).exists():
        raise ValueError('%s is not installed as VST3.' % child)
    set_path = _set_path(adapter)
    if set_path is None or not set_path.is_file():
        raise ValueError('The open Set has no file on disk, so its synth state cannot be read.')
    stamp = set_path.stat().st_mtime
    processor, controller = saved_state(set_path, track['name'], plugin, track.get('trackIndex'))
    log('Read %s state from the Set file saved %s (%d bytes).' % (plugin, time.ctime(stamp), len(processor)))

    before = {r['instance'] for r in wc.instances()}
    loaded = adapter.load_device(track['id'], 'Scrunch Host', shelf='plugins', uri=HOST_URI, after_device=synth['id'])
    log('Live loaded %s.' % loaded)
    now = next((t for t in adapter.session()['tracks'] if t['id'] == track['id']), None)
    if now is not None and now.get('name') != track['name']:     # Live renames an unnamed track after its device
        adapter.update_track(track['id'], name=track['name'])
    deadline = time.time() + wait
    fresh = []
    while time.time() < deadline and not fresh:
        fresh = [r for r in wc.instances() if r['instance'] not in before]
        time.sleep(0.2)
    if len(fresh) != 1:
        raise ValueError('Scrunch Host was placed but %d new wrappers answered; inspect the track.' % len(fresh))
    blob = wc.juce_vst3_state(processor, controller)
    with wc.Wrapper(fresh[0], timeout=90) as w:
        w.load(child)
        w.set_state(base64.b64encode(blob).decode())
        back = base64.b64decode(w.state()['data'])
        hello = w.hello()
        count = len(w.params(text=False))
    want, got = _values(plugin, blob), _values(plugin, back)
    differing = sorted(k for k in (want or {}) if (got or {}).get(k) != want[k]) if want is not None and got is not None else None
    if hello.get('child') != plugin:
        raise ValueError('The wrapper reports %r, not %s; inspect the track (Live Undo restores the old device).' % (hello.get('child'), plugin))
    if differing and len(differing) > 0.05 * len(want):
        raise ValueError('%d of %d saved %s settings read back differently inside Scrunch Host (%s); inspect the '
                         'track (Live Undo restores the old device).' % (len(differing), len(want), plugin, ', '.join(differing[:4])))
    still = [d for d in adapter.devices(track['id']) if d['id'] == synth['id']]
    if still:
        adapter.delete_device(synth['id'])
    after = adapter.devices(track['id'])
    unchanged = set_path.stat().st_mtime == stamp and _set_path(adapter) == set_path
    return {'track': track['name'], 'trackIndex': track.get('trackIndex'), 'plugin': plugin,
            'scrunchHost': loaded.get('id'), 'wrapperInstance': fresh[0]['instance'],
            'replacedByLive': not still, 'devicesNow': [d.get('name') for d in after],
            'controlsInHost': count, 'settingsCompared': len(want or {}), 'settingsDiffering': differing,
            'stateFromFileSaved': time.ctime(stamp), 'setFileUntouched': unchanged,
            'seconds': round(time.time() - started, 1),
            'say': '%s on %s is now inside Scrunch Host (%d controls). Set not saved or reopened. Its sound is the '
                   'last saved state (%s); edits made to %s after that save were not carried. Undo in Live brings '
                   'the old %s back.' % (plugin, track['name'], count, time.ctime(stamp), plugin, plugin)}


if __name__ == '__main__':
    import daw_remote_script
    args = sys.argv[1:]
    idx = None
    if args[:1] == ['--index']:
        idx, args = int(args[1]) - 1, args[2:]
    out = host(daw_remote_script.RemoteScriptAdapter(), ' '.join(args) or None, idx)
    print(json.dumps(out, indent=2))
