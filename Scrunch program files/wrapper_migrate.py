"""Move a VST3 synth in a Live Set into Scrunch Host, keeping its sound, exposed controls and automation.

Works on a copy of the Set (the source file is only read). The synth's PluginDevice element stays
where it is, with its own ids; only what it hosts changes:

- PluginDesc becomes Scrunch Host (from native/wrapper/live-device-template.xml), whose saved state
  holds the synth's path, its exact component + controller state from the Set, and the slot map.
- The synth's exposed controls keep their order, names and AutomationTarget ids, re-pointed at the
  wrapper slot that now carries that control, so automation envelopes keep playing the same control.
- "Scrunch Link" goes first on the page (fresh target ids), for the device <-> socket handshake.

Live has to open the result for it to take effect; verify with the wrapper's own readback.
"""
import binascii, copy, gzip, struct
import xml.etree.ElementTree as ET
from pathlib import Path
import plugin_knobs
import wrapper_client as wc

TEMPLATE = Path(__file__).resolve().parent / 'native/wrapper/live-device-template.xml'
EMPTY_ID = '-1'
EMPTY_VISUAL = '1073741823'


def _hex_block(data):
    h = binascii.hexlify(data).decode().upper()
    return '\n' + '\n'.join(h[i:i + 80] for i in range(0, len(h), 80)) + '\n'


def wrapper_state(child_path, child_state, slot_ids):
    """The blob Scrunch Host's setStateInformation reads (JUCE copyXmlToBinary of <ScrunchHost>)."""
    root = ET.Element('ScrunchHost', {'version': '1', 'childPath': child_path})
    cs = ET.SubElement(root, 'ChildState', {'size': str(len(child_state))})
    cs.text = wc._juce_base64(child_state)
    ET.SubElement(root, 'Slots').text = ','.join(slot_ids)
    xml = b'<?xml version="1.0" encoding="UTF-8"?>\n\n' + ET.tostring(root)
    return struct.pack('<II', 0x21324356, len(xml)) + xml + b'\0'   # JUCE counts the text without its null


def _vst3_preset(device):
    return device.find('PluginDesc/Vst3PluginInfo/Preset/Vst3Preset')


def track_device(doc, track, plugin, track_index=None):
    """The `plugin` device on the track called `track`. Real Sets repeat names ("Bass" twice), so with
    `track_index` (Live's track number, 0-based, as the bridge reports it) that exact track is used, and its
    name must still match."""
    if track_index is None:
        return plugin_knobs.device_on_track(doc, track, plugin)
    live = doc.find('LiveSet') if doc.tag == 'Ableton' else doc
    ordered = [t for t in live.find('Tracks') if t.tag in ('MidiTrack', 'AudioTrack', 'GroupTrack')]
    if not 0 <= track_index < len(ordered) or plugin_knobs.track_name(ordered[track_index]) != track:
        raise ValueError('Track %d is not %r in this Set; nothing was changed.' % (track_index + 1, track))
    devices = [d for d in ordered[track_index].iter('PluginDevice') if plugin_knobs._plugin_name(d) == plugin]
    if len(devices) != 1:
        raise ValueError('Track %r holds %d copies of %s; Scrunch works on one.' % (track, len(devices), plugin))
    return devices[0]


