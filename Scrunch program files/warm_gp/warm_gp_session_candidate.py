"""Research-only orchestration contract. No product imports or real host implementation.

The host must implement cancelled(), read(), write(x, expected, identity), capture().
read() returns identity and normalized x. write() must atomically enforce expected
state and identity. capture() returns eleven raw Part 6 descriptors, not errors.
The synchronous callback contract cannot interrupt a hung host call.
"""
import math
from copy import deepcopy
import numpy as np
from warm_gp_candidate import WarmGP


class GuardFailure(RuntimeError):
    pass


class SessionPolicy(WarmGP):
    """Bind the unchanged tested proposal policy to the caller's verified host identity."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.host_identity = deepcopy(kwargs['identity'])


class CaptureBudget:
    def __init__(self, passage_seconds, limit_seconds, max_search=4):
        self.passage = float(passage_seconds)
        self.limit = float(limit_seconds)
        if not all(math.isfinite(v) and v > 0 for v in (self.passage, self.limit)):
            raise ValueError('Finite positive capture durations required')
        if type(max_search) is not int or not 0 <= max_search <= 4:
            raise ValueError('Search attempts must be between zero and four')
        self.max_search = max_search
        self.search_attempts = 0
        self.confirm_attempts = 0

    @property
    def charged_seconds(self):
        return (self.search_attempts + self.confirm_attempts) * self.passage

    def can_search(self):
        # Leave one complete passage for independent confirmation.
        return (self.search_attempts < self.max_search
                and self.charged_seconds + 2 * self.passage <= self.limit)

    def charge_search(self):
        if not self.can_search():
            raise GuardFailure('Search capture budget exhausted')
        self.search_attempts += 1

    def charge_confirmation(self):
        if self.confirm_attempts or self.charged_seconds + self.passage > self.limit:
            raise GuardFailure('Confirmation capture budget exhausted')
        self.confirm_attempts += 1


def _setting(value):
    value = np.asarray(value, dtype=float)
    if value.shape != (2,) or not np.isfinite(value).all() or np.any((value < 0) | (value > 1)):
        raise GuardFailure('Invalid normalized readback')
    return value.tolist()


def _same(a, b):
    return bool(np.allclose(_setting(a), _setting(b), atol=1e-12, rtol=0))


def run_session(policy, host, *, identity, passage_seconds=6., capture_budget_seconds=24.):
    """Run a fresh policy with a mock/owned host contract; never claims application from model data.

    Capture duration accounting excludes the already-measured engineer's guess.
    A real adapter still needs bounded calls, actual audio duration validation,
    persistent undo/history and atomic expected-state writes.
    """
    if (not identity or not isinstance(policy, SessionPolicy) or policy.host_identity != identity
            or policy.tries or policy.pending is not None or policy.cancelled):
        raise ValueError('A fresh policy and nonempty identity are required')
    budget = CaptureBudget(passage_seconds, capture_budget_seconds)
    expected = _setting(policy.guess['x'])
    result = {'status': 'not run', 'requires_live_integration': True,
              'events': [], 'confirmed_match': False}

    def guard():
        if host.cancelled():
            raise GuardFailure('Cancelled')
        state = host.read()
        if state.get('identity') != identity:
            raise GuardFailure('Host identity changed')
        if not _same(state.get('x'), expected):
            raise GuardFailure('Controls moved outside this session')

    def put(x):
        nonlocal expected
        guard()
        x = _setting(x)
        if not _same(x, expected):
            host.write(list(x), list(expected), identity)
            expected = x
            guard()
            result['events'].append('write verified')

    def capture(kind):
        guard()
        if kind == 'search':
            budget.charge_search()
        else:
            budget.charge_confirmation()
        result['events'].append(kind + ' charged')
        features = host.capture()
        guard()
        return policy._row({'x': expected, 'features': features})

    try:
        guard()
        while budget.can_search():
            proposal = policy.ask()
            if proposal is None:
                break
            proposal = _setting(proposal)
            if np.any(np.abs(np.array(proposal) - policy.guess['x']) > .150000001):
                raise GuardFailure('Proposal outside authorized window')
            if policy.dimensions == 1 and abs(proposal[1] - policy.guess['x'][1]) > 1e-12:
                raise GuardFailure('Proposal changes inactive control')
            put(proposal)
            policy.tell(capture('search'))
        best = policy.best()
        put(best['x'])
        result['selected_setting'] = best['x']
        result['selected_rms_db'] = float(np.sqrt(np.mean(policy.error(best) ** 2)))
        if policy.matched():
            final = capture('confirmation')
            errors = policy.error(final)
            result['final_rms_db'] = float(np.sqrt(np.mean(errors ** 2)))
            result['confirmed_match'] = bool(np.all(np.abs(errors) <= 1.))
            result['status'] = 'matched' if result['confirmed_match'] else 'confirmation did not match'
        else:
            result['status'] = 'closest measured'
        guard()
    except Exception as exc:
        # An uncertain write or user intervention must never trigger a blind restoration.
        policy.cancel()
        result.update(status='stopped', confirmed_match=False,
                      reason=str(exc), live_state_requires_reconciliation=True)
    result.update(search_attempts=budget.search_attempts,
                  confirmation_attempts=budget.confirm_attempts,
                  charged_capture_seconds=budget.charged_seconds,
                  policy_trials=policy.tries)
    return result
