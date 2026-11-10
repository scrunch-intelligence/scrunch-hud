"""Validate one freshly read host graph before treating it as a synth instance.

Display names locate tracks; they never establish device identity. Producer Pal
2.2.0 derives device type from Live class_display_name, not the editable name.
Paths, object IDs, chain membership and parameter IDs must agree as well.
"""


def _id(value):
    ident = value.get('id')
    if not isinstance(ident, str) or not ident:
        raise ValueError('Instrument identity is incomplete: a host object ID is missing.')
    return ident


def parameters(device):
    rows = device.get('parameters')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Instrument identity is incomplete: parameter metadata is missing.')
    ids, names = set(), set()
    for row in rows:
        ident, name = _id(row), row.get('name')
        if not isinstance(name, str) or not name or ident in ids or name in names:
            raise ValueError('Instrument controls are ambiguous: duplicate parameter IDs or names.')
        ids.add(ident); names.add(name)
    return {p['name']: p for p in rows}


def validate(track_summary, track, rack, child):
    """Reject stale reads or lookalikes from a different position or parent."""
    ident = _id(track_summary)
    index = track_summary.get('trackIndex')
    if (type(index) is not int or index < 0 or track.get('trackIndex') != index
            or track.get('type') != 'midi' or _id(track) != ident
            or track.get('name') != track_summary.get('name')):
        raise ValueError('Track identity changed while reading the instrument.')
    devices = track.get('devices', [])
    if not devices or len({_id(d) for d in devices}) != len(devices):
        raise ValueError('Track device identity is missing or ambiguous.')
    matches=[(i,d) for i,d in enumerate(devices) if _id(d)==_id(rack)]
    if len(matches)!=1:raise ValueError('Rack is not present on the target track.')
    position,summary=matches[0];rack_path = f't{index}/d{position}'
    if (summary.get('path') != rack_path
            or rack.get('path') != rack_path or summary.get('type') != rack.get('type')
            or summary.get('name') != rack.get('name')):
        raise ValueError('Rack identity or its track position changed during inspection.')
    chains = rack.get('chains', [])
    matches=[(ci,di,c,d) for ci,c in enumerate(chains) for di,d in enumerate(c.get('devices',[])) if _id(d)==_id(child)]
    if len(matches)!=1:
        raise ValueError('Instrument rack membership is missing or ambiguous.')
    ci,di,chain,member=matches[0]
    if (chain.get('path') != rack_path + f'/c{ci}'
            or member.get('path') != rack_path + f'/c{ci}/d{di}'
            or child.get('path') != member.get('path')
            or _id(member) != _id(child) or member.get('type') != child.get('type')
            or member.get('name') != child.get('name')):
        raise ValueError('Synth identity or its rack membership changed during inspection.')
    object_ids = [_id(track), _id(rack), _id(chain), _id(child)]
    rack_params, child_params = parameters(rack), parameters(child)
    all_ids = object_ids + [p['id'] for p in rack_params.values()] + [p['id'] for p in child_params.values()]
    if len(set(all_ids)) != len(all_ids):
        raise ValueError('Instrument identity is ambiguous: a host ID is reused.')
    return rack_params, child_params
