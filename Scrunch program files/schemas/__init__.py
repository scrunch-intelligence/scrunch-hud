"""Offline contract validation. Never resolves references over the network."""
from functools import lru_cache
import json
import math
from pathlib import Path


@lru_cache(maxsize=1)
def _validators():
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    documents = {p.stem: json.loads(p.read_text()) for p in Path(__file__).parent.glob('*.json')}
    registry = Registry().with_resources(
        (d['$id'], Resource.from_contents(d)) for d in documents.values())
    validators = {}
    for name, document in documents.items():
        Draft202012Validator.check_schema(document)
        validators[name] = Draft202012Validator(document, registry=registry, format_checker=FormatChecker())
    return validators


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _changes(changes):
    _require(len({c['control_id'] for c in changes}) == len(changes), 'Duplicate control IDs.')
    for change in changes:
        mapping = change['mapping']
        if mapping['kind'] == 'range':
            low, high = mapping['minimum'], mapping['maximum']
            numeric = lambda v: isinstance(v, (float, int)) and not isinstance(v, bool)
            _require(numeric(low) and numeric(high) and low <= high and mapping['unit'] is not None,
                     'Trusted ranges require bounds and units (normalized is a unit).')
            _require(all(numeric(change[k]) and low <= change[k] <= high for k in ('before', 'after')),
                     'Control value outside trusted range.')
            delta = change['after']-change['before']
            _require(math.isclose(abs(delta), change['magnitude'], rel_tol=1e-9, abs_tol=1e-12),
                     'Magnitude must equal the recorded change in the declared unit.')
            _require(change['direction'] == 'set' or
                     (change['direction'] == 'increase' and delta > 0) or
                     (change['direction'] == 'decrease' and delta < 0), 'Direction contradicts values.')
        else:
            _require(bool(mapping['values']) and all(change[k] in mapping['values'] for k in ('before', 'after')),
                     'Control values must have a trusted value mapping.')
            _require(change['direction'] == 'set' and change['magnitude'] == 1,
                     'Categorical changes use set and magnitude one; further needs a numeric range.')


def _result(result):
    writes = result['writes']
    status = result['status']
    _require(len({w['control_id'] for w in writes}) == len(writes), 'Duplicate write controls.')
    if writes:
        _require(result['target_identity'] is not None and bool(result['start_revision']),
                 'Writes require exact target and starting revision.')
        _require(all(result['prewrite_checks'].values()), 'Writes require passing prewrite checks.')
        _require(result['restore_data'] is not None, 'Capture restore data before writing.')
        _require(result['restore_data']['state_revision'] == result['start_revision'],
                 'Restore data belongs to a different starting state.')
        for write in writes:
            restored = result['restore_data']['values']
            _require(write['control_id'] in restored and restored[write['control_id']] == write['before'],
                     'Restore data must cover every attempted write.')
            if write['verified']:
                _require(write['readback'] == write['requested'] and write['error'] is None,
                         'Verified write must match readback and have no error.')
    _require(result['partial_write'] == (status == 'partial'), 'Partial-write flag contradicts status.')
    if status == 'verified':
        _require(bool(writes) and all(w['verified'] for w in writes) and bool(result['end_revision'])
                 and not result['cancel_requested'] and result['failure_reason'] is None,
                 'Verified results require complete successful readback and an ending revision.')
    elif status == 'partial':
        _require(bool(writes) and bool(result['failure_reason']), 'Partial writes need details and reason.')
    else:
        _require(not writes and bool(result['failure_reason']),
                 'No-write results need a reason; uncertain attempted writes are partial.')


def validate(name, value):
    """Validate structure plus cross-field invariants; raises ValueError on rejection.

    This proves the record is coherent, not that a host write or audition happened.
    The action executor must freshly recheck identity, revision and cancellation.
    """
    json.dumps(value, allow_nan=False)
    validators = _validators()
    if name not in validators or name == 'common':
        raise ValueError('Unknown contract: '+name)
    errors = list(validators[name].iter_errors(value))
    if errors:
        error = errors[0]
        raise ValueError('/'+'/'.join(map(str, error.absolute_path))+': '+error.message)
    if name == 'action-proposal':
        _changes(value['changes'])
        _require(value['question_required'] == bool(value['question']), 'Question flag contradicts text.')
        reference = value['reference_target']
        if reference and reference['comparison_status'] == 'measured':
            _require(reference['comparable_material'] is True and reference['loudness_matched'] is True
                     and bool(reference['evidence_ids']), 'Reference measurement requires comparable matched audio.')
    elif name == 'verified-result':
        _result(value)
    elif name == 'move-history':
        _changes(value['changes'])
        result = value['verified_result']
        _result(result)
        _require(result['status'] in ('verified', 'partial'), 'Move history records attempted writes only.')
        for field in ('request_id', 'target_identity', 'start_revision', 'cancel_requested', 'partial_write', 'restore_data'):
            _require(value[field] == result[field], 'Move/result mismatch: '+field)
        changes = {c['control_id']: c for c in value['changes']}
        for write in result['writes']:
            change = changes.get(write['control_id'])
            _require(change is not None and change['before'] == write['before'] and change['after'] == write['requested'],
                     'Write does not match the recorded change.')
        if result['status'] == 'verified':
            _require(set(changes) == {w['control_id'] for w in result['writes']},
                     'Verified move needs readback for every proposed control.')
    return value
