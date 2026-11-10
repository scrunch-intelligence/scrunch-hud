"""Brooklyn Rose in the product: the warm GP (Part 6 winner) as the search inside reference_loop.refine.

reference_loop keeps doing everything around the search exactly as before (kill switch, compare-and-set writes,
readback, measuring each try against his reference passage, keeping the best MEASURED setting as one verified
change, confirming before 'matched'). Only the proposals change: instead of the local Refiner, each try comes
from Astra's warm GP (warm_gp/, byte-identical to the tested candidate), fitted on an offline map of THIS patch
(patch_map: the synth's own state rendered outside Live) plus every measured try.

Needs: the synth hosted in Scrunch Host (its state is the map's source), the tested renderer, and the reference loop
switched on (dashboard: 'measure and adjust'); engine.STATE['referenceEngine'] = 'refiner' keeps the old search. Metric: the raw eleven-band descriptors the GP was
tested on (band minus loudness, unrounded), never the floor-clamped display bands.

Transfer (open): the map is a held note, his passage is music. The map is shifted so its prediction at the
engineer's change equals what was measured there (bias correction), pending Astra's offline transfer test.
"""
import numpy as np
from adaptive_gp import AdaptivePolicy

import warm_gp  # noqa: F401  (puts the candidate's modules on the path)
import patch_map
from part3_search import FeatureGP

TOLERANCE_DB = 1.


def wanted():
    """The warm GP is the reference search whenever it can run (John, 24 Sep: 'Brooklyn Rose made live'); the
    older Refiner only if referenceEngine is set to 'refiner', or when there is no hosted synth to map."""
    import engine
    return engine.STATE.get('referenceEngine', 'warm-gp') == 'warm-gp'


def hosted_state(c):
    """The hosted synth's state bytes, or None when the synth is not in Scrunch Host."""
    try:
        import wrapper_backend
        if not isinstance(c.a, wrapper_backend.WrappedAdapter):
            return None
        device = c.a.device(c.device, parameters=False)
        if not wrapper_backend.is_host(device):
            return None
        return patch_map.state_from_wrapper(c.a.wrapper(c.device))
    except Exception:
        return None


def ready(c):
    return wanted() and patch_map.host_ok() and hosted_state(c) is not None


def descriptors(m, side):
    d = (m or {}).get('descriptors') or {}
    return d.get(side)


def corrected(rows, x, measured):
    """Shift every map row so the map's prediction at x equals what was measured there (transfer, held note ->
    his passage). Per band, from a GP fitted on the map alone."""
    xs = np.column_stack([np.array([r['x'] for r in rows]), np.zeros(len(rows))])
    y = np.array([r['features'] for r in rows])
    at = np.array([[x[0], x[1], 0.]])
    offset = np.array([FeatureGP(xs, y[:, j]).predict(at)[0][0] for j in range(y.shape[1])])
    shift = np.asarray(measured) - offset
    return [{'x': list(r['x']), 'features': (np.asarray(r['features']) + shift).tolist()} for r in rows], shift.tolist()


class WarmRefiner:
    """reference_refine.Refiner's interface (ask / tell / summary) around warm_gp.SessionPolicy."""

    def __init__(self, ids, start, before, rows, target, guess, before_features, max_tries, seed=0):
        self.ids = list(ids)
        pad = lambda d: [d[self.ids[0]], d[self.ids[1]] if len(self.ids) > 1 else 0.]
        identity = 'scrunch-live:' + ','.join(self.ids)
        self.gp = warm_gp.SessionPolicy(identity=identity, stored_identity=identity, metric=warm_gp.METRIC,
                                        stored_metric=warm_gp.METRIC, stored=rows, target={'features': target},
                                        guess={'x': pad(start), 'features': guess},
                                        before={'x': pad(before), 'features': before_features},
                                        dimensions=len(self.ids), seed=seed)
        self.max_tries = min(int(max_tries), 4)
        self.target = np.asarray(target, dtype=float)
        self.proposals = []           # log only: every proposal, with how many measured rows it was computed from

    def ask(self):
        if self.gp.tries >= self.max_tries:
            return None
        x = self.gp.ask()
        if x is not None and (not self.proposals or self.proposals[-1]['x'] != list(x)):
            self.proposals.append({'x': [float(v) for v in x], 'measuredRowsSeen': len(self.gp.rows) + 1})
        return None if x is None else {p: float(x[i]) for i, p in enumerate(self.ids)}

    def tell(self, x, m):
        """A measured try: m is reference_target.measure()'s result for the setting x."""
        f = descriptors(m, 'live')
        if f is None:
            self.gp.cancel()
            raise ValueError('this try has no raw descriptors')
        pad = [x[self.ids[0]], x[self.ids[1]] if len(self.ids) > 1 else 0.]
        self.gp.tell({'x': pad, 'features': f})

    def check(self, m):
        """Confirm listen: every one of the eleven raw descriptors within 1 dB of the reference's."""
        live, ref = descriptors(m, 'live'), descriptors(m, 'reference')
        return live is not None and ref is not None and bool(np.all(np.abs(np.asarray(live) - np.asarray(ref)) <= TOLERANCE_DB))

    def summary(self):
        best = self.gp.best()
        e = np.asarray(best['features']) - self.target
        setting = {p: float(best['x'][i]) for i, p in enumerate(self.ids)}
        return {'status': 'matched' if self.gp.matched() else 'closest with these controls', 'setting': setting,
                'rmsError': round(float(np.sqrt(np.mean(e * e))), 3), 'tries': self.gp.tries, 'engine': 'warm-gp',
                'bandErrorsDb': [round(float(v), 2) for v in e], 'stopped': 'cancelled' if self.gp.cancelled else None,
                # Log only: the measured rows the GP learned from (before, guess, every told try) and its proposals.
                'gpLog': {'observations': [{'x': r['x'], 'features': [round(v, 4) for v in r['features']]}
                                           for r in [self.gp.before] + self.gp.rows],
                          'proposals': self.proposals}}


def build(c, ids, knobs, start, before, ref_after, ref_before, max_tries, stop=None):
    """The map and the search for one request, or None when the warm GP cannot run here (the Refiner does)."""
    state = hosted_state(c)
    target, guess = descriptors(ref_after, 'reference'), descriptors(ref_after, 'live')
    before_f = descriptors(ref_before, 'live')
    if state is None or target is None or guess is None or before_f is None:
        return None
    vst = [str(p)[2:] if str(p).startswith('w:') else str(p) for p in ids]     # hosted ids are 'w:<VST3 ParamID>'
    rows = patch_map.build(state, vst, stop=stop)
    rows, shift = corrected(rows, [start[ids[0]], start[ids[1]] if len(ids) > 1 else 0.], guess)
    r = WarmRefiner(ids, start, before, rows, target, guess, before_f, max_tries)
    r.map = {'rows': len(rows), 'shiftDb': [round(v, 2) for v in shift]}
    return r
