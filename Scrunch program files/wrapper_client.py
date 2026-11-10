"""Scrunch side of the Scrunch Host wrapper (native/wrapper): localhost JSON lines, per-instance token.

Each wrapper instance writes /tmp/scrunch-wrapper-<uid>/<instance>.json (mode 0600) with its port and
token and removes it when it closes. Parameters are addressed by the child's VST3 ParamID (string)
or its index; values are normalised 0-1.
"""
import itertools, json, os, socket, stat, time
from pathlib import Path

# Must match native/wrapper/src/Platform.cpp discoveryDirectory().
WINDOWS = os.name == 'nt'
DIRECTORY = (Path(os.environ.get('LOCALAPPDATA', '~')).expanduser() / 'Scrunch' / 'wrapper') if WINDOWS \
    else Path(f'/tmp/scrunch-wrapper-{os.getuid()}')


class WrapperError(RuntimeError):
    pass


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def instances(directory=DIRECTORY):
    """Discovery records of running wrapper instances (stale or foreign files are ignored)."""
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        return []
    # On Windows the directory sits in the user's own profile; POSIX owner/mode checks do not apply.
    if not stat.S_ISDIR(st.st_mode) or (not WINDOWS and (st.st_uid != os.getuid() or st.st_mode & 0o077)):
        raise WrapperError('Wrapper discovery directory is not private to this user')
    found = []
    for path in sorted(directory.glob('*.json')):
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                fst = os.fstat(fd)
                if fst.st_size > 8192 or (not WINDOWS and (fst.st_uid != os.getuid() or fst.st_mode & 0o077)):
                    continue
                record = json.loads(os.read(fd, 8192))
            finally:
                os.close(fd)
        except (OSError, ValueError):
            continue
        if record.get('version') == 1 and isinstance(record.get('port'), int) and _alive(record.get('pid', -1)):
            found.append(record)
    return found


class Wrapper:
    def __init__(self, record, timeout=30.0):
        self.record = record
        self.instance = record['instance']
        self._token = record['token']
        self._ids = itertools.count(1)
        self._sock = socket.create_connection(('127.0.0.1', record['port']), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buffer = b''

    def close(self):
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, op, timeout=None, **fields):
        request = {'op': op, 'id': next(self._ids), 'token': self._token, **fields}
        self._sock.settimeout(timeout or 30.0)
        self._sock.sendall(json.dumps(request, separators=(',', ':')).encode() + b'\n')
        while b'\n' not in self._buffer:
            chunk = self._sock.recv(1 << 20)
            if not chunk:
                raise WrapperError('Wrapper closed the connection')
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b'\n', 1)
        reply = json.loads(line)
        if reply.get('id') != request['id']:
            raise WrapperError('Wrapper reply out of order')
        if not reply.get('ok'):
            raise WrapperError(reply.get('error') or f'{op} failed')
        return reply

    def hello(self):
        return self.call('hello')

    def load(self, path):
        return self.call('load', path=path, timeout=90)

    def params(self, ids=None, text=True):
        fields = {'text': text}
        if ids is not None:
            fields['ids'] = list(ids)
        return self.call('list', timeout=60, **fields)['params']

    def set(self, changes):
        """changes: [{'id'|'index': ..., 'value': 0-1 | 'text': '...'}] -> reply with seq + readback."""
        return self.call('set', changes=list(changes))

    def measure(self, window=1.0, after_seq=None, settle_ms=150):
        fields = {'window': window, 'settle_ms': settle_ms}
        if after_seq:
            fields['after_seq'] = after_seq
        return self.call('measure', timeout=window + settle_ms / 1000 + 10, **fields)

    def capture(self, seconds=3.0, after_seq=None, settle_ms=150):
        """(interleaved float32 samples, rate, channels) of audio played since the latest change."""
        import array, base64
        fields = {'window': seconds, 'settle_ms': settle_ms}
        if after_seq:
            fields['after_seq'] = after_seq
        r = self.call('capture', timeout=seconds + settle_ms / 1000 + 10, **fields)
        if r.get('silent') or not r.get('pcm'):
            raise WrapperError('The synth is silent; is the track playing?')
        samples = array.array('f')
        samples.frombytes(base64.b64decode(r['pcm']))
        return samples, int(r['sampleRate']), int(r['channels'])

    def state(self, data=True):
        return self.call('state_get', data=data, timeout=60)

    def set_state(self, data):
        return self.call('state_set', data=data, timeout=60)

    def slots(self):
        return self.call('slots')['slots']

    def lanes(self):
        """Automation lanes held inside the wrapper: [{id, index, name, points: [[beat, value], ...]}]."""
        return self.call('lanes')['lanes']

    def set_lanes(self, lanes, replace=False):
        """Automate any control of the hosted synth: lanes = [{'id': ParamID, 'points': [[beat, 0-1], ...]}].
        Empty points removes that lane. Saved with the Set; played from Live's beat position."""
        return self.call('lanes_set', lanes=list(lanes), replace=replace)['lanes']

    def map_slots(self, mapping):
        """mapping: {slot_number: child param id or None}"""
        return self.call('slots_set', slots=[{'slot': s, 'id': i} for s, i in mapping.items()])['slots']


