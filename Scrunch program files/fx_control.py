"""Any VST3 effect in Scrunch Host FX: discover, write with readback, snapshot and restore exactly.

Nothing here is written for one plug-in. Everything comes from the effect itself: its VST3 parameter ids,
names, unit groups, step counts, its own bypass flag, and its display text sampled read-only across each
control's whole range (the wrapper's `describe` op never moves a control to read it). Units are read
from that display text with a units table (Hz, dB, ms, %, :1 ...), never from what a control is called.
A control whose display gives no unit keeps dimension None.

Writes follow spec section 7: the control is resolved by VST3 id, every automatable control is read
before the write and compared with what Scrunch last verified (a control moved by hand refuses the write
and names it), the write goes through the audio-thread queue, and success is claimed only on readback.
Restore puts the whole saved effect state back and verifies every control value, plus the state bytes.
"""
from __future__ import annotations

import contextlib, json, math, re, time
from datetime import datetime, timezone
from pathlib import Path

import engine

INVENTORY_DIR = engine.DATA / 'fx-inventory'
VALUE_TOLERANCE = 1e-6
RESTORE_SETTLE_S = 2.0      # longest a restore may take to show its values before it is judged

# Display units -> (dimension, factor to the dimension's base unit). Symbols only.
UNITS = {
    'hz': ('frequency', 1.0), 'khz': ('frequency', 1000.0),
    'db': ('level', 1.0),
    'ms': ('time', 0.001), 's': ('time', 1.0), 'sec': ('time', 1.0),
    '%': ('proportion', 0.01),
    ':1': ('ratio', 1.0),
    'x': ('multiplier', 1.0),
    'db/oct': ('slope', 1.0),
    'semi': ('pitch', 1.0), 'st': ('pitch', 1.0), 'cents': ('pitch', 0.01), 'ct': ('pitch', 0.01),
}
# How each dimension's base unit is labelled, and the unit symbols accepted for it (case-insensitive).
BASE_UNIT = {'frequency': 'Hz', 'level': 'dB', 'time': 's', 'proportion': 'fraction (1.0 = 100 %)', 'ratio': ':1',
             'multiplier': 'x', 'slope': 'dB/oct', 'pitch': 'semi'}
UNIT_LABEL = {'hz': 'Hz', 'khz': 'kHz', 'db': 'dB', 'ms': 'ms', 's': 's', 'sec': 's', '%': '%', ':1': ':1', 'x': 'x',
              'db/oct': 'dB/oct', 'semi': 'semi', 'st': 'semi', 'cents': 'cents', 'ct': 'cents'}


def units_for(dimension):
    """Unit symbols a number for this dimension may be given in ([''] for a control shown without a unit)."""
    if not dimension or dimension.startswith('unrecognised'):
        return ['']
    return sorted({UNIT_LABEL[u] for u, (d, _) in UNITS.items() if d == dimension})


def to_base(number, unit, dimension, name='control'):
    """A number given with an explicit unit -> the dimension's base unit. The unit must belong to the control's own
    dimension (a time is never read as a frequency or a percentage); a control shown without a unit takes unit ''."""
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
        raise ValueError('%s: the target must be a finite number' % name)
    if not isinstance(unit, str):
        raise ValueError('%s: give the unit as text, one of %s' % (name, ', '.join(repr(u) for u in units_for(dimension))))
    u = unit.strip().lower()
    if not dimension or dimension.startswith('unrecognised'):
        if u:
            raise ValueError('%s is shown without a unit Scrunch knows; give the number as it is shown, with unit ""' % name)
        return float(number)
    if u not in UNITS or UNITS[u][0] != dimension:
        raise ValueError('%s is a %s control; its unit must be one of %s, not %r' % (name, dimension, ', '.join(units_for(dimension)), unit))
    return float(number) * UNITS[u][1]


class Unreachable(ValueError):
    """The asked-for setting is outside what the control can show, or its display cannot come close enough to it.
    Nothing was written."""


