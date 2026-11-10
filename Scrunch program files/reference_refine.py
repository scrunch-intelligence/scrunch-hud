"""Closer to the reference by measuring, not guessing: a bounded measure -> adjust -> re-measure loop.

After the engineer's first verified change toward his reference passage, the same 1-3 controls are nudged a
few times. Each try is measured against the passage (level-matched band differences), a small model of how
each band responds is fitted to every measurement so far, and the next try is the setting that model expects
to be closest. The best MEASURED setting wins; a prediction is never reported as a result. When the passage
cannot be matched with these controls, it says so with the remaining difference.

This is the product form of the Brooklyn Rose Part 2 result (an online quadratic response model beat
Bayesian, coordinate and random search at equal cost on Diva, offline). It is pure: it proposes settings and
reads measurements; the caller writes, verifies and captures. Not wired into the engineer yet (see
REFINE_ENABLED); live use waits for John's listening test.
"""
import itertools

import numpy as np

REFINE_ENABLED = False     # off until John has heard it on his own setup
MAX_TRIES = 4              # extra captures after the engineer's change (each is a few seconds of Live playing)
HALF_WIDTH = .15           # local window around the engineer's setting, as a share of each control's range
TOLERANCE_DB = 1.0         # a band within this of its goal counts as matched (engineering screen, not hearing)
NOISE = .1                 # a predicted gain smaller than this (in tolerance units) is not worth a capture


def residuals(measure, goals=None, tolerance=TOLERANCE_DB):
    """{band: error in tolerance units} from a reference measurement ({'bands': [{band, differenceDb}]}).
    goals: {band: wanted difference in dB}; 0 (match the reference) for any band not named."""
    goals = goals or {}
    return {b['band']: (b['differenceDb'] - goals.get(b['band'], 0.)) / tolerance for b in measure['bands']}


def score(errors):
    """RMS of the band errors: what 'closest' means when choosing the best measured try."""
    v = np.array(list(errors.values()), dtype=float)
    return float(np.sqrt(np.mean(v * v))) if v.size else float('inf')


def matched(errors):
    return bool(errors) and all(abs(e) <= 1. for e in errors.values())


def choose_controls(changes, limit=2):
    """The controls to refine: the (at most) two the engineer moved most. With four tries, two controls leave
    room for a fitted model step after the probes; three would spend almost every try probing."""
    moved = sorted((c for c in changes if abs(c['after'] - c['before']) > 0), key=lambda c: -abs(c['after'] - c['before']))
    return [c['control_id'] for c in moved[:limit]]


