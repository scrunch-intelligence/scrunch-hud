"""Serum 2's own installed content as construction assets: its wavetables, samples, noises, multisamples, impulse
responses, LFO shapes, arp patterns, MIDI clips, effect presets and effect racks.

Tables use the file-reference form Serum saves in its factory presets (path under Tables, frames, rate and channels
read from the file; 184 of 187 factory references match their files exactly, 27 Sep 2026). Effects are the rack slots
Serum stores in its .SerumFX / .SerumFXRack files; their effect type numbers match the slots in Serum's factory
presets. Samples, noises and impulse responses use the path forms Serum writes in its factory presets (samples
relative to Samples, noises relative to Samples/Factory Non-Tonal/Noises, impulses relative to Impulses) with frames,
rate and channels read from the file. Multisamples are referenced by their .sfz path under Multisamples; Serum reads
the file itself. Shapes, arp patterns and clips are the dictionaries Serum stores in its .XferShape, .XferArp and
.XferClip files. Files Serum's codec or the audio reader cannot decode are skipped and counted, never substituted.
"""
from functools import lru_cache
from pathlib import Path

SERUM_CONTENT = Path('/Library/Audio/Presets/Xfer Records/Serum 2 Presets')


@lru_cache(maxsize=2)
def installed_assets(root=SERUM_CONTENT):
    import soundfile
    from synth_state import serum2
    root = Path(root)
    tables, effects, racks, skipped = {}, {}, {}, []
    for f in sorted((root / 'Tables').rglob('*.wav')):
        if f.name.startswith('._'):
            continue
        try:
            info = soundfile.info(str(f))
        except RuntimeError:
            skipped.append(str(f))
            continue
        rel = str(f.relative_to(root / 'Tables'))
        tables['table:' + rel] = {'relativePathToWT': rel, 'numFrames': info.frames,
                                  'sampleRate': info.samplerate, 'numChannels': info.channels}
    chains = root / 'Effect Chains'
    for f in sorted(chains.rglob('*')):
        if f.name.startswith('._') or f.suffix not in ('.SerumFX', '.SerumFXRack'):
            continue
        try:
            doc = serum2.SerumCodec(f.read_bytes()).doc
        except (ValueError, KeyError):
            skipped.append(str(f))
            continue
        name = str(f.relative_to(chains).with_suffix('')).replace('/Factory/', '/')
        if f.suffix == '.SerumFX':
            kinds = [k for k, v in doc.items() if k.startswith('FX') and isinstance(v, dict)]
            if len(kinds) != 1 or not isinstance(doc.get('type'), int):
                skipped.append(str(f))
                continue
            effects['fx:' + name] = {kinds[0]: doc[kinds[0]], 'type': doc['type']}
        else:
            ids = []
            for i, slot in enumerate(doc.get('FXRack', {}).get('FX', [])):
                effects[f'rack:{name}/{i}'] = slot
                ids.append(f'rack:{name}/{i}')
            if ids:
                racks['rack:' + name] = ids
    audio = ('.wav', '.flac', '.aif', '.aiff')

    def files(folder, suffixes):
        return [f for f in sorted((root / folder).rglob('*')) if f.suffix.lower() in suffixes and not f.name.startswith('._')]

    def info(f):
        try:
            i = soundfile.info(str(f))
            return {'numFrames': i.frames, 'sampleRate': i.samplerate, 'numChannels': i.channels}
        except RuntimeError:
            skipped.append(str(f))
            return None

    samples, noises, multisamples, impulses, shapes, arps, clips = {}, {}, {}, {}, {}, {}, {}
    noise_root = root / 'Samples' / 'Factory Non-Tonal' / 'Noises'
    for f in files('Samples', audio):
        i = info(f)
        if i:
            samples['sample:' + str(f.relative_to(root / 'Samples'))] = {'samplePathRelative': str(f.relative_to(root / 'Samples')), **i}
            if f.is_relative_to(noise_root):
                rel = str(f.relative_to(noise_root))
                noises['noise:' + rel] = {'relativePathToNoiseSample': rel, **i}
    for f in files('Multisamples', ('.sfz',)):
        rel = str(f.relative_to(root / 'Multisamples'))
        multisamples['multisample:' + rel] = {'sfzPathRelative': rel}
    for f in files('Impulses', audio):
        i = info(f)
        if i:
            impulses['ir:' + str(f.relative_to(root / 'Impulses'))] = {'relativePathToIR': str(f.relative_to(root / 'Impulses')), **i}
    for folder, suffix, key, out, prefix in (('LFO Shapes', '.XferShape', 'LFO', shapes, 'shape:'),
                                             ('Arp Patterns', '.XferArp', 'ArpClip', arps, 'arp:'),
                                             ('Clips', '.XferClip', 'MidiClip', clips, 'clip:')):
        for f in files(folder, (suffix.lower(),)) + files(folder, (suffix,)):
            try:
                body = serum2.SerumCodec(f.read_bytes()).doc[key]
            except (ValueError, KeyError):
                skipped.append(str(f))
                continue
            out[prefix + str(f.relative_to(root / folder).with_suffix('')).replace('Factory/', '', 1)] = body
    return {'tables': tables, 'effects': effects, 'racks': racks, 'samples': samples, 'noises': noises,
            'multisamples': multisamples, 'impulses': impulses, 'shapes': shapes, 'arps': arps, 'clips': clips,
            'skipped': skipped}
