"""Read Standard MIDI Files into Scrunch notes, with no third-party packages.

Notes come out in the canonical form `music.py` validates: `p` MIDI pitch, `t` start and `d`
duration in quarter-note beats, `v` velocity. Each track, and each channel within a track, is
its own part, because a reference file often carries bass, lead and drums side by side and each
is labelled separately in the library.

Positions are in beats from the file's own PPQ, not seconds, so tempo changes do not move
notes. The first tempo and time signature are reported; later changes are counted and kept in
`tempoChanges`/`meterChanges` rather than silently dropped.
"""
import base64, binascii, json, struct

DRUM_CHANNEL = 9  # MIDI channel 10, zero-based


class MidiFileError(ValueError):
    pass


def _vlq(data, i):
    value = 0
    for _ in range(4):
        if i >= len(data):
            raise MidiFileError('A variable-length number runs past the end of the track.')
        b = data[i]; i += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, i
    raise MidiFileError('A variable-length number is longer than four bytes.')


def _chunks(data):
    i = 0
    while i + 8 <= len(data):
        kind = data[i:i + 4]; size = struct.unpack('>I', data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + size]
        if len(body) != size:
            raise MidiFileError('A chunk is shorter than its declared length.')
        yield kind, body
        i += 8 + size


def _track_events(body, emit_sysex=False):
    """(tick, kind, payload) for one MTrk, handling running status."""
    i = tick = 0; status = None
    while i < len(body):
        delta, i = _vlq(body, i); tick += delta
        if i >= len(body):
            raise MidiFileError('A track ends in the middle of an event.')
        b = body[i]
        if b == 0xFF:
            if i + 1 >= len(body):
                raise MidiFileError('A meta event is truncated.')
            meta = body[i + 1]; size, i = _vlq(body, i + 2)
            yield tick, 'meta', (meta, body[i:i + size]); i += size
            if meta == 0x2F:
                return
            continue
        if b in (0xF0, 0xF7):
            size, i = _vlq(body, i + 1)
            if i+size>len(body):raise MidiFileError('A system-exclusive event is truncated.')
            if emit_sysex:yield tick,'sysex',body[i:i+size]
            i += size; status = None
            continue
        if b & 0x80:
            status = b; i += 1
        elif status is None:
            raise MidiFileError('Running status is used before any status byte.')
        kind = status & 0xF0
        width = 1 if kind in (0xC0, 0xD0) else 2
        args = body[i:i + width]
        if len(args) != width:
            raise MidiFileError('A channel event is truncated.')
        i += width
        yield tick, 'channel', (kind, status & 0x0F, tuple(args))


def read(data):
    """Parse SMF bytes. Returns format, ppq, tempo, time signature and parts with notes."""
    if not isinstance(data, (bytes, bytearray)) or data[:4] != b'MThd':
        raise MidiFileError('This is not a Standard MIDI File.')
    chunks = list(_chunks(bytes(data)))
    if not chunks or chunks[0][0] != b'MThd':
        raise MidiFileError('The MIDI header is missing or truncated.')
    header = chunks[0][1]
    if len(header) < 6:
        raise MidiFileError('The MIDI header is too short.')
    fmt, ntracks, division = struct.unpack('>HHH', header[:6])
    if division & 0x8000:
        raise MidiFileError('SMPTE-timed MIDI files are not supported; export with PPQ timing.')
    if division == 0:
        raise MidiFileError('The MIDI file declares zero ticks per beat.')
    ppq = division
    tempos, meters, parts = [], [], []
    tracks = [body for kind, body in chunks[1:] if kind == b'MTrk']
    for index, body in enumerate(tracks):
        name = ''
        open_notes = {}; notes = {}; programs = {}; last_tick = 0
        for tick, kind, payload in _track_events(body):
            last_tick = tick
            if kind == 'meta':
                meta, value = payload
                if meta == 0x03 and not name:
                    name = value.decode('utf-8', 'replace').strip()
                elif meta == 0x51 and len(value) == 3:
                    tempos.append((tick, round(60000000 / int.from_bytes(value, 'big'), 6)))
                elif meta == 0x58 and len(value) >= 2:
                    meters.append((tick, value[0], 2 ** value[1]))
                continue
            ev, channel, args = payload
            if ev == 0xC0:
                programs.setdefault(channel, args[0])
            elif ev == 0x90 and args[1] > 0:
                open_notes.setdefault((channel, args[0]), []).append((tick, args[1]))
            elif ev == 0x80 or (ev == 0x90 and args[1] == 0):
                stack = open_notes.get((channel, args[0]))
                if stack:  # first on, first off: overlapping same-pitch notes stay distinct
                    start, vel = stack.pop(0)
                    if tick > start:
                        notes.setdefault(channel, []).append((args[0], start, tick - start, vel))
        for (channel, pitch), stack in open_notes.items():  # notes never switched off end with the track
            for start, vel in stack:
                if last_tick > start:
                    notes.setdefault(channel, []).append((pitch, start, last_tick - start, vel))
        for channel in sorted(notes):
            rows = sorted(notes[channel], key=lambda n: (n[1], n[0]))
            parts.append({'track': index, 'channel': channel, 'name': name, 'drums': channel == DRUM_CHANNEL,
                          'program': programs.get(channel),
                          'notes': [dict(p=p, t=round(s / ppq, 8), d=round(d / ppq, 8), v=v) for p, s, d, v in rows]})
    tempos.sort(); meters.sort()
    numerator, denominator = (meters[0][1], meters[0][2]) if meters else (4, 4)
    for part in parts:
        end = max(n['t'] + n['d'] for n in part['notes'])
        bar = numerator * 4 / denominator
        part['length'] = max(bar, -(-round(end, 6) // bar) * bar)
    return {'format': fmt, 'ppq': ppq, 'declaredTracks': ntracks,
            'tempo': tempos[0][1] if tempos else 120.0, 'tempoChanges': max(0, len(tempos) - 1),
            'timeSignature': [numerator, denominator], 'meterChanges': max(0, len(meters) - 1),
            'parts': parts}


def read_pasted(content):
    """MIDI that was pasted rather than dropped: raw SMF bytes, base64 SMF text, or a JSON note list.

    A JSON list must be `[{p,t,d,v}, ...]` in beats; it becomes one part in 4/4. Anything else is
    refused with a reason instead of being guessed at.
    """
    if isinstance(content, (bytes, bytearray)):
        if bytes(content[:4]) == b'MThd':
            return read(bytes(content))
        content = bytes(content).decode('utf-8', 'replace')
    text = str(content or '').strip()
    if not text:
        raise MidiFileError('Nothing was pasted.')
    if text[:1] in '[{':
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise MidiFileError('The pasted text looks like JSON but does not parse.') from None
        notes = value.get('notes') if isinstance(value, dict) else value
        if not isinstance(notes, list) or not notes:
            raise MidiFileError('Pasted JSON needs a list of {p,t,d,v} notes.')
        import music
        end = max(float(n.get('t', 0)) + float(n.get('d', 0)) for n in notes if isinstance(n, dict))
        length = max(4.0, -(-round(end, 6) // 4) * 4)
        clean = music.validate(notes, length)
        return {'format': None, 'ppq': None, 'declaredTracks': 1, 'tempo': None, 'tempoChanges': 0,
                'timeSignature': [4, 4], 'meterChanges': 0,
                'parts': [{'track': 0, 'channel': None, 'name': '', 'drums': False, 'program': None, 'notes': clean, 'length': length}]}
    try:
        raw = base64.b64decode(''.join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise MidiFileError('Pasted text is neither a note list nor base64 MIDI.') from None
    return read(raw)