class Refiner:
    """Proposes settings for the controls in `names`, starting at `start` (the engineer's change), within
    [lo, hi] per control. Feed every capture back with tell(); ask() returns the next setting or None."""

    def __init__(self, names, start, lo, hi, before=None, max_tries=MAX_TRIES, half_width=HALF_WIDTH,
                 resolution=None, direction=None):
        self.names = list(names)
        self.x1 = np.array([float(start[n]) for n in self.names])
        self.lo = np.array([float(lo[n]) for n in self.names])
        self.hi = np.array([float(hi[n]) for n in self.names])
        span = self.hi - self.lo
        self.h = np.maximum(span * half_width, 1e-9)
        self.res = np.array([float((resolution or {}).get(n, span_i * 1e-3)) for n, span_i in zip(self.names, span)])
        self.max_tries = max_tries
        self.before = {n: float(before[n]) for n in self.names} if before else None
        # Probe each control first the way the engineer moved it (+1 / -1): fewer tries that sound further away.
        self.direction = {n: (-1 if (direction or {}).get(n, 1) < 0 else 1) for n in self.names}
        self.points, self.errors, self.choosable = [], [], []      # settings tried, their band errors, keepable
        self.tries = 0
        self.rho = 1.
        self.stop_reason = None
        self._stalls = 0
        self._probing = False          # the last proposal was a probe (exploration), not a model step

    # ---- bookkeeping -------------------------------------------------------------------------------
    def tell(self, setting, errors, charged=True, choosable=True):
        """A measured setting and its band errors. charged=False for measurements the loop did not pay for
        (the engineer's change itself, or the sound before it). choosable=False: it informs the model but is
        never offered as the result (the sound before his request)."""
        best = self.best()
        self.points.append({n: float(setting[n]) for n in self.names})
        self.errors.append(dict(errors))
        self.choosable.append(bool(choosable))
        if charged:
            self.tries += 1
            if best is None or score(errors) < best[1]:
                self._stalls = 0
            elif not self._probing:             # a probe that misses is exploration, not a stall
                self._stalls += 1
            if best is not None and score(errors) >= best[1] and not self._probing:
                self.rho = max(self.rho / 2, .125)     # a model step that did not help: look closer next time

    def best(self):
        """(setting, score, errors) of the closest MEASURED try so far, or None."""
        keep = [k for k in range(len(self.points)) if self.choosable[k]]
        if not keep:
            return None
        i = min(keep, key=lambda k: score(self.errors[k]))
        return self.points[i], score(self.errors[i]), self.errors[i]

    # ---- model ---------------------------------------------------------------------------------------
    def _z(self, x):
        return (np.asarray(x, dtype=float) - self.x1) / self.h

    def _design(self, Z, n_obs):
        """Columns: 1, z_i, then curvature only when there are more points than affine terms."""
        d = Z.shape[1]
        cols = [np.ones(len(Z))] + [Z[:, i] for i in range(d)]
        curved = []
        if n_obs > d + 1:
            if d == 1:
                curved = [Z[:, 0] ** 2]
            elif d == 2:
                curved = [Z[:, 0] * Z[:, 1]] + ([Z[:, 0] ** 2, Z[:, 1] ** 2] if n_obs > d + 2 else [])
            elif n_obs > d + 2:
                curved = [Z[:, i] ** 2 for i in range(d)]
        return np.column_stack(cols + curved), len(curved)

    def _fit(self):
        bands = sorted(set.intersection(*[set(e) for e in self.errors])) if self.errors else []
        if not bands:
            return None, bands
        Z = self._z([[p[n] for n in self.names] for p in self.points])
        A, k = self._design(Z, len(Z))
        lam = np.array([1e-4] * (A.shape[1] - k) + [.1] * k)       # curvature is held back when data are thin
        Y = np.array([[e[b] for b in bands] for e in self.errors])
        W = np.linalg.solve(A.T @ A + np.diag(lam), A.T @ Y)
        return W, bands

    def predict(self, x):
        W, bands = self._fit()
        if W is None:
            return None
        A, _ = self._design(self._z([x]), len(self.points))
        return dict(zip(bands, (A @ W)[0]))

    # ---- proposal -----------------------------------------------------------------------------------
    def _probe(self):
        """Not enough points for a line through every control: probe one untried control by half a window."""
        # Spread among keepable points only: the sound before his request moved every control at once and
        # would otherwise stop each control being probed on its own.
        tried = np.array([[p[n] for n in self.names] for p, ok in zip(self.points, self.choosable) if ok])
        for i in range(len(self.names)):
            if np.ptp(tried[:, i]) > self.res[i]:
                continue
            first = self.direction[self.names[i]]
            for sign in (first, -first):
                x = self.x1.copy()
                x[i] = np.clip(self.x1[i] + sign * .5 * self.h[i], self.lo[i], self.hi[i])
                if abs(x[i] - self.x1[i]) > self.res[i]:
                    return x
        return None

    def ask(self):
        """The next setting to try ({name: value}), or None when the loop should stop (see stop_reason)."""
        best = self.best()
        if best is None:
            self.stop_reason = 'nothing measured'
            return None
        if matched(best[2]):
            self.stop_reason = 'matched'
            return None
        if self.tries >= self.max_tries:
            self.stop_reason = 'out of tries'
            return None
        if self._stalls >= 2:
            self.stop_reason = 'two tries without improvement'
            return None
        self._probing = False
        if sum(self.choosable) < len(self.names) + 1:
            x = self._probe()
            if x is not None:
                self._probing = True
                return dict(zip(self.names, map(float, x)))
        W, bands = self._fit()
        if W is None:
            self.stop_reason = 'no band measured every time'
            return None
        d = len(self.names)
        steps = {1: 41, 2: 21, 3: 11}.get(d, 7)
        # Around the best measured setting, never further than one window from the engineer's own choice.
        centre = np.array([best[0][n] for n in self.names])
        axis = np.linspace(-self.rho, self.rho, steps)
        Z = np.array(list(itertools.product(axis, repeat=d)))
        X = np.clip(centre + Z * self.h, np.maximum(self.lo, self.x1 - self.h), np.minimum(self.hi, self.x1 + self.h))
        tried = np.array([[p[n] for n in self.names] for p in self.points])
        far = np.all(np.any(np.abs(X[:, None, :] - tried[None, :, :]) > self.res, axis=2), axis=1)
        if not far.any():
            self.stop_reason = 'next move smaller than the controls can resolve'
            return None
        X = X[far]
        A, _ = self._design(self._z(X), len(self.points))
        predicted = np.sqrt(np.mean((A @ W) ** 2, axis=1))
        i = int(np.argmin(predicted))
        if predicted[i] > best[1] - NOISE:
            self.stop_reason = 'no setting is expected to get meaningfully closer'
            return None
        return dict(zip(self.names, map(float, X[i])))

    def summary(self):
        """What to tell him: the best measured setting and how close it came, per band."""
        best = self.best()
        if best is None:
            return {'status': 'not measured', 'reason': self.stop_reason}
        setting, s, errors = best
        return {'status': 'matched' if matched(errors) else 'closest with these controls',
                'setting': setting, 'rmsError': round(s, 3), 'tries': self.tries,
                'bandErrorsDb': {b: round(e * TOLERANCE_DB, 2) for b, e in errors.items()},
                'stopped': self.stop_reason}
