"""Product GP session: pending proposal identity and lossless reproducible updates."""
from copy import deepcopy
import numpy as np
from adaptive_sound import ComponentGP


def measured_gp(rows,categorical,noise):
    responses=[r['response'] for r in rows] if all('response' in r for r in rows) else None
    sizes=rows[0].get('response_sizes') if responses is not None else None
    if responses is not None and any(r['response_sizes']!=sizes for r in rows):
        raise ValueError('Audio response dimensions changed')
    return ComponentGP([r['x'] for r in rows],[r['features'] for r in rows],categorical,noise,responses,sizes)


class AdaptivePolicy:
    def __init__(self,space,rows,noise,seed):
        self.space,self.rows,self.noise,self.seed=space,deepcopy(rows),np.asarray(noise),seed
        self.pending=None; self.asks=0; self.silent=[]
        self.refit()

    def refit(self):
        self.gp=measured_gp(self.rows,self.space.categorical,self.noise)

    def record(self,trace,name): return self.gp.record(trace,name)

    def ask(self,center,incumbent,trace,name):
        if self.pending is not None: return self.pending[0].copy(),self.pending[1]
        pool,draws,rng=self.space.pool(center,self.seed+self.asks*100000,[r['x'] for r in self.rows]+self.silent)
        local=self.gp.refinements(center,self.space)
        if len(local):
            seen=np.asarray([r['x'] for r in self.rows]+self.silent)
            local=local[np.all(np.max(np.abs(local[:,None,:]-seen[None,:,:]),axis=2)>1e-6,axis=1)]
            pool=np.concatenate((pool,local))
        values=np.asarray([np.mean(r['features'][:4]) for r in self.rows])
        exploration=2. if self.asks>=3 and values[-3:].min()>=values[:-3].min()-1e-6 else .1
        weights=np.ones(4)
        if self.gp.feasibility:
            nearest=int(np.argmin(np.max(np.abs(self.gp.x-center),axis=1)))
            weights=(self.gp.y[nearest,:4]>1).astype(float)
        index,means,variances,acquisition=self.gp.select(pool,draws,incumbent,exploration,weights)
        record=trace.arrays(name,pool=pool,draws=draws,means=means,variances=variances,acquisition=acquisition)
        trace.event('ask_state',name=name,rng=rng,incumbent=incumbent,center=center.tolist(),index=index,record=record,
                    exploration=exploration,feasibility=self.gp.feasibility,uncertainty_weights=weights.tolist())
        context={'pool':pool,'draws':draws,'incumbent':incumbent,'index':index,'record':record,
                 'exploration':exploration,'feasibility':self.gp.feasibility,'uncertainty_weights':weights.tolist()}
        self.pending=(pool[index].copy(),context); self.asks+=1
        return self.pending[0].copy(),context

    def propose(self,x,trace,name,reason):
        """Measured-refinement proposal; its observation still reaches the GP through tell()."""
        if self.pending is not None: raise ValueError('Proposal already pending')
        context={'reason':reason,'record':trace.arrays(name,proposal=np.asarray(x,float))}
        trace.event('ask_state',name=name,reason=reason,x=np.asarray(x,float).tolist())
        self.pending=(np.asarray(x,float).copy(),context); self.asks+=1
        return self.pending[0].copy(),context

    def tell(self,row):
        if self.pending is None or not np.allclose(row['x'],self.pending[0],atol=1e-6,rtol=0):
            raise ValueError('Observation does not identify the pending actual setting')
        self.rows.append(deepcopy(row)); self.pending=None; self.refit()

    def silent_result(self,x):
        """A measured silent phrase is the worst possible result: never proposed again, never kept."""
        if self.pending is None or not np.allclose(x,self.pending[0],atol=1e-6,rtol=0):
            raise ValueError('Silent result does not identify the pending actual setting')
        self.silent.append(list(map(float,x))); self.pending=None

    def add_confirmation(self,row):
        if self.pending is not None: raise ValueError('Pending proposal at confirmation')
        self.rows.append(deepcopy(row)); self.refit()

    def counterfactual(self,without_first,context,trace):
        other=measured_gp(without_first,self.space.categorical,self.noise)
        other.feasibility=context.get('feasibility',other.feasibility)
        idx,m,v,a=other.select(context['pool'],context['draws'],context['incumbent'],context.get('exploration',.1),context.get('uncertainty_weights'))
        with_observation=context['pool'][context['index']]; without=context['pool'][idx]
        delta=np.abs(with_observation-without)
        material=bool(np.any(delta[~self.space.categorical]>=.01) or np.any(delta[self.space.categorical]>=1))
        arrays=trace.arrays('counterfactual',pool=context['pool'],draws=context['draws'],means_without=m,
                            variances_without=v,acquisition_without=a,with_observation=with_observation,without_observation=without)
        other.record(trace,'gp-withheld-observation-1')
        result={'material':material,'max_coordinate_difference':float(delta.max()),
                'with_observation':with_observation.tolist(),'without_observation':without.tolist(),
                'same_pool_draws_incumbent_exclusion_mask':True,'arrays':arrays}
        trace.event('counterfactual',**result)
        return result