def connect(instance=None, child=None, pid=None, wait=0.0):
    """Connect to exactly one running wrapper, filtered by instance id, child name or host pid."""
    deadline = time.monotonic() + wait
    while True:
        matches = [r for r in instances()
                   if (instance is None or r['instance'] == instance)
                   and (child is None or r.get('child') == child)
                   and (pid is None or r.get('pid') == pid)]
        if len(matches) == 1:
            return Wrapper(matches[0])
        if time.monotonic() >= deadline:
            raise WrapperError(f'Expected one wrapper, found {len(matches)}')
        time.sleep(0.1)


def link(set_link_value, candidates=None, tries=3):
    """Bind a Live device to its wrapper socket without Live exposing identities.

    `set_link_value(v)` writes the wrapper's "Scrunch Link" parameter (Live parameter 1 of the device)
    through Live's API. The one wrapper whose link value then equals v is that device.
    """
    import random
    records = candidates if candidates is not None else instances()
    for _ in range(tries):
        v = round(random.uniform(0.05, 0.95), 4)
        set_link_value(v)
        time.sleep(0.15)
        hits = []
        for record in records:
            with Wrapper(record, timeout=5) as w:
                if abs(w.hello()['link'] - v) < 5e-4:
                    hits.append(record)
        if len(hits) == 1:
            return hits[0]
    raise WrapperError('Could not identify a unique wrapper for this device')


# ---- Migration helpers: Live Set VST3 state -> hosted-child state ----

def _juce_base64(data):
    """JUCE MemoryBlock::toBase64Encoding (its own format: '<size>.' + 6-bit little-endian groups)."""
    from synth_state.juce_state import encode64
    return encode64(data)


def juce_vst3_state(processor, controller=b''):
    """The blob JUCE's VST3 host accepts in setStateInformation, from a plug-in's raw
    component (ProcessorState) and controller (ControllerState) data, as Live stores them."""
    import struct
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n\n<VST3PluginState><IComponent>' + _juce_base64(processor)
           + '</IComponent>' + (('<IEditController>' + _juce_base64(controller) + '</IEditController>') if controller else '')
           + '</VST3PluginState>').encode()
    return struct.pack('<II', 0x21324356, len(xml)) + xml + b'\0'   # JUCE counts the text without its null


def live_vst3_states(als_path, uid_fields):
    """[(processor, controller)] for every VST3 device in a Live Set with this class UID (4 ints)."""
    import gzip, re
    text = gzip.open(als_path).read().decode()
    found = []
    for m in re.finditer(r'<Vst3Preset Id="\d+">(.*?)</Vst3Preset>', text, re.S):
        body = m.group(1)
        uid = [int(v) for v in re.findall(r'<Fields\.\d Value="(-?\d+)"', body.split('</Uid>')[0])]
        if uid != list(uid_fields):
            continue
        blob = lambda tag: bytes.fromhex(re.sub(r'\s', '', (re.search(rf'<{tag}>(.*?)</{tag}>', body, re.S) or [None, ''])[1]))
        found.append((blob('ProcessorState'), blob('ControllerState')))
    return found


def vst3_param_id(juce_id):
    """The VST3 ParamID JUCE publishes for a parameter string ID (String::hashCode, Studio One-compatible mask)."""
    h = 0
    for ch in juce_id:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return h & 0x7FFFFFFF


def live_page(slot_names):
    """[(ParamId, name)] for Live's ParameterList: 'Scrunch Link' then the 127 slots."""
    return [(vst3_param_id('link'), 'Scrunch Link')] + [(vst3_param_id(f'slot{i}'), n or f'Slot {i}') for i, n in enumerate(slot_names, 1)]