def migrate(source, dest, track, plugin, child_path=None, track_index=None):
    """Write `dest`: `source` with the `plugin` device on `track` hosted inside Scrunch Host. Returns a report."""
    source, dest = Path(source), Path(dest)
    if source.resolve() == dest.resolve():
        raise ValueError('Migration writes a copy; give a different destination.')
    child_path = child_path or f'/Library/Audio/Plug-Ins/VST3/{plugin}.vst3'
    if not Path(child_path).exists():
        raise ValueError(f'{child_path} is not installed.')
    doc = ET.fromstring(gzip.decompress(source.read_bytes()))
    device = track_device(doc, track, plugin, track_index)
    preset = _vst3_preset(device)
    if preset is None:
        raise ValueError(f'{plugin} on {track} is not a VST3 device; only VST3 synths can be hosted.')
    blob = lambda tag: binascii.unhexlify(''.join((preset.find(tag).text or '').split())) if preset.find(tag) is not None else b''
    processor, controller = blob('ProcessorState'), blob('ControllerState')
    if not processor:
        raise ValueError(f'{plugin} on {track} has no saved state in this Set; save it in Live first.')

    plist = device.find('ParameterList')
    exposed = sorted([c for c in plist if c.find('ParameterId').get('Value') != EMPTY_ID],
                     key=lambda c: int(c.find('VisualIndex').get('Value')))
    dropped = []
    if len(exposed) > 127:
        # The wrapper page holds 127 besides its link. Controls past that stay reachable through the
        # wrapper (every control is), but not as a Live knob: leave off ones nothing automates.
        pointed = {env.find('EnvelopeTarget/PointeeId').get('Value') for env in doc.iter('AutomationEnvelope')}
        targets = lambda c: {e.get('Id') for e in c.iter() if e.tag.endswith('Target')}
        free = [c for c in reversed(exposed) if not (targets(c) & pointed)]
        if len(exposed) - len(free) > 127:
            raise ValueError('More than 127 of its controls are automated; the wrapper page cannot carry them all.')
        for c in free[:len(exposed) - 127]:
            exposed.remove(c); dropped.append(c.find('ParameterName').get('Value'))
    slot_ids = [c.find('ParameterId').get('Value') for c in exposed]

    live = doc.find('LiveSet') if doc.tag == 'Ableton' else doc
    nxt_el = live.find('NextPointeeId')
    nxt = max([int(nxt_el.get('Value'))] + [i + 1 for i in plugin_knobs._pointee_ids(doc)])

    def fresh():
        nonlocal nxt
        nxt += 1
        return str(nxt - 1)

    template = ET.fromstring(TEMPLATE.read_bytes())
    # Hosted synth -> Scrunch Host: descriptor, source context, saved state.
    for tag in ('PluginDesc', 'SourceContext'):
        old, new = device.find(tag), copy.deepcopy(template.find(tag))
        device.insert(list(device).index(old), new); device.remove(old)
    for e in device.find('PluginDesc').iter():
        if e.tag.endswith('Target'):
            e.set('Id', fresh())
    new_preset = _vst3_preset(device)
    state = wrapper_state(child_path, wc.juce_vst3_state(processor, controller), slot_ids + [''] * (127 - len(slot_ids)))
    new_preset.find('ProcessorState').text = _hex_block(state)
    ctrl = new_preset.find('ControllerState')
    if ctrl is not None:
        new_preset.remove(ctrl)

    # Page: link first, then the synth's exposed controls on slots 1..n, same targets.
    entry = copy.deepcopy(exposed[0] if exposed else next(c for c in plist if c.tag == 'PluginFloatParameter'))
    for c in list(plist):
        plist.remove(c)
    link = copy.deepcopy(entry)
    link.find('ParameterName').set('Value', 'Scrunch Link')
    link.find('ParameterId').set('Value', str(wc.vst3_param_id('link')))
    link.find('ParameterValue/Manual').set('Value', '0')
    for e in link.iter():
        if e.tag.endswith('Target'):
            e.set('Id', fresh())
    rows = [link]
    for n, c in enumerate(exposed, 1):
        c.find('ParameterId').set('Value', str(wc.vst3_param_id(f'slot{n}')))
        rows.append(c)
    for visual, c in enumerate(rows):
        c.set('Id', str(visual))
        c.find('VisualIndex').set('Value', str(visual))
        plist.append(c)
    for n in range(len(rows), 128):            # the rest of the page stays hidden, as Live writes it
        c = copy.deepcopy(entry)
        c.set('Id', str(n))
        c.find('ParameterName').set('Value', f'Slot {n}')
        c.find('ParameterId').set('Value', EMPTY_ID)
        c.find('VisualIndex').set('Value', EMPTY_VISUAL)
        value = c.find('ParameterValue')
        for e in list(value):
            if e.tag == 'ModulationTarget':
                value.remove(e)
            elif e.tag.endswith('Target'):
                e.set('Id', fresh())
        plist.append(c)
    nxt_el.set('Value', str(nxt + 1))

    dest.write_bytes(gzip.compress(b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc)))
    return {'source': str(source), 'dest': str(dest), 'track': track, 'plugin': plugin, 'childPath': child_path,
            'processorBytes': len(processor), 'controllerBytes': len(controller), 'controlsCarried': len(exposed), 'offThePage': dropped,
            'slotIds': slot_ids, 'trackIndex': track_index}
