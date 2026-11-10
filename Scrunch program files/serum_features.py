"""Serum 2's full stored feature set, taken from the installed plug-in itself (28 Sep 2026).

Sources, all local: Serum's 626 factory presets (every stored module field and the values Serum itself writes there),
the enum declarations compiled into the Serum binary (complete option lists such as oscillator engines and warp
modes), the published-control map (which stored field each of the 543 published controls writes; measured by
tools/serum_control_map_proof.py) and the modulation-source probe (tools/serum_mod_sources_proof.py). Nothing here is
typed in by hand: a field or option exists because Serum wrote it or declares it. Whether a write takes effect is
proven separately by readback, render and save/reload (tools/serum_feature_proof.py).
"""
from functools import lru_cache
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOWLEDGE = ROOT / 'data' / 'knowledge'
EVIDENCE = ROOT / 'evidence' / 'serum-features-2026-09-28'
CONTENT = Path('/Library/Audio/Presets/Xfer Records/Serum 2 Presets')
BINARY = Path('/Library/Audio/Plug-Ins/VST3/Serum2.vst3/Contents/MacOS/Serum2')

MODULES = r'(Oscillator[0-4]|Env[0-3]|LFO[0-9]|ModSlot(?:[0-9]|[1-5][0-9]|6[0-3])|VoiceFilter[01]|RoutingSlot[0-6]|' \
          r'Global0|VoicePanel0|Macro[0-7]|Arp0|ArpClip(?:[0-9]|1[01])|MidiClip(?:[0-9]|1[01])|ClipPlayer0|' \
          r'PitchQuantizer0|LFOPointModBus(?:[0-9]|1[0-5])|RetriggerState0)'
ENGINES = ('WTOsc', 'SampleOsc', 'MultiSampleOsc', 'GranularOsc', 'SpectralOsc', 'NoiseOsc', 'SubOsc')
# Which engine each oscillator type stores its settings in, by Serum's own oscillator-type names.
ENGINE_OF_TYPE = {'kOsc_WT': 'WTOsc', 'kOsc_Sample': 'SampleOsc', 'kOsc_MultiSample': 'MultiSampleOsc',
                  'kOsc_Granular': 'GranularOsc', 'kOsc_Spectral': 'SpectralOsc'}


def kind_of(path):
    """'Oscillator0/SampleOsc0/plainParams/kParamWarp' -> 'Oscillator/SampleOsc';
    'FXRack1/FX/3/FXDelay/plainParams/kParamWet' -> 'FX/FXDelay'."""
    parts = [p for p in path.strip('/').split('/') if p != 'plainParams' and not p.startswith('kParam')]
    if parts and parts[0].startswith('FXRack'):
        return 'FX/' + parts[3] if len(parts) > 3 else 'FXRack'
    return '/'.join(re.sub(r'\d+$', '', p) for p in parts)


def _declarations():
    """Every enum declaration string compiled into Serum: option lists and ordered parameter lists, as written."""
    text = subprocess.run(['strings', '-n', '6', str(BINARY)], capture_output=True, text=True, check=True).stdout
    out = []
    for line in text.splitlines():
        if not re.match(r'^[A-Za-z_]\w*(?: *= *[\w +-]+)? *, *[A-Za-z_]\w*', line) or not re.search(r'= *0 *,|kCount|kNum', line):
            continue
        if 'kNumAutomatableParams' in line.split(',')[0]:
            continue                                   # private (non-modulatable) parameters start after these
        names = []
        for term in line.split(','):
            name = term.split('=')[0].strip()
            if name.startswith(('kNum', 'kCount', 'NumModes')):
                break                                  # count sentinels, not options
            if not re.fullmatch(r'[A-Za-z_]\w*', name):
                names = []
                break
            names.append(name)
        if len(names) > 1 and names not in out:
            out.append(names)
    return out


def _private_declarations():
    """Serum's private (stored, not automatable) parameter lists, in declaration order."""
    text = subprocess.run(['strings', '-n', '6', str(BINARY)], capture_output=True, text=True, check=True).stdout
    out = []
    for line in text.splitlines():
        if re.match(r'^kParam\w+ *= *kNumAutomatableParams *,', line):
            names = []
            for term in line.split(','):
                name = term.split('=')[0].strip()
                if name.startswith('kNum') or not re.fullmatch(r'kParam\w+', name):
                    break
                names.append(name)
            if names not in out:
                out.append(names)
    return out


def _automatable(decls):
    """Ordered automatable parameter lists (index = the parameter ID Serum stores in modulation routes)."""
    return [d for d in decls if d[0].startswith('kParam') and all(n.startswith('kParam') for n in d)]