_NUMBER = re.compile(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+)|[+-]?inf)\s*(.*)$', re.I)
_RATIO = re.compile(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*:\s*1$')


class HandMoved(RuntimeError):
    """A control changed since Scrunch last verified it: someone moved it. Nothing was written."""
    def __init__(self, moved):
        self.moved = moved
        names = ', '.join('%s (%s -> %s)' % (m['name'], m['expectedText'], m['nowText']) for m in moved[:6])
        super().__init__('Moved by hand since Scrunch last read it, so nothing was written: ' + names)


class Unverified(RuntimeError):
    """A write or restore did not read back as intended."""


class NotAllowed(PermissionError):
    """A write outside the allow-list the current move was given (spec 28). Nothing was written."""


def parse_display(text):
    """'1000.0 Hz' -> (1000.0, 'frequency', 1000.0 base units). Unknown or unitless -> dimension None."""
    t = (text or '').strip()
    m = _RATIO.match(t)
    if m:
        v = float(m.group(1))
        return v, 'ratio', v
    m = _NUMBER.match(t)
    if not m:
        return None, None, None
    v = float(m.group(1).replace('INF', 'inf').replace('Inf', 'inf'))
    unit = m.group(2).strip().lower()
    if not unit:
        return v, None, v
    if unit in UNITS:
        dim, k = UNITS[unit]
        return v, dim, v * k
    return v, 'unrecognised:' + unit, v


def _monotonic(numbers):
    pairs = [(a, b) for a, b in zip(numbers, numbers[1:]) if a is not None and b is not None and math.isfinite(a) and math.isfinite(b)]
    if len(pairs) < 2:
        return None
    if all(b >= a for a, b in pairs) and any(b > a for a, b in pairs):
        return 'increasing'
    if all(b <= a for a, b in pairs) and any(b < a for a, b in pairs):
        return 'decreasing'
    return None


def describe_control(p):
    """Inventory record for one control from the wrapper's `describe` row (pure; unit-tested)."""
    texts = p.get('sampleTexts') or []
    parsed = [parse_display(t) for t in texts]
    dims = [d for _, d, _ in parsed if d]
    dimension = None
    if dims and len(dims) >= 0.8 * len(texts) and len(set(dims)) == 1:
        dimension = dims[0]
    elif dims and len(set(dims)) == 1 and all(d == dims[0] or n is None for n, d, _ in parsed):
        dimension = dims[0]
    numeric = sum(n is not None for n, _, _ in parsed) >= 0.8 * max(1, len(texts))
    steps = int(p.get('steps') or 0)
    labels = list(p.get('labels') or [])
    options = None
    if steps == 2 or p.get('boolean'):
        kind = 'switch'
    elif steps > 2:
        kind = 'stepped'
    elif not numeric and len(set(texts)) > 1:
        kind = 'choice'           # continuous-coded choice: the display names plateaus
    else:
        kind = 'continuous'
    if kind in ('switch', 'stepped'):
        n = steps if steps > 1 else 2
        options = [{'value': k / (n - 1), 'text': labels[k] if k < len(labels) else None} for k in range(n)]
    elif kind == 'choice':
        options, last = [], None
        for v, t in zip(p.get('sampleValues') or [], texts):
            if t != last:
                options.append({'firstSeenAt': v, 'text': t}); last = t
    numbers = [b for _, _, b in parsed]          # in the quantity's base unit (Hz, dB, s, ratio, 1.0 = 100 %)
    finite = [n for n in numbers if n is not None and math.isfinite(n)]
    record = {
        'id': str(p['id']), 'index': p['index'], 'name': p['name'], 'group': p.get('group') or '',
        'automatable': bool(p.get('automatable')), 'bypassFlag': bool(p.get('bypass')),
        'value': p.get('value'), 'text': p.get('text'), 'default': p.get('default'),
        'steps': steps, 'discrete': bool(p.get('discrete')), 'boolean': bool(p.get('boolean')),
        'kind': kind, 'options': options,
        # False: the plug-in answered every "text for value v" with the current text (Valhalla does this);
        # its range can only be read by moving the control (EffectSession.discover(write_sweep=True)).
        'displayTracksValue': len(set(texts)) > 1 or steps > 1,
        'display': {'atMin': texts[0] if texts else None, 'atMax': texts[-1] if texts else None,
                    'samples': [[v, t] for v, t in zip(p.get('sampleValues') or [], texts)]},
        'quantity': None if not dimension else {
            'dimension': dimension, 'min': min(finite) if finite else None, 'max': max(finite) if finite else None,
            'monotonic': _monotonic(numbers), 'unit': 'base (Hz, dB, s, ratio, 1.0 = 100 %)', 'source': 'plug-in display text'},
    }
    if 'plainMin' in p:
        record['plain'] = {'min': p['plainMin'], 'max': p['plainMax']}
    return record


def slug(plugin):
    return re.sub(r'[^A-Za-z0-9]+', '-', '%s %s %s' % (plugin.get('manufacturer'), plugin.get('name'), plugin.get('version'))).strip('-')


class EffectSession:
    """One effect hosted in one Scrunch Host FX instance (a `wrapper_client.Wrapper`)."""

    def __init__(self, wrapper, inventory_dir=None):
        self.w = wrapper
        self.inventory_dir = Path(inventory_dir) if inventory_dir else INVENTORY_DIR
        self.inventory = None
        self.expected = None          # id -> value last verified by Scrunch (automatable controls)
        self.controls = {}            # id -> inventory record
        self.allowed = None           # while a move runs: the only controls it may write (spec 28)

    @contextlib.contextmanager
    def only(self, ids):
        """Structural write boundary for one move: inside, a write to any other control is refused."""
        before, self.allowed = self.allowed, {str(i) for i in ids}
        try:
            yield self
        finally:
            self.allowed = before

    # ---- discovery ----
    def discover(self, samples=20, save=True, write_sweep=False):
        """Inventory of every control. With write_sweep, controls whose display ignores the asked value are
        moved through their range to read it, then the whole effect is restored exactly (verified)."""
        d = self.w.call('describe', timeout=120, samples=samples)
        swept = []
        if write_sweep:
            blind = [p for p in d['params'] if p.get('automatable') and int(p.get('steps') or 0) < 2
                     and len(set(p.get('sampleTexts') or [])) <= 1 and not p.get('bypass')]
            if blind:
                snap_state = self.w.state(data=True)
                before = {str(p['id']): p['value'] for p in self.w.params(text=False) if p.get('automatable')}
                for p in blind:
                    texts = []
                    for v in p['sampleValues']:
                        self.w.set([{'id': str(p['id']), 'value': v}])
                        texts.append(self.w.params(ids=[str(p['id'])])[0]['text'])
                    p['sampleTexts'] = texts
                    swept.append(p['name'])
                self.w.call('state_restore', data=snap_state['data'], timeout=60)
                after = {str(p['id']): p['value'] for p in self.w.params(text=False)}
                off = [i for i, v in before.items() if abs(after.get(i, v) - v) > VALUE_TOLERANCE]
                if off:
                    names = {str(p['id']): p['name'] for p in d['params']}
                    raise Unverified('written display sweep did not restore: ' + ', '.join(names.get(i, i) for i in off))
        rows = [describe_control(p) for p in d['params']]
        for r in rows:
            if r['name'] in swept:
                r['displayTracksValue'] = False
                r['display']['source'] = 'written sweep, effect restored exactly'

        plugin = d['plugin']
        self.inventory = {'schemaVersion': 1, 'observedAt': datetime.now(timezone.utc).isoformat(),
                          'plugin': plugin, 'total': d['total'], 'automatable': sum(r['automatable'] for r in rows),
                          'bypassControl': next((r['id'] for r in rows if r['bypassFlag']), None),
                          'controls': rows, 'displaySweptByWriting': swept,
                          'source': 'Scrunch Host FX describe (read-only)' + (' + written display sweep' if swept else '')}
        self.controls = {r['id']: r for r in rows}
        self.expected = {r['id']: r['value'] for r in rows if r['automatable']}
        if save:
            self.inventory_dir.mkdir(parents=True, exist_ok=True)
            path = self.inventory_dir / (slug(plugin) + '.json')
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.inventory, indent=1))
            tmp.replace(path)
            self.inventory['path'] = str(path)
        return self.inventory

    def use_inventory(self, path):
        """Work from an inventory saved earlier (offline) instead of discovering again: nothing is swept or written.
        It must describe the plug-in loaded now - same name, maker, version and unique id, the same controls by id
        and name - or it is refused. The verified state to protect is the effect as it is now (read, not written)."""
        inv = json.loads(Path(path).read_text())
        d = self.w.call('describe', timeout=120, samples=2)
        off = [k for k in ('name', 'manufacturer', 'version', 'uid') if d['plugin'].get(k) != inv['plugin'].get(k)]
        loaded = {str(p['id']): p['name'] for p in d['params']}
        saved = {r['id']: r['name'] for r in inv['controls']}
        if off or loaded != saved:
            why = ', '.join('%s %r saved, %r loaded' % (k, inv['plugin'].get(k), d['plugin'].get(k)) for k in off)
            raise Unverified('the saved inventory is not this plug-in: ' + (why or 'controls differ (%d saved, %d loaded)' % (len(saved), len(loaded))))
        now = {str(p['id']): p['value'] for p in d['params']}
        self.inventory = dict(inv, path=str(path))
        self.controls = {r['id']: r for r in inv['controls']}
        self.expected = {r['id']: now[r['id']] for r in inv['controls'] if r['automatable']}
        return {'path': str(path), 'observedAt': inv.get('observedAt'), 'controls': len(saved),
                'valuesDifferFromSaved': sum(abs(now[r['id']] - r['value']) > VALUE_TOLERANCE for r in inv['controls'])}

    def control(self, ident):
        return self.controls[str(ident)]

    # ---- reading ----
    def values(self, ids=None, text=False):
        rows = self.w.params(ids=[str(i) for i in ids] if ids is not None else None, text=text)
        return {str(p['id']): p for p in rows}

    def values_of(self, ids, text=False):
        """Read every control in `ids`; one that does not come back (empty or partial readback) is never taken as
        unchanged or as a match: it raises Unverified, naming it."""
        ids = [str(i) for i in ids]
        now = self.values(text=text)
        missing = [i for i in ids if i not in now or not isinstance(now[i].get('value'), (int, float))]
        if missing:
            raise Unverified('readback missing %d of %d controls: %s' % (len(missing), len(ids), ', '.join(
                self.controls.get(i, {}).get('name', i) for i in missing[:6])))
        return now

    def hand_moved(self):
        """Automatable controls whose value differs from what Scrunch last verified."""
        if self.expected is None:
            raise RuntimeError('discover() first: there is no verified state to protect')
        now = self.values_of(self.expected, text=True)
        moved = []
        for ident, want in self.expected.items():
            got = now[ident]
            if abs(got['value'] - want) > VALUE_TOLERANCE:
                moved.append({'id': ident, 'name': self.controls[ident]['name'], 'expected': want, 'now': got['value'],
                              'expectedText': self._text(ident, want), 'nowText': got.get('text')})
        return moved

    def accept_current(self):
        """Take the effect as it is now as the verified state (after the user has said a hand move stands)."""
        now = self.values_of(self.expected)
        self.expected = {i: now[i]['value'] for i in self.expected}

    def _text(self, ident, value):
        return self.w.call('texts', id=str(ident), values=[value])['texts'][0]

    def _refresh_expected(self):
        now = self.values_of(self.expected)
        self.expected = {i: now[i]['value'] for i in self.expected}
        return now

    # ---- text -> value (read-only inversion of the plug-in's own display) ----
    def value_for(self, ident, target, rounds=4, per_round=16):
        """Normalised value whose display is closest to `target`, in the quantity's base unit (Hz, dB, s, ratio,
        1.0 = 100 %; a plain number for a unitless display).

        Bisection on the plug-in's display text only; nothing is written. Needs a monotonic display."""
        c = self.control(ident)
        q = c.get('quantity') or {}
        samples = [(v, parse_display(t)[2]) for v, t in c['display']['samples']]
        mono = q.get('monotonic') or _monotonic([n for _, n in samples])
        if not mono:
            raise ValueError('%s has no monotonic numeric display to invert' % c['name'])
        sign = 1 if mono == 'increasing' else -1
        key = lambda n: sign * (n - target)        # <= 0: at or below the target, walking up the control
        lo, hi, best = 0.0, 1.0, None

        def narrow(points):
            nonlocal lo, hi, best
            for g, n, t in points:
                if n is not None and math.isfinite(n) and (best is None or abs(n - target) < abs(best[1] - target)):
                    best = (g, n, t)
            under = [g for g, n, _ in points if n is not None and key(n) <= 0]
            over = [g for g, n, _ in points if n is not None and key(n) >= 0]
            if under:
                lo = max(lo, max(under))
            if over:
                hi = min([g for g in over if g >= lo] or [hi])
        narrow([(v, n, t) for (v, n), (_, t) in zip(samples, c['display']['samples'])])
        for _ in range(rounds if c.get('displayTracksValue', True) else 0):
            if hi - lo < 1e-7:
                break
            grid = [lo + (hi - lo) * k / (per_round - 1) for k in range(per_round)]
            texts = self.w.call('texts', id=str(ident), values=grid)['texts']
            narrow([(g, parse_display(t)[2], t) for g, t in zip(grid, texts)])
        if best is None:
            raise ValueError('%s: no display value near %s' % (c['name'], target))
        return {'value': best[0], 'display': best[2], 'number': best[1]}

    def _seek_display(self, ident, target, steps=12):
        """For a plug-in that only shows the current value's text: move the control and read its display
        until it shows `target` (bisection inside the bracket from the inventory's written sweep).
        Called from write(), after the hand-moved check, so these moves are part of that one write."""
        c = self.controls[ident]
        pts = [(v, parse_display(t)[2]) for v, t in c['display']['samples']]
        pts = [(v, n) for v, n in pts if n is not None and math.isfinite(n)]
        mono = _monotonic([n for _, n in pts])
        if not mono:
            raise ValueError('%s has no monotonic display to seek' % c['name'])
        sign = 1 if mono == 'increasing' else -1
        lo = max([v for v, n in pts if sign * (n - target) <= 0] or [0.0])
        hi = min([v for v, n in pts if sign * (n - target) >= 0 and v >= lo] or [1.0])
        best = None
        for _ in range(steps):
            mid = (lo + hi) / 2
            self.w.set([{'id': ident, 'value': mid}])
            n = parse_display(self.w.params(ids=[ident])[0].get('text'))[2]
            if n is None:
                break
            if best is None or abs(n - target) < abs(best[1] - target):
                best = (mid, n)
            if sign * (n - target) < 0:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-6 or n == target:
                break
        return best[0] if best else lo

    def value_for_option(self, ident, text):
        c = self.control(ident)
        for o in c.get('options') or []:
            if o.get('text') == text and 'value' in o:
                return o['value']
        raise ValueError('%s has no option %r' % (c['name'], text))

    # ---- verified writes ----
    def reachable(self, ident, target):
        """Refuse (Unreachable) a base-unit target outside the control's measured display range. Read-only."""
        c = self.control(ident)
        q = c.get('quantity') or {}
        lo, hi = q.get('min'), q.get('max')
        if lo is None or hi is None:
            nums = [parse_display(t)[2] for _, t in c['display']['samples']]
            nums = [n for n in nums if n is not None and math.isfinite(n)]
            lo, hi = (min(nums), max(nums)) if nums else (None, None)
        if lo is None:
            raise Unreachable('%s has no numeric display range to check a target against' % c['name'])
        slack = 1e-9 * max(1.0, abs(lo), abs(hi))
        if not lo - slack <= target <= hi + slack:
            dim = (q.get('dimension') or '')
            raise Unreachable('%s goes from %s to %s; %g %s is outside it. Nothing was written.' % (
                c['name'], c['display']['atMin'], c['display']['atMax'], target, BASE_UNIT.get(dim, '(as shown)')))
        return lo, hi

    def close_enough(self, ident, target, got):
        """The display's nearest setting must land within 1 % of the target (and, except for frequency, within
        0.2 % of the control's span), or the target is refused as unreachable."""
        c = self.control(ident)
        lo, hi = self.reachable(ident, target)
        dim = (c.get('quantity') or {}).get('dimension')
        tol = max(0.01 * abs(target), 0.0 if dim == 'frequency' else 0.002 * (hi - lo), 1e-9)
        if got is None or abs(got - target) > tol:
            raise Unreachable('%s cannot be set to %g %s: the nearest the plug-in shows is %s. Nothing was written.' % (
                c['name'], target, BASE_UNIT.get(dim or '', '(as shown)'), 'nothing numeric' if got is None else '%g' % got))

    def resolve(self, ch):
        """One change -> {'id', 'value'} without writing, or {'id', 'seek': base target} for a display that can only be
        found by moving the control. A 'display' given with a 'unit' is converted from that unit; without one it is
        already in the quantity's base unit (Hz, dB, s, ratio, 1.0 = 100 %). Unreachable targets are refused."""
        ident = str(ch['id'])
        if ident not in self.controls:
            raise KeyError('unknown control %s' % ident)
        c = self.controls[ident]
        if len({'value', 'display', 'option'} & set(ch)) != 1:
            raise ValueError('%s: a change needs exactly one of value, display or option' % c['name'])
        if 'value' in ch:
            v = ch['value']
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
                raise Unreachable('%s: value %r is outside 0-1. Nothing was written.' % (c['name'], v))
            return {'id': ident, 'value': float(v)}
        if 'option' in ch:
            return {'id': ident, 'value': self.value_for_option(ident, ch['option'])}
        dim = (c.get('quantity') or {}).get('dimension')
        target = to_base(ch['display'], ch['unit'], dim, c['name']) if 'unit' in ch else float(ch['display'])
        self.reachable(ident, target)
        if not c.get('displayTracksValue', True):
            return {'id': ident, 'seek': target}
        best = self.value_for(ident, target)
        self.close_enough(ident, target, best['number'])
        return {'id': ident, 'value': best['value']}

    def write(self, changes, protect=True):
        """changes: [{'id', 'value' (0-1) | 'display' (+ optional 'unit'; see resolve) | 'option' (label)}].

        Every change is resolved and checked before anything moves, so one bad target writes nothing.

        Returns the seq to measure after, the readback of every written control, and any other control the
        plug-in itself moved in response (reported, never hidden)."""
        outside = [str(ch['id']) for ch in changes if self.allowed is not None and str(ch['id']) not in self.allowed]
        if outside:
            raise NotAllowed('not on this move\'s allow-list, nothing written: ' + ', '.join(
                self.controls.get(i, {}).get('name', i) for i in outside))
        if protect:                   # before anything moves: seeking a display below writes as it goes
            moved = self.hand_moved()
            if moved:
                raise HandMoved(moved)
        plan = [self.resolve(ch) for ch in changes]          # read-only: refuses before anything moves
        resolved, sought = [], {}
        try:
            for r in plan:
                if 'seek' in r:                              # moves the control as it looks (part of this write)
                    sought.setdefault(r['id'], self.values_of([r['id']])[r['id']]['value'])
                    v = self._seek_display(r['id'], r['seek'])
                    shown = parse_display(self.w.params(ids=[r['id']])[0].get('text'))[2]
                    self.close_enough(r['id'], r['seek'], shown)
                    r = {'id': r['id'], 'value': v}
                resolved.append(r)
        except Exception as exc:
            if sought:                                       # put back what seeking moved, then say so
                self.w.set([{'id': i, 'value': v} for i, v in sought.items()])
                back = self.values_of(sought)
                off = [self.controls[i]['name'] for i, v in sought.items() if abs(back[i]['value'] - v) > VALUE_TOLERANCE]
                if off:
                    raise Unverified('%s; seeking moved %s and it did not go back exactly: inspect it' % (exc, ', '.join(off))) from exc
            raise
        before = dict(self.expected)
        reply = self.w.set(resolved)
        after = self.values_of(set(self.expected) | {r['id'] for r in resolved}, text=True)
        readback = []
        for r in resolved:
            got = after[r['id']]
            c = self.controls[r['id']]
            tol = 0.5 / max(1, c['steps'] - 1) if c['steps'] > 1 else VALUE_TOLERANCE
            if abs(got['value'] - r['value']) > tol:
                raise Unverified('%s reads back %s (%.6f), not %.6f' % (c['name'], got.get('text'), got['value'], r['value']))
            readback.append({'id': r['id'], 'name': c['name'], 'requested': r['value'], 'value': got['value'], 'text': got.get('text')})
        written = {r['id'] for r in resolved}
        self.expected = {i: after[i]['value'] for i in self.expected}
        followers = [{'id': i, 'name': self.controls[i]['name'], 'from': before[i], 'to': self.expected[i], 'text': after[i].get('text')}
                     for i in self.expected if i not in written and abs(self.expected[i] - before.get(i, self.expected[i])) > VALUE_TOLERANCE]
        return {'seq': reply['seq'], 'viaAudioThread': reply.get('viaAudioThread'), 'readback': readback, 'pluginMoved': followers}

    # ---- exact state ----
    def snapshot(self):
        s = self.w.state(data=True)
        now = self.values_of(self.controls, text=True)
        return {'data': s['data'], 'md5': s['md5'], 'size': s['size'], 'takenAt': time.time(),
                'values': {i: p['value'] for i, p in now.items()}, 'texts': {i: p.get('text') for i, p in now.items()}}

    def restore(self, snap, protect=True):
        """Put the effect back exactly as `snap`. Refuses if a control was moved by hand since the last verified
        write (names it); verifies every control value afterwards and reports whether the state bytes match."""
        if protect:
            moved = self.hand_moved()
            if moved:
                raise HandMoved(moved)
        r = self.w.call('state_restore', data=snap['data'], timeout=60)
        # A plug-in can hand its restored values to its parameters after setState returns (Pro-C 3 in Live does,
        # by tens of ms); read until they arrive or RESTORE_SETTLE_S passes, then judge what is actually there.
        t0 = time.monotonic()
        while True:
            now = self.values_of(set(snap['values']) | set(self.expected or {}), text=True)   # missing = unverified
            differ = [{'id': i, 'name': self.controls.get(i, {}).get('name', i), 'want': v, 'got': now[i]['value']}
                      for i, v in snap['values'].items() if abs(now[i]['value'] - v) > VALUE_TOLERANCE]
            auto_differ = [d for d in differ if self.controls.get(d['id'], {}).get('automatable')]
            if not auto_differ or time.monotonic() - t0 > RESTORE_SETTLE_S:
                break
            time.sleep(0.02)
        settled_ms = round((time.monotonic() - t0) * 1000)
        md5 = self.w.state(data=False)['md5']
        self.expected = {i: now[i]['value'] for i in (self.expected or {})}
        result = {'seq': r['seq'], 'valuesCompared': len(snap['values']), 'valuesDiffering': len(differ),
                  'automatableDiffering': len(auto_differ), 'differing': differ[:20], 'valuesArrivedAfterMs': settled_ms,
                  'stateBytesIdentical': md5 == snap['md5'], 'md5': md5}
        if auto_differ:
            raise Unverified('restore left %d controls different: %s' % (len(auto_differ), ', '.join(d['name'] for d in auto_differ[:6])))
        return result
