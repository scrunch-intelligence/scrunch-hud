"""Read-only reconciliation of uncertain writes. Never resolves or retries them."""
import math
import session_plans

def reconcile(record, observation):
    plan = record['plan']
    if not record.get('requiresRecovery'):
        raise ValueError('Transaction does not require interrupted-write recovery')
    current_hash = session_plans.fingerprint(observation)
    rows = []
    for action in plan['actions']:
        matches = [t for t in observation['tracks'] if t.get('id') == action['target']]
        value = matches[0].get(action['parameter']) if len(matches) == 1 else None
        valid = type(value) in (float, int) and math.isfinite(value)
        same = lambda expected: valid and abs(value - expected) < .001
        state = 'original-value-observed' if same(action['before']) else 'planned-value-observed' if same(action['after']) else 'conflict-or-unavailable'
        rows.append({'trackId': action['target'], 'track': action['track'],
                     'parameter': action['parameter'], 'original': action['before'],
                     'planned': action['after'], 'current': value if valid else None,
                     'state': state})
    return {'planId': plan['id'], 'status': 'inspection-only',
            'observationFingerprint': current_hash, 'targets': rows,
            'canResolveAutomatically': False, 'canRetry': False,
            'summary': 'Current values read. The interrupted transaction remains unresolved; matching values do not prove restoration or ownership.',
            'limitations': ['Host IDs alone do not establish saved-session identity.',
                           'A separate fresh, authorized recovery plan is required; concurrent edits must be preserved.']}