def _survey():
    from synth_state import serum2
    fields = {}

    def note(kind, key, value):
        f = fields.setdefault(kind, {}).setdefault(key, {'options': set(), 'low': None, 'high': None, 'integer': True, 'count': 0})
        f['count'] += 1
        if isinstance(value, str):
            f['options'].add(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            f['low'] = value if f['low'] is None else min(f['low'], value)
            f['high'] = value if f['high'] is None else max(f['high'], value)
            f['integer'] &= float(value).is_integer()

    def walk(path, body):
        if not isinstance(body, dict):
            return
        if isinstance(body.get('plainParams'), dict):
            for k, v in body['plainParams'].items():
                note(kind_of(path), k, v)
        for k, v in body.items():
            if k == 'FX' and isinstance(v, list):
                for i, slot in enumerate(v):
                    for fx, b in slot.items():
                        if isinstance(b, dict):
                            walk(f'{path}/FX/{i}/{fx}', b)
            elif isinstance(v, dict) and k != 'plainParams':
                walk(f'{path}/{k}', v)

    for f in sorted((CONTENT / 'Presets').rglob('*.SerumPreset')):
        if not f.name.startswith('._'):
            for k, v in serum2.SerumCodec(f.read_bytes()).doc.items():
                walk(k, v)
    return fields


def _factory_routes():
    """(destination type, parameter name) -> parameter IDs Serum stored in factory modulation routes."""
    from synth_state import serum2
    routes = {}
    for f in sorted((CONTENT / 'Presets').rglob('*.SerumPreset')):
        if f.name.startswith('._'):
            continue
        for k, v in serum2.SerumCodec(f.read_bytes()).doc.items():
            if k.startswith('ModSlot') and isinstance(v, dict) and v.get('destModuleTypeString'):
                routes.setdefault(v['destModuleTypeString'], {}).setdefault(v['destModuleParamName'], set()).add(v['destModuleParamID'])
    return routes


def _destination_lists(fields, decls, routes):
    """Per module type, Serum's ordered automatable parameter list: the one declaration that places every factory
    route's parameter at the ID Serum stored for it, preferring the most factory fields when several fit."""
    autos = _automatable(decls)
    out, unresolved = {}, {}
    types = {k.split('/')[-1]: k for k in fields} | {t: t for t in routes}
    for t, kind in sorted(types.items()):
        pairs = {n: ids for n, ids in routes.get(t, {}).items()}
        fits = [d for d in autos if pairs and all(n in d and d.index(n) in ids for n, ids in pairs.items())]
        observed = set(fields.get(kind, {}))
        best = max((len(observed & set(d)) for d in fits), default=0)
        top = [d for d in fits if len(observed & set(d)) == best]
        if top and all(d == top[0] for d in top):
            out[t] = top[0]
        else:
            unresolved[t] = 'no factory routes' if not pairs else f'{len(top)} candidate lists'
    return out, unresolved


def build():
    decls = _declarations()
    fields = _survey()
    routes = _factory_routes()
    control_map = json.loads((EVIDENCE / 'control-map.json').read_text())
    engines_file = EVIDENCE / 'control-map-engines.json'
    published = {}
    for row in control_map['rows']:
        for s in row['stored']:
            if s['path'].startswith('/tags'):
                continue
            keys = list(s['after']) if s['path'].endswith('plainParams') and isinstance(s['after'], dict) else [s['path'].split('/')[-1]]
            for k in keys:
                published.setdefault(kind_of(s['path']), {})[k] = row['name']
    for row in (json.loads(engines_file.read_text())['rows'] if engines_file.exists() else []):
        for st in row['stored']:
            keys = list(st['after']) if st['path'].endswith('plainParams') and isinstance(st['after'], dict) else [st['path'].split('/')[-1]]
            for k in keys:
                published.setdefault(kind_of(st['path']), {}).setdefault(k, row['name'])
    published_options = {}
    steps_file = EVIDENCE / 'control-steps.json'
    for row in (json.loads(steps_file.read_text())['rows'] if steps_file.exists() else []):
        kind, key = kind_of(row['path']), row['path'].split('/')[-1]
        published_options.setdefault(kind, {})[key] = [v for _, v in row['options']]
    # FX filter types are the voice filters' types (FX filter factory values all fall inside that list).
    if 'VoiceFilter' in published_options:
        published_options.setdefault('FX/FXFilter', {})['kParamType'] = published_options['VoiceFilter'].get('kParamType')
    not_kept = {}
    sweep = EVIDENCE / 'sweep-options.json'
    for row in (json.loads(sweep.read_text())['rows'] if sweep.exists() else []):
        if row['result'].startswith('changed to'):
            not_kept.setdefault((row['kind'], row['field']), []).append(row['option'])
    # Settings Serum declares but no factory preset stores: attributed to the one module type whose factory fields
    # share the most names with that declaration (ties are left out), offered with no range, proven by readback.
    declared_only = {}
    kinds = {k.split('/')[-1]: k for k in fields}
    for names in _private_declarations():
        # A list whose positions match a module's factory modulation routes is that module's (the retrigger switches).
        routed = [t for t, pairs in routes.items() if t not in kinds and pairs
                  and all(n in names and names.index(n) in ids for n, ids in pairs.items())]
        if len(routed) == 1:
            kind = routed[0]
            fields.setdefault(kind, {})
        else:
            overlap = sorted(((len(set(names) & set(keys)), kind) for kind, keys in fields.items()), reverse=True)
            if not overlap or overlap[0][0] < 2 or (len(overlap) > 1 and overlap[1][0] == overlap[0][0]):
                continue
            kind = overlap[0][1]
        for n in names:
            if n not in fields[kind]:
                declared_only.setdefault(kind, []).append(n)
    catalogue = {kind: {} for kind in declared_only}
    for kind, keys in sorted(fields.items()):
        for key, f in sorted(keys.items()):
            options = sorted(f['options'])
            complete = False
            source = 'factory presets'
            homes = [d for d in decls if options and set(options) <= set(d)]
            engine = kind.split('/')[-1].replace('Osc', '')
            named = [d for d in homes if any(engine in o for o in d)] if '/' in kind else []
            if len(named) == 1:
                homes = named                          # the list that names this engine (granular vs spectral Y-axis)
            if homes:
                smallest = min(homes, key=len)
                if all(set(smallest) <= set(h) for h in homes):
                    options, complete = list(smallest), True
                    source = 'Serum enum declaration' if len(homes) == 1 else \
                        'smallest Serum enum declaration holding every factory value (others are supersets)'
            steps = published_options.get(kind, {}).get(key)
            if steps and set(options) <= set(steps):
                options, complete, source = list(steps), True, 'published control stepped through every option'
            entry = {'published': published.get(kind, {}).get(key), 'factoryUses': f['count']}
            refused = [o for o in options if o in not_kept.get((kind, key), ())]
            if refused:
                options = [o for o in options if o not in refused]
                entry['optionsSerumReplaces'] = refused
            if options:
                entry.update(options=options, optionsComplete=complete, optionsFrom=source)
            if f['low'] is not None:
                entry.update(factoryRange=[f['low'], f['high']], integer=f['integer'])
            catalogue.setdefault(kind, {})[key] = entry
        for key in declared_only.get(kind, []):
            entry = {'published': published.get(kind, {}).get(key), 'factoryUses': 0, 'declaredOnly': True}
            engine = kind.split('/')[-1].replace('Osc', '')
            if key == 'kParamYAxisAssignment':
                lists = [d for d in decls if d[0] == 'kYAxisNone' and any(engine in o for o in d)]
                if len(lists) == 1:
                    entry.update(options=lists[0], optionsComplete=True, optionsFrom='Serum enum declaration naming this engine')
            catalogue.setdefault(kind, {})[key] = entry
    sources = json.loads((EVIDENCE / 'mod-sources.json').read_text())
    labels = {r['label']: r['code'] for r in sources['rows'] if r['label']}
    unlabelled = [r['code'] for r in sources['rows'] if r['kept'] and not r['label']]
    destinations, unresolved = _destination_lists(fields, decls, routes)
    # Engines whose parameter list no single declaration pins down: the published Osc A control IDs in that engine
    # mode give each parameter's index (ID - 33, as for the wavetable engine), kept only when every factory route
    # to that engine agrees.
    engine_ids = {}
    for row in (json.loads(engines_file.read_text())['rows'] if engines_file.exists() else []):
        eng = ENGINE_OF_TYPE[row['engine']]
        for st in row['stored']:
            if f'/{eng}0' in st['path']:
                keys = list(st['after']) if isinstance(st['after'], dict) else [st['path'].split('/')[-1]]
                for k in keys:
                    engine_ids.setdefault(eng, {})[k] = int(row['id']) % 1000 - 33
    for eng, ids in engine_ids.items():
        order = sorted(ids, key=ids.get)
        if eng in unresolved and [ids[k] for k in order] == list(range(len(order))) and \
                all(k not in ids or ids[k] in v for k, v in routes.get(eng, {}).items() if ids.get(k, 0) < len(order)):
            destinations[eng] = order
            unresolved.pop(eng)
    no_effect = {}
    sweep = EVIDENCE / 'sweep-destinations.json'
    for row in (json.loads(sweep.read_text())['rows'] if sweep.exists() else []):
        if row.get('verdict', '').startswith('ROUTE FAILS'):
            kind = kind_of(row['destination']).split('/')[-1]
            key = row['destination'].split('/')[-1]
            if key not in no_effect.setdefault(kind, []):
                no_effect[kind].append(key)
    return {'plugin_version': control_map['plugin_version'], 'destinationParameters': destinations,
            'unresolvedDestinationTypes': unresolved, 'modulationHasNoEffect': no_effect,
            'binarySha256': hashlib.sha256(BINARY.read_bytes()).hexdigest(),
            'fields': catalogue, 'modSources': labels, 'unlabelledModSourceCodes': unlabelled}


@lru_cache(maxsize=1)
def catalogue():
    version = json.loads((EVIDENCE / 'control-map.json').read_text())['plugin_version']
    path = KNOWLEDGE / f'serum2-features-{version}.json'
    if path.exists():
        return json.loads(path.read_text())
    data = build()
    path.write_text(json.dumps(data, indent=1))
    return data


def field(path):
    """Catalogue entry for a stored field path, or None."""
    key = path.rstrip('/').split('/')[-1]
    return catalogue()['fields'].get(kind_of(path), {}).get(key)
