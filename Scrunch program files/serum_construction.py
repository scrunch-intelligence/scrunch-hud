"""Serum patch construction from explicit structures and locally observed assets.

No invented Serum enum IDs. Templates carry embedded assets as well as settings.
The host must read back structural changes before they are called verified.
"""
from copy import deepcopy
import hashlib
import json
import math
import re


def _finite(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Expected finite number in [{low}, {high}]')
    return value


INSTALLED = ('table:', 'fx:', 'rack:', 'sample:', 'noise:', 'multisample:', 'ir:', 'shape:', 'arp:', 'clip:')
FIELD = re.compile(r'(?:(Oscillator[0-4])/(?:(WTOsc|SampleOsc|MultiSampleOsc|GranularOsc|SpectralOsc|NoiseOsc|SubOsc)(\d)/)?'
                   r'|((?:Env|LFO|ModSlot|VoiceFilter|RoutingSlot|Global|VoicePanel|Macro|Arp|ArpClip|MidiClip|ClipPlayer|'
                   r'PitchQuantizer|LFOPointModBus|RetriggerState)\d+)/|(FXRack[0-2])/FX/(\d+)/(FX[A-Za-z0-9]+)/)plainParams/(kParam\w+)')


def _features():
    import serum_features
    return serum_features.catalogue()


def field_node(doc, path):
    """(plainParams dict owner, field key) for a stored field path; validates the module exists as addressed."""
    m = FIELD.fullmatch(path)
    if not m:
        raise ValueError(f'Not a stored field path: {path}')
    osc, engine, eidx, module, rack, slot, fx, key = m.groups()
    if osc:
        node = doc[osc]
        if engine:
            if eidx != osc[-1] or f'{engine}{eidx}' not in node:
                raise ValueError(f'{osc} has no {engine}{eidx}')
            node = node[f'{engine}{eidx}']
    elif module:
        if module not in doc:
            raise ValueError(f'No module {module}')
        node = doc[module]
    else:
        chain = doc[rack]['FX']
        if int(slot) >= len(chain) or fx not in chain[int(slot)]:
            raise ValueError(f'{rack} slot {slot} holds no {fx}')
        node = chain[int(slot)][fx]
    return node, key


def field_value(doc, path):
    """Stored value at a field path, or MISSING when Serum stores its default (it omits default values)."""
    node, key = field_node(doc, path)
    params = node.get('plainParams')
    return params.get(key, MISSING) if isinstance(params, dict) else MISSING


MISSING = '<Serum default: not stored>'


def check_field_value(path, value):
    import serum_features
    entry = serum_features.field(path)
    if entry is None:
        raise ValueError(f'{path}: Serum stores no such field for this module type')
    if 'options' in entry and entry.get('optionsComplete'):
        if value not in entry['options'] and not isinstance(value, (int, float)):
            raise ValueError(f'{path}: {value!r} is not one of Serum\'s options')
        if isinstance(value, str):
            return value
    if isinstance(value, str):
        if value in entry.get('options', []):
            return value
        raise ValueError(f'{path}: {value!r} is not a stored option')
    value = _finite(value, -1e9, 1e9)
    return float(value)


def destination(doc, path):
    """Serum's route identity for a stored field path (type, module ID, parameter ID, parameter name)."""
    field_node(doc, path)
    m = FIELD.fullmatch(path)
    osc, engine, _, module, rack, slot, fx, key = m.groups()
    kind = fx or engine or (osc and 'Oscillator') or re.sub(r'\d+$', '', module)
    ids = _features()['destinationParameters'].get(kind)
    if not ids or key not in ids:
        raise ValueError(f'{path}: not a modulatable parameter of {kind}')
    module_id = int(rack[-1]) * 100 + int(slot) if fx else int(re.search(r'\d+$', osc or module).group())
    return {'destModuleID': module_id, 'destModuleParamID': ids.index(key), 'destModuleParamName': key,
            'destModuleTypeString': kind}


def stored_settings():
    """Settings only the 'set' operation reaches (no published control writes them), per module type: Serum's own
    option names, or the range Serum's factory presets use. Settings with a published control are set through it."""
    out = {}
    for kind, keys in _features()['fields'].items():
        for key, e in keys.items():
            if e['published']:
                continue
            row = {'options': e['options']} if e.get('options') else {'factoryRange': e.get('factoryRange'), 'whole': e.get('integer')}
            out.setdefault(kind, {})[key] = row
    return out


class Library:
    """Explicitly supplied local patch documents; metadata alone is sent to the engineer."""
    def __init__(self, documents, installed=None):
        self.assets = {}
        self.sources = []
        self.destinations = []
        installed = installed or {}
        self.racks = dict(installed.get('racks', {}))
        for k, body in installed.get('tables', {}).items():
            self.assets[k] = ('WTFile', deepcopy(body))
        for k, slot in installed.get('effects', {}).items():
            self.assets[k] = ('FX', deepcopy(slot))
        for group, kind in (('samples', 'Sample'), ('noises', 'Noise'), ('multisamples', 'MultiSample'),
                            ('impulses', 'IR'), ('shapes', 'Shape'), ('arps', 'ArpClip'), ('clips', 'MidiClip')):
            for k, body in installed.get(group, {}).items():
                self.assets[k] = (kind, deepcopy(body))
        for label, doc in documents.items():
            for key, body in doc.items():
                if not isinstance(body, dict):
                    continue
                if re.fullmatch(r'Oscillator[0-2]|Env[0-3]|LFO[0-9]', key):
                    self.assets[f'{label}/{key}'] = (key, deepcopy(body))
                if re.fullmatch(r'Oscillator[0-2]', key):
                    wt = 'WTOsc' + key[-1]
                    if wt in body:
                        self.assets[f'{label}/{key}/{wt}'] = (wt, deepcopy(body[wt]))
                if re.fullmatch(r'FXRack[0-2]', key):
                    for i, fx in enumerate(body.get('FX', [])):
                        self.assets[f'{label}/{key}/FX/{i}'] = ('FX', deepcopy(fx))
                if re.fullmatch(r'ModSlot\d+', key) and body.get('destModuleParamName'):
                    src = body.get('source')
                    dst = {k: deepcopy(body[k]) for k in ('destModuleID', 'destModuleParamID',
                           'destModuleParamName', 'destModuleTypeString')}
                    if src and src not in self.sources:
                        self.sources.append(deepcopy(src))
                    if dst not in self.destinations:
                        self.destinations.append(dst)

    def describe(self):
        return {'assets': [{'id': k, 'kind': v[0], 'settings': v[1].get('plainParams'),
                            'effectType': next((n for n in v[1] if n.startswith('FX')), None) if v[0] == 'FX' else None,
                            'wavetable': v[1].get('relativePathToWT'),
                            'identitySha256': hashlib.sha256(json.dumps(v[1], sort_keys=True, default=str).encode()).hexdigest(),
                            'embeddedWavetable': 'embeddedWTData' in v[1]}
                           for k, v in self.assets.items() if not k.startswith(INSTALLED)],
                'installedWavetables': [k for k, v in self.assets.items() if v[0] == 'WTFile'],
                'installedEffects': [{'id': k, 'effectType': next((n for n in v[1] if n.startswith('FX')), None)}
                                     for k, v in self.assets.items() if k.startswith('fx:')],
                'installedEffectRacks': [{'id': k, 'effects': [next((n for n in self.assets[i][1] if n.startswith('FX')), None)
                                                               for i in ids]} for k, ids in self.racks.items()],
                'installedSamples': [k for k, v in self.assets.items() if v[0] == 'Sample'],
                'installedNoises': [k for k, v in self.assets.items() if v[0] == 'Noise'],
                'installedMultisamples': [k for k, v in self.assets.items() if v[0] == 'MultiSample'],
                'installedImpulses': [k for k, v in self.assets.items() if v[0] == 'IR'],
                'installedLfoShapes': [k for k, v in self.assets.items() if v[0] == 'Shape'],
                'installedArpPatterns': [k for k, v in self.assets.items() if v[0] == 'ArpClip'],
                'installedClips': [k for k, v in self.assets.items() if v[0] == 'MidiClip'],
                'modulationSourceLabels': sorted(_features()['modSources'], key=_features()['modSources'].get),
                'modulationHasNoEffect': _features()['modulationHasNoEffect'],
                'storedSettings': stored_settings(),
                'modulationSources': [{'id': str(i), 'storedSource': s} for i, s in enumerate(self.sources)],
                'modulationDestinations': [{'id': str(i), **d} for i, d in enumerate(self.destinations)],
                'operations': {
                    'oscillator': 'target Oscillator0..2, value an oscillator asset ID; copies architecture and assets',
                    'wavetable': 'target Oscillator0..2, value a WTOsc asset ID (replaces the whole wavetable state) or an '
                                 'installed table: ID (loads that Serum table; the oscillator keeps its own wavetable settings)',
                    'envelope': 'target Env0..3, value an Env asset ID; further ADSR edits use published controls',
                    'lfo': 'target LFO0..9, value an LFO asset ID',
                    'lfo_shape': 'target LFO0..9, value an installed shape: ID or {x: [0..1], y: [0..1], curves: [0..1]}; '
                                 'equal lengths, endpoints x=0,1',
                    'modulation': 'target ModSlot0..63, value {source: a modulation source label (or listed ID), '
                                  'aux: optional source label scaling the route, destination: a stored field path such as '
                                  '"Oscillator0/plainParams/kParamVolume" or "FXRack0/FX/2/FXDelay/plainParams/kParamWet" '
                                  '(or listed ID), amount: -100..100, bipolar: boolean}',
                    'set': 'target a stored field path "<Module>/[<Engine>/]plainParams/<field>" or '
                           '"FXRack<r>/FX/<slot>/<FXType>/plainParams/<field>" or "Macro<n>/name"; value a number or one '
                           'of the field options; use for settings no published control reaches (oscillator type, LFO '
                           'mode/type, envelope/LFO sync, filter key track, routing destination, polyphony, every '
                           'effect setting, modulation-slot curves and smoothing...)',
                    'sample': 'target Oscillator0..2 (after setting its kParamType to kOsc_Sample, kOsc_Granular or '
                              'kOsc_Spectral: an installed sample: ID; kOsc_MultiSample: a multisample: ID), Oscillator3 '
                              '(a noise: ID), or FXRack<r>/FX/<slot> holding a convolution (an ir: ID)',
                    'clip': 'target ArpClip0..11 (an arp: ID) or MidiClip0..11 (a clip: ID); replaces the pattern',
                    'remove_modulation': 'target ModSlot0..63, value null',
                    'fx_chain': 'target FXRack0..2, value ordered list of FX asset IDs: current-patch effects, installed fx: '
                                'presets, or a whole installed rack: ID expanded in place (empty removes the chain); '
                                'remove existing FX modulation routes first'},
                'limits': 'Assets are installed Serum content or supplied local patches; unknown assets, fields, options '
                          'and source labels are rejected. Unlabelled modulation source codes are not offered. '
                          'No sample synthesis or missing-asset substitution.'}

    def asset(self, identity, pattern):
        if identity not in self.assets or not re.fullmatch(pattern, self.assets[identity][0]):
            raise ValueError(f'Unknown or incompatible construction asset: {identity}')
        return deepcopy(self.assets[identity])


def apply(doc, operations, library):
    """All-or-nothing document transform; no file writes, rendering or provider calls."""
    out = deepcopy(doc)
    touched = []
    for op in operations:
        kind, target, value = op['operation'], op['target'], op['value']
        patterns = {'oscillator': r'Oscillator[0-2]', 'wavetable': r'Oscillator[0-2]',
                    'envelope': r'Env[0-3]', 'lfo': r'LFO[0-9]', 'lfo_shape': r'LFO[0-9]',
                    'modulation': r'ModSlot(?:[0-9]|[1-5][0-9]|6[0-3])',
                    'remove_modulation': r'ModSlot(?:[0-9]|[1-5][0-9]|6[0-3])', 'fx_chain': r'FXRack[0-2]',
                    'set': FIELD.pattern + r'|Macro[0-7]/name', 'sample': r'Oscillator[0-3]|FXRack[0-2]/FX/\d+',
                    'clip': r'(?:ArpClip|MidiClip)(?:[0-9]|1[01])'}
        if kind not in patterns or not re.fullmatch(patterns[kind], target) or target.split('/')[0] not in out:
            raise ValueError(f'Unknown construction operation or target: {kind} {target}')
        path = target
        if kind == 'set':
            if target.endswith('/name'):
                if not isinstance(value, str) or not 0 < len(value) <= 64:
                    raise ValueError('A macro name is 1..64 characters.')
                out[target.split('/')[0]]['name'] = value
            else:
                node, key = field_node(out, target)
                if not isinstance(node.get('plainParams'), dict):
                    node['plainParams'] = {}
                node['plainParams'][key] = check_field_value(target, value)
            touched.append(path)
            continue
        if kind == 'sample':
            asset_kind = library.assets.get(value, ('',))[0]
            if target.startswith('FXRack'):
                rack, _, slot = target.split('/')
                chain = out[rack]['FX']
                if int(slot) >= len(chain) or 'FXConv' not in chain[int(slot)] or asset_kind != 'IR':
                    raise ValueError('An impulse (ir:) loads into a convolution slot only.')
                own = chain[int(slot)]['FXConv']
                chain[int(slot)]['FXConv'] = {'plainParams': deepcopy(own.get('plainParams', 'default')), **library.asset(value, 'IR')[1]}
                path += '/FXConv'
            elif target == 'Oscillator3':
                own = out[target]['NoiseOsc3']
                # detuneFactor is Serum's own measurement of the loaded file; Serum writes it on load.
                out[target]['NoiseOsc3'] = {**{k: deepcopy(v) for k, v in own.items() if k == 'plainParams'},
                                            **library.asset(value, 'Noise')[1]}
                path += '/NoiseOsc3'
            else:
                import serum_features
                params = out[target].get('plainParams')
                osc_type = params.get('kParamType', 'kOsc_WT') if isinstance(params, dict) else 'kOsc_WT'
                engine = serum_features.ENGINE_OF_TYPE.get(osc_type)
                want = {'SampleOsc': 'Sample', 'GranularOsc': 'Sample', 'SpectralOsc': 'Sample', 'MultiSampleOsc': 'MultiSample'}.get(engine)
                if want is None or asset_kind != want:
                    raise ValueError(f'{target} is {osc_type}; set its kParamType to a sample engine that takes {asset_kind or value}.')
                child = engine + target[-1]
                own = out[target].get(child, {})
                body = library.asset(value, want)[1]
                out[target][child] = {**{k: deepcopy(v) for k, v in own.items() if k in ('plainParams', 'flex')}, **body}
                path += '/' + child
            touched.append(path)
            continue
        if kind == 'clip':
            clip_kind = 'ArpClip' if target.startswith('ArpClip') else 'MidiClip'
            body = library.asset(value, clip_kind)[1]
            # Pattern files may hold option settings as numbers; Serum stores them by name (index into its option list).
            import serum_features
            if isinstance(body.get('plainParams'), dict):
                for k, v in list(body['plainParams'].items()):
                    entry = serum_features.field(f'{target}/plainParams/{k}') or {}
                    if isinstance(v, (int, float)) and entry.get('optionsComplete') and float(v).is_integer() and 0 <= v < len(entry['options']):
                        body['plainParams'][k] = entry['options'][int(v)]
            out[target] = body
            touched.append(path)
            continue
        if kind in ('oscillator', 'envelope', 'lfo'):
            source, body = library.asset(value, patterns[kind])
            if kind == 'oscillator':
                # Child module suffixes belong to the receiving oscillator, not the donor.
                body = {re.sub(r'\d+$', target[-1], k) if re.fullmatch(r'.*Osc\d+', k) else k: v
                        for k, v in body.items()}
            out[target] = body
        elif kind == 'wavetable':
            child = 'WTOsc' + target[-1]
            if library.assets.get(value, ('',))[0] == 'WTFile':
                own = out[target].get(child, {})
                body = {'flex': deepcopy(own.get('flex', {})), **library.asset(value, 'WTFile')[1],
                        'plainParams': deepcopy(own.get('plainParams', 'default'))}
            else:
                _, body = library.asset(value, r'WTOsc[0-2]')
            out[target][child] = body
            path += '/' + child
        elif kind == 'lfo_shape' and isinstance(value, str):
            # An installed shape is Serum's own curve data, kept exactly (points, curves and loop point).
            out[target]['curveData'] = library.asset(value, 'Shape')[1]
            out[target]['pathData'] = {}
            path += '/curveData'
        elif kind == 'lfo_shape':
            x, y, curves = value['x'], value['y'], value['curves']
            if not 2 <= len(x) <= 256 or len(x) != len(y) or len(x) != len(curves):
                raise ValueError('LFO shape needs 2..256 equally sized point/curve arrays.')
            for v in x + y + curves:
                _finite(v, 0, 1)
            if x[0] != 0 or x[-1] != 1 or any(a > b for a, b in zip(x, x[1:])):
                raise ValueError('LFO x points must span 0..1 in order.')
            curve = out[target].setdefault('curveData', {})
            curve.update(numPoints=len(x) - 1, xVals=deepcopy(x), yVals=deepcopy(y), curveVals=deepcopy(curves))
            out[target]['pathData'] = {}
            path += '/curveData'
        elif kind == 'remove_modulation':
            if value is not None:
                raise ValueError('remove_modulation takes null.')
            out[target] = {'plainParams': 'default'}
        elif kind == 'modulation':
            labels = _features()['modSources']
            src, dst_id = str(value['source']), str(value['destination'])
            if src in labels:
                source = [labels[src], labels[value['aux']] if value.get('aux') else 0]
                if value.get('aux') and value['aux'] not in labels:
                    raise ValueError('Unknown auxiliary modulation source.')
            elif src in [str(i) for i in range(len(library.sources))] and not value.get('aux'):
                source = deepcopy(library.sources[int(src)])
            else:
                raise ValueError('Unknown modulation source.')
            if '/' in dst_id:
                dst = destination(out, dst_id)
            elif dst_id in [str(i) for i in range(len(library.destinations))]:
                dst = deepcopy(library.destinations[int(dst_id)])
                if dst['destModuleTypeString'].startswith('FX'):
                    raise ValueError('Address an effect parameter by its stored field path (FXRack<r>/FX/<slot>/...).')
            else:
                raise ValueError('Unknown modulation destination.')
            if not isinstance(value['bipolar'], bool):
                raise ValueError('bipolar must be a boolean.')
            out[target] = {**dst, 'source': source,
                           'plainParams': {'kParamAmount': float(_finite(value['amount'], -100, 100)),
                                           'kParamBipolar': float(value['bipolar'])}}
        elif kind == 'fx_chain':
            if not isinstance(value, list):
                raise ValueError('FX chain must be a list of observed effect assets.')
            value = [i for v in value for i in (library.racks[v] if v in library.racks else [v])]
            if len(value) > 16:
                raise ValueError('FX chain must be a list of up to 16 observed effect assets.')
            if any(isinstance(v, dict) and str(v.get('destModuleTypeString', '')).startswith('FX')
                   for k, v in out.items() if k.startswith('ModSlot')):
                raise ValueError('Remove existing FX modulation routes explicitly before rebuilding an FX chain.')
            out[target]['FX'] = [library.asset(v, 'FX')[1] for v in value]
            path += '/FX'
        touched.append(path)
    return out, list(dict.fromkeys(touched))


def read_path(doc, path):
    for key in path.split('/'):
        doc = doc[int(key)] if isinstance(doc, list) else doc[key]
    return doc


def equivalent(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equivalent(v, b[k]) for k, v in a.items())
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equivalent(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7)
    return a == b


def _empty(v):
    return v in ({}, []) or (isinstance(v, list) and all(_empty(x) for x in v))


def without_added_empties(want, got):
    """Serum adds empty default containers on load that effect files do not carry (27 Sep 2026: a Downsample
    distortion came back with flex=[{}, {}]). Only keys absent from the request whose value is empty are dropped."""
    if isinstance(want, dict) and isinstance(got, dict):
        return {k: without_added_empties(want.get(k), v) for k, v in got.items() if k in want or not _empty(v)}
    if isinstance(want, list) and isinstance(got, list) and len(want) == len(got):
        return [without_added_empties(a, b) for a, b in zip(want, got)]
    return got


def clip_notes(clip):
    return [{k: n.get(k) for k in ('noteNum', 'timeStamp', 'length', 'channel', 'attributes')} for n in (clip or {}).get('notes', [])]


def omitted_settings(want, got, at=()):
    """Paths of stored settings (inside plainParams) present in want but absent from Serum's readback."""
    out = []
    if isinstance(want, dict) and isinstance(got, dict):
        for k, v in want.items():
            if k == 'plainParams' and isinstance(v, dict):
                g = got.get(k) if isinstance(got.get(k), dict) else {}
                out += [at + (k, p) for p in v if p not in g]
            elif k in got:
                out += omitted_settings(v, got[k], at + (k,))
    elif isinstance(want, list) and isinstance(got, list) and len(want) == len(got):
        for i, (a, b) in enumerate(zip(want, got)):
            out += omitted_settings(a, b, at + (i,))
    return out


def drop_settings(value, paths):
    value = deepcopy(value)
    for p in paths:
        node = value
        for part in p[:-1]:
            node = node[part]
        node.pop(p[-1])
        if len(p) > 1 and p[-2] == 'plainParams' and not node:
            parent = value
            for part in p[:-2]:
                parent = parent[part]
            parent['plainParams'] = 'default'
    return value


FX_RUNTIME = ('lfophasor', 'lfo', 'kUIParamMixOrGain', 'kUIParamMixOrGainDimE', 'kUIParamMixOrGainHyper', 'flex')
FX_FROM_FILE = ('embeddedIR', 'numChannels', 'numFrames', 'sampleRate', 'sourceRate')


def without_fx_runtime(want, got):
    """An effect inserted without Serum's runtime state gets Serum's own on load (28 Sep 2026: chorus/flanger/phaser LFO
    phase, HyperDimension LFO table, the UI mix-or-gain flag; a convolution adds the impulse it reads from its file).
    Only those keys, and only when the request did not carry them, are dropped; type and settings must still match."""
    if not (isinstance(want, list) and isinstance(got, list) and len(want) == len(got)):
        return got
    out = []
    for w, g in zip(want, got):
        if not (isinstance(w, dict) and isinstance(g, dict)):
            out.append(g)
            continue
        g = {k: v for k, v in g.items() if k in w or k not in FX_RUNTIME}
        for k, body in list(g.items()):
            if k.startswith('FX') and isinstance(body, dict) and isinstance(w.get(k), dict):
                drop = FX_RUNTIME + (FX_FROM_FILE if k == 'FXConv' and 'relativePathToIR' in w[k] else ())
                g[k] = {kk: vv for kk, vv in body.items() if kk in w[k] or kk not in drop}
        out.append(g)
    return out


ENGINE_ASSET = re.compile(r'Oscillator\d/(?:SampleOsc|GranularOsc|SpectralOsc|MultiSampleOsc|NoiseOsc)\d|FXRack\d/FX/\d+/FXConv')


def readback(expected, actual, paths, parameters_after=()):
    """Compare what was written (expected document) with Serum's own saved state after loading it (actual).

    Returns (checks, unproven). Serum omits values equal to its defaults; each such write is listed in unproven as a
    (path, setting path or None) pair for the caller to prove by rendering the patch without it (bit-identical audio
    means it was Serum's default). Macro names are not in Serum's processor state; the caller checks them in the saved
    preset. Rules, each measured on Serum 2.0.19 (27-28 Sep 2026):
      - a loaded sample, multisample, noise or impulse must read back every field written; Serum adds what it reads
        from the file itself (regions, base note, measured detune, spectral curve), and a multisample may fill
        settings left at 'default' from its own .sfz;
      - some installed tables come back as a bare path (no frame count); path, warp and settings must still match;
      - a pattern's notes (pitch, start, length, channel, attributes) must match; Serum rewrites older automation
        and expression forms, drops editor layout, and names option settings stored as numbers;
      - Osc A's enabled flag is omitted at its default; it is confirmed through the published control.
    """
    checks, unproven = [], []
    enabled = {p['name']: p['value'] for p in parameters_after}
    for path in paths:
        try:
            if path.endswith('/name'):
                continue
            if FIELD.fullmatch(path):
                want, got = field_value(expected, path), field_value(actual, path)
                if got == MISSING and want != MISSING:
                    unproven.append((path, None))
                    checks.append(dict(path=path, verified=None, storedAsSerumDefault=True))
                else:
                    checks.append(dict(path=path, verified=equivalent(want, got)))
                continue
            want, got = read_path(expected, path), deepcopy(read_path(actual, path))
            if re.fullmatch(r'(?:ArpClip|MidiClip)\d+', path):
                wp, gp, named = want.get('plainParams'), got.get('plainParams'), {}
                if isinstance(wp, dict) and isinstance(gp, dict) and wp.keys() == gp.keys():
                    named = {k: [v, gp[k]] for k, v in wp.items() if isinstance(v, (int, float)) and isinstance(gp[k], str)}
                    wp = {k: (gp[k] if k in named else v) for k, v in wp.items()}
                checks.append(dict(path=path, verified=equivalent(clip_notes(want.get('clip')), clip_notes(got.get('clip')))
                                   and equivalent(wp, gp), serumNamedOptions=named))
                continue
            if ENGINE_ASSET.fullmatch(path):
                def same(k, v):
                    if k == 'plainParams' and isinstance(v, dict) and isinstance(got.get(k), dict):
                        # every written setting must read back; a multisample also fills others from its own .sfz
                        return all(kk in got[k] and equivalent(vv, got[k][kk]) for kk, vv in v.items())
                    return k in got and equivalent(v, got[k])
                ok = all(same(k, v) for k, v in want.items() if not (k == 'plainParams' and v == 'default'))
                checks.append(dict(path=path, verified=ok, serumAddedFromFile=sorted(set(got) - set(want))))
                continue
            if path == 'Oscillator0' and isinstance(want.get('plainParams'), dict):
                flag = want['plainParams'].get('kParamEnable')
                if flag is not None and isinstance(got.get('plainParams'), dict) and 'kParamEnable' not in got['plainParams'] \
                        and abs(enabled.get('A Enable', -1) - flag) < 1e-6:
                    got['plainParams']['kParamEnable'] = flag
            if path.split('/')[-1].startswith('WTOsc') and isinstance(want, dict) and isinstance(got, dict) \
                    and 'relativePathToWT' in want and 'embeddedWTData' not in want and 'numFrames' not in got:
                want = {k: v for k, v in want.items() if k not in ('numFrames', 'sampleRate', 'numChannels')}
            if re.fullmatch(r'FXRack\d/FX', path):
                got = without_fx_runtime(want, got)
            if equivalent(want, without_added_empties(want, got)):
                checks.append(dict(path=path, verified=True))
                continue
            omitted = omitted_settings(want, got)
            if omitted and equivalent(drop_settings(want, omitted), without_added_empties(want, got)):
                unproven += [(path, o) for o in omitted]
                checks.append(dict(path=path, verified=None, serumDroppedDefaults=['/'.join(map(str, o)) for o in omitted]))
                continue
            checks.append(dict(path=path, verified=False))
        except (KeyError, IndexError, TypeError, ValueError):
            checks.append(dict(path=path, verified=False))
    return checks, unproven


def without_writes(doc, unproven):
    """The document with the writes Serum did not store removed (for the bit-identical default proof)."""
    doc = deepcopy(doc)
    for path, setting in unproven:
        if setting is None:
            node, key = field_node(doc, path)
            node['plainParams'].pop(key)
            if not node['plainParams']:
                node['plainParams'] = 'default'
        else:
            parent = read_path(doc, path)
            for part in setting[:-1]:
                parent = parent[part]
            parent.pop(setting[-1])
    return doc
