"""Scrunch Host in the sound path: to the engineer, a wrapper device IS the synth it hosts.

`WrappedAdapter` sits in front of the Live bridge adapter. For a Scrunch Host device it reports the
hosted synth's name and every automatable control of that synth (281 on Diva, not Live's 128 page),
routes writes through the wrapper's audio-thread queue, and captures audio from inside the wrapper.
Every other device and call goes to the bridge unchanged.

Control records keep the bridge's shape. Continuous controls are 0..1; stepped controls are shown in
steps (0..n-1) so a whole-step write means one option. Ids are 'w:<VST3 ParamID>'.
"""
import time
import wrapper_client as wc
from daw_adapter import DeviceParameterConflict, DeviceParameterUnverified

HOST = 'Scrunch Host'
PREFIX = 'w:'
_BOUND = {}   # Live device id -> wrapper instance id (survives coach rebuilds; re-checked on use)


def is_host(device):
    return HOST in (device.get('deviceName'), device.get('name'))


class WrappedAdapter:
    def __init__(self, base):
        self._base = base
        self._open = {}      # instance id -> Wrapper connection
        self._cache = {}     # device id -> (time, child params) for the steps of one request
        self.last_seq = {}   # device id -> seq of the latest write
        self._names = {}     # device id -> (time, hosted synth name)
        self._reads = {}     # (call, args) -> (time, value) for READS
        self._base_devices = {}

    # Live round trips cost ~180 ms each and one request asks the same things several times, so session and
    # device listings are reused for a moment; anything that changes Live clears them at once.
    READS = {'session': 1.0, 'devices': 1.0, 'selected_track': 1.0}
    DEVICE_SECONDS = 30.0

    def __getattr__(self, name):
        attr = getattr(self._base, name)
        if not callable(attr):
            return attr
        if name in self.READS:
            def cached(*args, **kwargs):
                key = (name, args, tuple(sorted(kwargs.items())))
                hit = self._reads.get(key)
                if hit and time.time() - hit[0] < self.READS[name]:
                    return hit[1]
                value = attr(*args, **kwargs)
                self._reads[key] = (time.time(), value)
                return value
            return cached
        if name in ('transport', 'connect', 'heartbeat', 'track', 'clip', 'device_parameter', 'document_path', 'supports'):
            return attr
        def changing(*args, **kwargs):          # a write or anything unknown: forget what was read
            self._reads.clear(); self._base_devices.clear()
            return attr(*args, **kwargs)
        return changing

    def _base_device(self, ident, parameters):
        """Live's record of a device. A bound wrapper's name/class never change, so that record is reused."""
        if not parameters:
            hit = self._base_devices.get(ident)
            if hit and time.time() - hit[0] < self.DEVICE_SECONDS:
                return dict(hit[1])
        d = self._base.device(ident, parameters)
        if not parameters:
            self._base_devices[ident] = (time.time(), dict(d))
        return d

    # ---- binding ----
    def wrapper(self, device):
        """The wrapper connection for this Live device, bound by the Scrunch Link handshake."""
        records = {r['instance']: r for r in wc.instances()}
        instance = _BOUND.get(device)
        if instance not in records:
            if not records:
                raise ValueError('Scrunch Host on this track is not running.')
            params = self._base.device(device)['parameters']
            link = next((p for p in params if p['name'].strip() == 'Scrunch Link'), None)
            if link is not None:
                rec = wc.link(lambda v: self._base.set_device_parameter(device, link['id'], link['name'], value=v),
                              list(records.values()))
            elif len(records) == 1 and len([d for t in self._base.session()['tracks'] for d in self._base.devices(t['id']) if is_host(d)]) == 1:
                rec = next(iter(records.values()))
            else:
                rec = self._bind_by_power(device, params, records)
            instance = _BOUND[device] = rec['instance']
        conn = self._open.get(instance)
        if conn is None:
            conn = self._open[instance] = wc.Wrapper(records[instance])
        return conn

    def _bind_by_power(self, device, params, records):
        """Live is not showing the link control (a newly added wrapper): switch the device off for a moment.

        Live stops running a switched-off plug-in, so the one wrapper whose audio stops is this device.
        Costs about half a second of silence on that track; it is switched back on straight away.
        """
        power = next((p for p in params if p['name'] == 'Device On'), None)
        if power is None or not power.get('value'):
            raise ValueError('Scrunch Host on this track is switched off, so Scrunch cannot tell which wrapper it is.')
        self._base.set_device_parameter(device, power['id'], 'Device On', value=0.0)
        try:
            time.sleep(0.45)
            stopped = []
            for rec in records.values():
                with wc.Wrapper(rec, timeout=5) as w:
                    if not w.hello().get('processing'):
                        stopped.append(rec)
        finally:
            self._base.set_device_parameter(device, power['id'], 'Device On', value=1.0)
        if len(stopped) != 1:
            raise ValueError('Could not tell which Scrunch Host this track holds (%d wrappers went quiet).' % len(stopped))
        return stopped[0]

    def _child(self, device, fresh=False):
        hit = self._cache.get(device)
        if hit and not fresh and time.time() - hit[0] < 0.5:
            return hit[1]
        params = [p for p in self.wrapper(device).params() if p.get('automatable')]
        self._cache[device] = (time.time(), params)
        return params

    @staticmethod
    def _labels(params):
        seen, out = {}, []
        for p in params:
            label = ((p.get('group') or '') + ' ' + p['name']).strip()
            seen[label] = seen.get(label, 0) + 1
            out.append(label if seen[label] == 1 else '%s %d' % (label, seen[label]))
        return out

    @staticmethod
    def _record(p, label, device):
        steps = int(p.get('steps') or 0)
        stepped = steps > 1
        value = round(p['value'] * (steps - 1)) if stepped else p['value']
        return {'id': PREFIX + str(p['id']), 'index': p['index'], 'name': label, 'originalName': p['name'],
                'value': float(value), 'min': 0.0, 'max': float(steps - 1) if stepped else 1.0,
                'defaultValue': round(p['default'] * (steps - 1)) if stepped else p['default'],
                'quantized': stepped, 'enabled': True, 'state': 'enabled', 'automation': 'none',
                'display': p.get('text'), 'deviceId': device, 'group': p.get('group'), 'steps': steps}

    def _records(self, device, fresh=False):
        params = self._child(device, fresh)
        return [self._record(p, l, device) for p, l in zip(params, self._labels(params))]

    def _child_name(self, device):
        hit = self._names.get(device)
        if hit and time.time() - hit[0] < 2.0:
            return hit[1]
        name = self.wrapper(device).hello().get('child') or HOST
        self._names[device] = (time.time(), name)
        return name

    # ---- reads ----
    def devices(self, track, parameters=False):
        out = []
        for d in self.__getattr__('devices')(track, parameters):
            if is_host(d):
                try:
                    d = dict(d, deviceName=self._child_name(d['id']), host=HOST)
                    if parameters:
                        d['parameters'] = [d['parameters'][0]] + self._records(d['id'])
                except (ValueError, OSError, wc.WrapperError):
                    pass     # not bound yet: stays a plain Scrunch Host until it answers
            out.append(d)
        return out

    def device(self, ident, parameters=True):
        # A bound wrapper's controls come from the wrapper itself: skip reading Live's 129-knob page.
        d = self._base_device(ident, parameters and ident not in _BOUND)
        if not is_host(d):
            return d
        d = dict(d, deviceName=self._child_name(ident), host=HOST)
        if parameters:
            on = [p for p in d.get('parameters', [])[:1] if p['name'] == 'Device On']
            d['parameters'] = on + self._records(ident, fresh=True)
        return d

    def device_parameter(self, device, ident):
        if not str(ident).startswith(PREFIX):
            return self._base.device_parameter(device, ident)
        return next(p for p in self._records(device, fresh=True) if p['id'] == ident)

    # ---- writes ----
    def set_device_parameter(self, device, ident, name, value=None, option=None, expect_value=None):
        if not str(ident).startswith(PREFIX):
            self._reads.clear()
            return self._base.set_device_parameter(device, ident, name, value=value, option=option, expect_value=expect_value)
        if option is not None:
            raise DeviceParameterConflict('Hosted controls take a value, not a named option.')
        write = {'id': ident, 'name': name, 'value': value}
        if expect_value is not None:
            write['expect_value'] = expect_value
        return self.set_device_parameters_together(device, [write])[0]

    def set_device_parameters_together(self, device, writes, confirm=True):
        if not any(str(w['id']).startswith(PREFIX) for w in writes):
            self._reads.clear()
            return self._base.set_device_parameters_together(device, writes, confirm)
        current = {p['id']: p for p in self._records(device, fresh=True)}
        changes = []
        for w in writes:
            p = current.get(w['id'])
            if p is None or p['name'] != w['name']:
                raise DeviceParameterConflict('%r is not a control of this synth any more; nothing was written.' % w['name'])
            if not p['min'] <= w['value'] <= p['max']:
                raise DeviceParameterConflict('%r is outside %r\'s range; nothing was written.' % (w['value'], w['name']))
            if w.get('expect_value') is not None and abs(p['value'] - w['expect_value']) > (0.5 if p['quantized'] else 1e-4):
                raise DeviceParameterConflict('%r moved since it was read (now %s); nothing was written.' % (w['name'], p['display']))
            normal = w['value'] / p['max'] if p['quantized'] else w['value']
            changes.append({'id': w['id'][len(PREFIX):], 'value': normal})
        reply = self.wrapper(device).set(changes)
        self.last_seq[device] = reply['seq']
        after = {p['id']: p for p in self._records(device, fresh=True)}
        out = []
        for w in writes:
            p = after[w['id']]
            if abs(p['value'] - w['value']) > (0.5 if p['quantized'] else 1e-4):
                raise DeviceParameterUnverified('%r reads back %s, not %r.' % (w['name'], p['display'], w['value']))
            out.append(dict(p, previousValue=current[w['id']]['value'], requested=w['value']))
        return out

    def hosts(self, device):
        """True when this Live device is a Scrunch Host."""
        return device in _BOUND or is_host(self._base_device(device, False))

    # ---- hearing ----
    def capture(self, device, seconds):
        """Audio from inside the wrapper, only from after the latest change to this synth."""
        try:
            # The wrapper never returns audio from before its latest applied write, so this is the
            # sound as it is now, waiting only if a change has just been made.
            return self.wrapper(device).capture(seconds)
        except wc.WrapperError as exc:
            raise ValueError(str(exc)) from None


_WRAPPED = {}


def wrap(adapter):
    """One WrappedAdapter per bridge adapter, so wrapper connections and bindings are reused."""
    if isinstance(adapter, WrappedAdapter):
        return adapter
    if id(adapter) not in _WRAPPED or _WRAPPED[id(adapter)]._base is not adapter:
        _WRAPPED[id(adapter)] = WrappedAdapter(adapter)
    return _WRAPPED[id(adapter)]
