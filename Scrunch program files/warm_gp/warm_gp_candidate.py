"""Research-only Part6 policy adapter. No plugin, product imports, or host writes."""
from copy import deepcopy
import numpy as np
from part5_search import propose

METRIC = 'part6-raw-lufs-normalized-eleven-bands-v1'


class WarmGP:
    """Explicit map identity, one pending proposal, and measured-only best retention."""
    def __init__(self, *, identity, stored_identity, metric, stored_metric,
                 stored, target, guess, before, dimensions, seed):
        if not identity or identity != stored_identity:
            raise ValueError('Map identity mismatch')
        if metric != METRIC or stored_metric != METRIC:
            raise ValueError('Unsupported metric: floored product bands are not Part6 raw descriptors')
        if dimensions not in (1, 2) or not stored:
            raise ValueError('One/two controls and a nonempty map required')
        self.target={'features':self._features(target)}
        self.guess=self._row(guess);self.before=self._row(before)
        self.stored=[self._row(r) for r in stored]
        if dimensions==1 and any(abs(r['x'][1]-self.guess['x'][1])>1e-9 for r in self.stored+[self.before]):
            raise ValueError('Inactive control differs from map slice')
        self.dimensions,self.seed=dimensions,int(seed)
        self.rows=[self.guess];self.tries=0;self.pending=None;self.cancelled=False

    @staticmethod
    def _features(row):
        f=np.asarray(row['features'],dtype=float)
        if f.shape!=(11,) or not np.isfinite(f).all():
            raise ValueError('All eleven finite unrounded descriptors required')
        return f.tolist()

    @staticmethod
    def _row(row):
        x=np.asarray(row['x'],dtype=float);f=np.asarray(row['features'],dtype=float)
        if x.shape!=(2,) or f.shape!=(11,) or not np.isfinite(x).all() or not np.isfinite(f).all():
            raise ValueError('Finite two-control setting and all eleven unrounded descriptors required')
        if np.any((x<0)|(x>1)):raise ValueError('Unnormalized control')
        return {'x':x.tolist(),'features':f.tolist()}

    def error(self,row):return np.array(row['features'])-self.target['features']

    def best(self):
        return deepcopy(min(self.rows,key=lambda r:float(np.mean(self.error(r)**2))))

    def matched(self):return bool(np.all(np.abs(self.error(self.best()))<=1))

    def ask(self):
        if self.cancelled or self.tries>=4 or self.matched():return None
        if self.pending is None:
            x,_=propose(self.stored,[self.before]+self.rows,self.target,self.guess['x'],self.dimensions,self.seed+self.tries*100000)
            self.pending=x
        return list(self.pending)

    def tell(self,row):
        if self.cancelled or self.pending is None:raise ValueError('No pending proposal')
        try:
            row=self._row(row)
            if not np.allclose(row['x'],self.pending,atol=1e-12,rtol=0):raise ValueError('Measurement/readback does not identify pending setting')
            if np.any(np.abs(np.array(row['x'])-self.guess['x'])>.150000001):raise ValueError('Outside local window')
        except (ValueError,KeyError,TypeError):
            self.cancel()
            raise
        self.rows.append(row);self.tries+=1;self.pending=None

    def cancel(self):
        self.cancelled=True;self.pending=None

    def summary(self):
        best=self.best();e=self.error(best)
        return {'status':'cancelled' if self.cancelled else 'matched' if self.matched() else 'closest measured',
                'setting':best['x'],'tries':self.tries,'rms_db':float(np.sqrt(np.mean(e*e))),
                'max_db':float(np.max(np.abs(e))),
                'requires_verified_application':True}
