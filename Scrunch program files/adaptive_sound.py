"""Phrase-based, real-coordinate search shared by Match Reference and offline acceptance."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from scipy.signal import stft

VERSION = 'adaptive-diva-1'
COMPONENTS = ('S', 'L', 'E', 'T')
SILENT = 'Required DSP cannot measure a silent phrase'
# John, 26 Sep 2026: keep a clearly better sound even if one measure is somewhat worse than the original.
WORSE_ALLOWANCE = .25


def proposal_loss(components,feasibility=False):
    values=np.maximum(np.asarray(components)[...,:4],0)
    return values.mean(axis=-1)+(np.maximum(values.max(axis=-1)-1,0) if feasibility else 0)


def retention_rank(components):
    values=np.asarray(components)[:4]
    return (bool(np.any(values>1)),float(np.square(np.maximum(values-1,0)).sum()),float(values.mean()))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False))


class Trace:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = 0

    def event(self, kind, **fields):
        self.index += 1
        row = dict(event=self.index, kind=kind, at=time.time(), **fields)
        with (self.root / 'events.jsonl').open('a') as f:
            f.write(canonical(row).decode() + '\n')
        return self.index

    def arrays(self, name, **arrays):
        path = self.root / (name + '.npz')
        np.savez_compressed(path, **arrays)
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def _rms(x):
    return float(np.sqrt(np.mean(np.asarray(x) ** 2)))


def _curve(y, hop):
    return np.array([20 * np.log10(max(_rms(y[i:i+hop]), 1e-3)) for i in range(0, len(y), hop)])


def features(audio, rate, phrase):
    import reference_dsp
    y = reference_dsp.prepare(audio, rate)
    report = reference_dsp.analyse(y)
    loudness = report['integratedLUFS']
    if loudness is None or not np.isfinite(loudness) or loudness < -60:
        raise ValueError(SILENT)
    bands = np.array([max(-80., b['energyDbFS'] if b['energyDbFS'] is not None else -80.)
                      for b in report['bands']]) - loudness
    # One fixed loudness reference leaves time and channel relationships intact.
    matched = y * 10 ** ((-18. - loudness) / 20.)
    envelope = _curve(matched * 10 ** (18/20), 480)
    f, _, z = stft(matched, 48000, nperseg=4800, noverlap=2400, axis=0, boundary='zeros')
    power = np.mean(np.abs(z)**2, axis=1)
    edges = (20, 200, 500, 2000, 4000, 8000, 20000)
    moving = np.stack([10*np.log10(np.maximum(power[(f>=a)&(f<b)].sum(axis=0), 1e-12))
                       for a,b in zip(edges[:-1], edges[1:])], axis=1)
    correlations, active = [], []
    for i in range(0, len(matched), 4800):
        block = matched[i:i+4800]
        denom = float(np.sqrt(np.sum(block[:,0]**2)*np.sum(block[:,1]**2)))
        active.append(_rms(block) > 1e-6)
        correlations.append(float(np.sum(block[:,0]*block[:,1])/denom) if denom>1e-20 else 0.)
    note_evidence = []
    for note in phrase.get('notes', []):
        on, off = note['on'], note['off']
        end = min(len(y), max(off, on+1))
        local = envelope[on//480:max(on//480+1, end//480)]
        if not len(local): continue
        overlap = any(n is not note and n['on']<off and n['off']>on for n in phrase['notes'])
        note_evidence.append({'onSample':on, 'offSample':off, 'overlap':overlap,
                              'peakAfterOnMs':int(np.argmax(local))*10,
                              'onsetDb':float(local[0]), 'lastActiveDb':float(local[-1]),
                              'following100msDb':float(np.mean(envelope[end//480:end//480+10]))
                              if end//480<len(envelope) else None})
    return {'report':report, 'lufs':float(loudness), 'bands':bands, 'envelope':envelope,
            'movement':np.diff(moving,axis=0), 'correlation':np.array(correlations),
            'correlation_active':np.array(active), 'notes':note_evidence, 'matched':matched}


class Evaluator:
    # CLAP was removed on 26 Sep 2026 (John): it disagreed with his blind picks (evidence/clap-vs-john-2026-09-26).
    def __init__(self, target, rate, phrase, trace):
        self.phrase, self.trace = phrase, trace
        self.target = features(target, rate, phrase)
        trace.arrays('target-features', bands=self.target['bands'], envelope=self.target['envelope'],
                     movement=self.target['movement'], correlation=self.target['correlation'])
        trace.event('evaluator', version=VERSION, dsp='reference-ab-2', phrase=digest(phrase))

    def measure(self, audio, rate, name):
        started = time.monotonic()
        f = features(audio,rate,self.phrase)
        if f['matched'].shape != self.target['matched'].shape: raise ValueError('Phrase duration/channels changed')
        mask = f['correlation_active'] | self.target['correlation_active']
        spatial = _rms((f['correlation']-self.target['correlation'])[mask])/.1 if mask.any() else 0.
        movement = _rms(f['movement']-self.target['movement'])/2
        components = np.array([_rms(f['bands']-self.target['bands']),
                               abs(f['lufs']-self.target['lufs']),
                               _rms(f['envelope']-self.target['envelope'])/2,
                               (movement+spatial)/2])
        response_blocks=[f['bands']-self.target['bands'],
                         np.array([f['lufs']-self.target['lufs']]),
                         (f['envelope']-self.target['envelope'])/2,
                         (f['movement']-self.target['movement']).ravel()/2,
                         np.where(mask,(f['correlation']-self.target['correlation'])/.1,0)]
        response_sizes=[block.size for block in response_blocks]
        response=np.concatenate([block/np.sqrt(max(1,(int(mask.sum()) if i==4 else block.size)))
                                 for i,block in enumerate(response_blocks)])
        if not np.isfinite(components).all(): raise ValueError('Nonfinite required measurement')
        arrays = self.trace.arrays(name, bands=f['bands'], envelope=f['envelope'],
                                  movement=f['movement'], correlation=f['correlation'])
        row = {'components':components.tolist(), 'named':dict(zip(COMPONENTS,components.tolist())),
               'response':response.tolist(),'response_sizes':response_sizes,
               'J0':float(components.mean()),
               'movement':{'temporal':movement,'stereo':spatial}, 'notes':f['notes'],
               'lufs':f['lufs'],'matchingGainDb':-18-f['lufs'], 'report':f['report'],
               'arrays':arrays, 'seconds':time.monotonic()-started}
        self.trace.event('measurement',name=name,**{k:v for k,v in row.items() if k not in ('report','notes','response')})
        return row


class CoordinateSpace:
    def __init__(self, scope):
        self.scope = deepcopy(scope)
        self.controls = scope['controls']
        self.ids = [c['id'] for c in self.controls]
        if not self.ids or len(set(self.ids))!=len(self.ids): raise ValueError('Invalid scope coordinates')
        self.categorical = np.array([c['type']=='categorical' for c in self.controls])
        self.options = []
        for c in self.controls:
            options=list(c.get('options',[])) if c['type']=='categorical' else []
            if options:
                labels=c.get('labels',[])
                if c.get('original_label') in labels:
                    options[labels.index(c['original_label'])]=c['original']
                elif c['original'] not in options: options.append(c['original'])
            self.options.append(options)
        self.original = self.encode({c['id']:c['original'] for c in self.controls})

    def encode(self, values):
        out=[]
        for c,options in zip(self.controls,self.options):
            v=float(values[c['id']])
            if not np.isfinite(v) or not c['low']<=v<=c['high']: raise ValueError('Coordinate outside scope')
            if options:
                j=int(np.argmin(np.abs(np.array(options)-v)))
                if abs(options[j]-v)>1e-6: raise ValueError('Invalid categorical readback')
                out.append(j)
            else: out.append((v-c['low'])/(c['high']-c['low']))
        return np.array(out,dtype=float)

    def decode(self, x):
        x=np.asarray(x)
        if x.shape!=(len(self.controls),) or not np.isfinite(x).all(): raise ValueError('Invalid coordinate vector')
        out={}
        for v,c,options in zip(x,self.controls,self.options):
            if options:
                if v!=round(v) or not 0<=v<len(options): raise ValueError('Fractional category')
                out[c['id']]=options[int(v)]
            else:
                if not 0<=v<=1: raise ValueError('Unbounded coordinate')
                out[c['id']]=c['low']+v*(c['high']-c['low'])
        return out

    def pool(self, center, seed, seen):
        rng=np.random.default_rng(seed)
        before=deepcopy(rng.bit_generator.state)
        pool=np.tile(center,(512,1))
        for row in pool:
            for j in rng.choice(len(center),size=min(len(center),int(rng.integers(1,5))),replace=False):
                if self.options[j]: row[j]=rng.integers(len(self.options[j]))
                elif rng.random()<.5: row[j]=rng.uniform()
                else: row[j]=np.clip(row[j]+rng.normal(0,.18),0,1)
        local=[]
        for j in range(len(center)):
            values=(range(len(self.options[j])) if self.options[j] else
                    [self.original[j],*[np.clip(center[j]+step,0,1) for step in (-.1,-.05,-.02,.02,.05,.1)]])
            for value in values:
                row=center.copy(); row[j]=value; local.append(row)
        pool=np.unique(np.concatenate((pool,np.asarray(local))),axis=0)
        if len(seen):
            pool=pool[np.all(np.max(np.abs(pool[:,None,:]-np.asarray(seen)[None,:,:]),axis=2)>1e-6,axis=1)]
        if not len(pool): raise ValueError('Candidate pool exhausted')
        # Common random numbers compare posterior distributions without a separate
        # sampling lottery for each candidate (including identical coordinates).
        draws=rng.normal(size=(32,1,4))
        return pool,draws,{'seed':seed,'before':before,'after':deepcopy(rng.bit_generator.state)}

    def initial_design(self, seed, count=8):
        rng=np.random.default_rng(seed)
        rows=np.tile(self.original,(count,1))
        # Disjoint probes cover the domain without requiring one render per axis.
        for i,group in enumerate(np.array_split(rng.permutation(len(self.ids)),count)):
            for j in group:
                if self.options[j]:
                    choices=[k for k in range(len(self.options[j])) if k!=self.original[j]]
                    if choices: rows[i,j]=rng.choice(choices)
                else: rows[i,j]=.8 if self.original[j]<.5 else .2
        return rows


class ComponentGP:
    def __init__(self, x, y, categorical, noise=None, responses=None, response_sizes=None):
        self.x,self.y=np.asarray(x,float),np.asarray(y,float)
        self.feasibility=bool(np.min(self.y[:,:4].mean(axis=1))<=1)
        self.categorical=np.asarray(categorical,bool)
        self.mean=self.y.mean(axis=0)
        self.amplitude=np.maximum(self.y.var(axis=0),1.)
        self.noise=np.maximum(np.asarray(noise if noise is not None else np.full(4,.03)),.01)
        self.lengths=np.full((4,self.x.shape[1]),.4)
        self.alpha=np.zeros_like(self.y)
        self.factors=[]
        # Different audible components need not depend on the same controls.
        for j in range(4):
            best=self.fit(self.lengths[j],score_only=True,component=j)
            for axis in range(self.x.shape[1]):
                for value in (.2,.8,1.6):
                    candidate=self.lengths[j].copy(); candidate[axis]=value
                    score=self.fit(candidate,score_only=True,component=j)
                    if score<best: self.lengths[j],best=candidate,score
        self.fit(self.lengths)
        self.responses=None if responses is None else np.asarray(responses,float)
        self.response_sizes=response_sizes
        if self.responses is not None:
            if (self.responses.ndim!=2 or len(self.responses)!=len(self.x) or
                len(response_sizes)!=5 or sum(response_sizes)!=self.responses.shape[1] or
                not np.isfinite(self.responses).all()):
                raise ValueError('Invalid signed audio response observations')
            self.response_mean=self.responses.mean(axis=0)
            self.response_factors=[cho_factor(self.kernel(self.x,self.x,self.lengths[min(j,3)])+
                                             np.eye(len(self.x))*1e-5,lower=True) for j in range(5)]
            augmented=np.vstack((self.responses-self.response_mean,self.response_mean))
            self.response_grams=[]
            for block in np.split(augmented,np.cumsum(response_sizes)[:-1],axis=1):
                self.response_grams.append(block@block.T)

    def kernel(self,a,b,lengths):
        d=a[:,None,:]-b[None,:,:]
        d[:,:,self.categorical]=(d[:,:,self.categorical]!=0).astype(float)
        terms=np.exp(-.5*(d/lengths)**2)
        # Additive effects share evidence across coordinates; the joint term
        # still represents interactions instead of assuming a separable synth.
        return .9*terms.mean(axis=2)+.1*terms.prod(axis=2)

    def refinements(self,center,space):
        from scipy.optimize import minimize
        continuous=np.flatnonzero(~space.categorical)
        if not len(continuous): return np.empty((0,len(center)))
        def value(z):
            x=center.copy(); x[continuous]=z
            mean,_=self.predict(x[None,:])
            return float(proposal_loss(mean[0],self.feasibility))
        fit=minimize(value,center[continuous],method='L-BFGS-B',
                     bounds=[(max(0,center[j]-.3),min(1,center[j]+.3)) for j in continuous],
                     options={'maxiter':25,'ftol':1e-8})
        if not np.isfinite(fit.x).all(): return np.empty((0,len(center)))
        order=continuous[np.argsort(-np.abs(fit.x-center[continuous]))]
        full=center.copy(); full[continuous]=fit.x
        rows=[]
        for count in (1,2,4,8,len(continuous)):
            for scale in (.25,.5,1.):
                row=center.copy(); axes=order[:count]
                row[axes]+=scale*(full[axes]-center[axes]); rows.append(row)
        return np.asarray(rows)

    def fit(self,lengths,score_only=False,component=None):
        score=0.; factors=[]; alphas=[]
        for j in ([component] if component is not None else range(4)):
            k=self.kernel(self.x,self.x,lengths if lengths.ndim==1 else lengths[j])
            matrix=k*self.amplitude[j]+np.eye(len(k))*(self.noise[j]**2+1e-8)
            factor=cho_factor(matrix,lower=True)
            centered=self.y[:,j]-self.mean[j]
            alpha=cho_solve(factor,centered)
            score+=float(centered@alpha/2+np.log(np.diag(factor[0])).sum())
            factors.append(factor); alphas.append(alpha)
        if not score_only: self.factors=factors; self.alpha=np.array(alphas).T
        return score

    def predict(self,pool):
        means=[]; variances=[]; kernels=[]
        for j in range(4):
            k=self.kernel(self.x,pool,self.lengths[j]); kernels.append(k)
            kj=k*self.amplitude[j]
            means.append(self.mean[j]+kj.T@self.alpha[:,j])
            variances.append(np.maximum(self.amplitude[j]-np.sum(kj*cho_solve(self.factors[j],kj),axis=0),1e-10))
        means=np.array(means).T
        if self.responses is not None:
            norms=[]
            for j,g in enumerate(self.response_grams):
                weights=np.column_stack((cho_solve(self.response_factors[j],kernels[min(j,3)]).T,np.ones(len(pool))))
                norms.append(np.sqrt(np.maximum(np.einsum('ij,ij->i',weights@g,weights),0)))
            norms=np.array(norms).T
            means[:,:3]=norms[:,:3]; means[:,3]=(norms[:,3]+norms[:,4])/2
        return means,np.array(variances).T

    def select(self,pool,draws,incumbent,exploration=.1,uncertainty_weights=None):
        if draws.ndim!=3 or draws.shape[1:]!=(1,4):
            raise ValueError('Acquisition requires common random samples across candidates')
        means,variances=self.predict(pool)
        weights=np.ones(4) if uncertainty_weights is None else np.asarray(uncertainty_weights,float)
        if weights.shape!=(4,) or not np.isfinite(weights).all() or np.any(weights<0):
            raise ValueError('Invalid uncertainty component weights')
        # A modest confidence bonus explores uncertainty without allowing it to
        # dominate measured improvements in a short, high-dimensional search.
        acquisition=-proposal_loss(means,self.feasibility)+exploration*np.sqrt((variances[:,:4]*weights).sum(axis=1))/4
        index=int(np.argmax(acquisition))
        return index,means,variances,acquisition

    def record(self,trace,name):
        response={} if self.responses is None else dict(responses=self.responses,response_sizes=self.response_sizes,
                        response_mean=self.response_mean,response_cholesky=np.array([np.tril(f[0]) for f in self.response_factors]))
        return trace.arrays(name,X=self.x,Y=self.y,lengths=self.lengths,mean=self.mean,
                            amplitude=self.amplitude,noise=self.noise,alpha=self.alpha,
                            cholesky=np.array([np.tril(f[0]) for f in self.factors]),**response)


def refine_fade(space, policy, best, base, allowed, observe, trace, budget, cancelled):
    """Measured compass steps on the controls the GP finds most envelope-sensitive; keep only real envelope gains."""
    lengths=np.asarray(policy.gp.lengths[2])
    axes=[int(j) for j in np.argsort(lengths,kind='stable') if not space.categorical[j]][:6]
    trace.event('fade_refine_start',axes=[space.ids[j] for j in axes],budget=budget,start=best['features'])
    step,used,cursor=.04,0,0; seen=lambda x:_seen(x,[r['x'] for r in policy.rows]+policy.silent)
    while used<budget and step>=.005:
        if cursor>=2*len(axes): step/=2; cursor=0; continue
        j=axes[cursor//2]; s=step if cursor%2==0 else -step; cursor+=1
        x=np.array(best['x']); x[j]=x[j]+s
        if not 0<=x[j]<=1 or seen(x): continue
        cancelled(); policy.propose(x,trace,f'fade-{used+1:03d}',f'fade {space.ids[j]} {s:+.3f}')
        row=observe(x,'fade',used+1); used+=1
        if row is None: policy.silent_result(x); continue
        policy.tell(row)
        f,b=np.asarray(row['features']),np.asarray(best['features'])
        keep=allowed(f) and f[2]<b[2]-1e-9 and np.all(f[[0,1,3]]<=b[[0,1,3]]+1e-9)
        trace.event('retention',number=f'fade-{used}',eligible=allowed(f),kept=bool(keep),J0=float(f[:4].mean()),tidy=f'fade {space.ids[j]} {s:+.3f}')
        if keep: best=row; cursor=0
    return best,float(np.mean(best['features'][:4]))


def _seen(x, seen):
    return bool(len(seen)) and bool(np.any(np.max(np.abs(np.asarray(seen)-x),axis=1)<=1e-6))


def search(driver, target, *, seed=20260925, max_proposals=56, stop=None, fade_refine=0):
    """Driver owns all effects; model sees only source scope and target audio, never target controls."""
    from warm_gp_engine import AdaptivePolicy
    trace=Trace(driver.root/'trace'); space=CoordinateSpace(driver.scope)
    target_audio,target_rate=target
    started=time.monotonic(); observations=[]; silent=[]; improvements=0; result=None
    trace.event('context',version=VERSION,scope=driver.scope,scope_sha256=digest(driver.scope),
                phrase=driver.phrase,phrase_sha256=digest(driver.phrase),seed=seed,
                budgets={'baseline':3,'priors':8,'adaptive':max_proposals,'confirmation':3},
                provenance=driver.provenance,proposal_policy='J0 until a measured J0<=1, then J0 plus maximum component excess; confidence bonus 0.1 or 2 after stagnation; feasibility exploration uses only still-failing measured components',
                retention_policy=f'eligible = no component more than {WORSE_ALLOWANCE:.0%} worse than the source; eligible feasible candidates precede infeasible; then squared component excess; then J0')
    def cancelled():
        if stop and stop(): raise InterruptedError('Search cancelled')
    def observe(x,role,index):
        cancelled(); values=space.decode(x)
        audio,rate,record=driver.render(values,role,index)
        cancelled(); actual=space.encode(record['values'])
        if not np.allclose(actual,x,atol=1e-6,rtol=0): raise ValueError('Proposal readback mismatch')
        try: measurement=evaluator.measure(audio,rate,f'{role}-{index:03d}')
        except ValueError as exc:
            # John, 26 Sep 2026: a genuinely silent candidate is a measured worst result, not a stop.
            # The source patch and confirmations must still sound; every other failure still stops.
            if role in ('baseline','confirmation') or not str(exc).startswith(SILENT): raise
            trace.event('silent',role=role,index=index,x=actual.tolist(),audio=record['audio'],reason=str(exc))
            silent.append(actual.tolist())
            return None
        row={'x':actual.tolist(),'features':measurement['components'],'measurement':measurement,
             'response':measurement['response'],'response_sizes':measurement['response_sizes'],
             'audio':record['audio'],'record':record['record'],'role':role,'index':index}
        observations.append(row)
        trace.event('observation',observation=len(observations),**{k:v for k,v in row.items() if k!='measurement'})
        return row
    try:
        evaluator=Evaluator(target_audio,target_rate,driver.phrase,trace)
        baseline=[observe(space.original,'baseline',i) for i in range(3)]
        base=np.mean([r['features'] for r in baseline],axis=0)
        noise=np.std([r['features'] for r in baseline],axis=0,ddof=1)
        best=baseline[0]; best_value=float(np.mean(best['features'][:4])); baseline_value=float(base[:4].mean())
        allowed=lambda f:bool(np.all(np.asarray(f)[:4]<=base[:4]*(1+WORSE_ALLOWANCE)+2*noise[:4]+1e-9))
        for i,probe in enumerate(space.initial_design(seed-100)):
            row=observe(probe,'prior',i)
            if row is None: continue
            f=np.asarray(row['features']); value=float(f[:4].mean())
            if allowed(f) and retention_rank(f)<retention_rank(best['features']):
                best=row; best_value=value
        working=min(observations,key=lambda r:float(proposal_loss(r['features'])))
        working_value=float(np.mean(working['features'][:4]))
        policy=AdaptivePolicy(space,observations,noise,seed)
        trace.event('gp_update',posterior=policy.record(trace,'gp-initial'),observation_count=len(observations))
        causal=None; before_first=None
        for i in range(max_proposals):
            cancelled()
            if i==0: before_first=deepcopy(observations)
            proposal,ask=policy.ask(np.array(working['x']),working_value,trace,f'ask-{i+1:03d}')
            if i==1:
                causal=policy.counterfactual(before_first,ask,trace)
            trace.event('proposal',number=i+1,x=proposal.tolist(),ask=ask['record'])
            row=observe(proposal,'proposal',i+1)
            if row is None:
                policy.silent_result(proposal)
                trace.event('retention',number=i+1,eligible=False,kept=False,silent=True,best_J0=best_value)
                continue
            # Policy keeps its own immutable training rows; a rejected sound still teaches it.
            policy.tell(row)
            posterior=policy.record(trace,f'gp-{i+1:03d}')
            trace.event('gp_update',number=i+1,posterior=posterior,observation_count=len(policy.rows))
            f=np.array(row['features']); value=float(f[:4].mean())
            feasible_phase=any(np.mean(r['features'][:4])<=1 for r in observations)
            working=min(observations,key=lambda r:float(proposal_loss(r['features'],feasible_phase)))
            working_value=float(np.mean(working['features'][:4]))
            eligible=allowed(f)
            keep=eligible and retention_rank(f)<retention_rank(best['features'])
            if keep: best=row; best_value=value; improvements+=1
            trace.event('retention',number=i+1,eligible=eligible,kept=bool(keep),J0=value,best_J0=best_value,
                        search_center=working['x'],search_center_J0=working_value)
            if i>=1 and np.all(np.array(best['features'][:4])<=1) and best_value<=baseline_value*.5: break
        if fade_refine and best is not baseline[0]:
            best,best_value=refine_fade(space,policy,best,base,allowed,observe,trace,fade_refine,cancelled)
        confirmations=[observe(np.array(best['x']),'confirmation',i) for i in range(3)]
        for row in confirmations: policy.add_confirmation(row)
        trace.event('gp_update',posterior=policy.record(trace,'gp-confirmed'),observation_count=len(policy.rows))
        worst=np.max([r['features'][:4] for r in confirmations],axis=0)
        improved=base[:4]-worst
        convergence=bool(np.all(worst<=1) and worst.mean()<=baseline_value*.5
                         and worst[2]<=base[2]*.7 and worst[3]<=base[3]*.7
                         and np.all(improved>2*noise[:4]))
        passed=bool(convergence and causal and causal['material'])
        result={'status':'PASS' if passed else 'FAIL','scope':'one offline hidden-target case' if driver.offline else 'live candidate search',
                'baseline':base.tolist(),'repeatability_std':noise.tolist(),'confirmation_worst':worst.tolist(),
                'convergence':convergence,'causal':causal,'best':best,'confirmations':confirmations,
                'incumbent_improvements':improvements,'observations':len(observations),'silent_candidates':len(silent),
                'elapsed_seconds':time.monotonic()-started,'selection_lambda':0,
                'musical_acceptance':False}
        if driver.offline or not convergence: driver.restore()
        else: driver.retain(space.decode(np.array(best['x'])))
    except Exception as exc:
        result={'status':'FAIL','failure':str(exc),'failure_type':type(exc).__name__,
                'observations':len(observations),'elapsed_seconds':time.monotonic()-started}
        try: driver.restore()
        except Exception as restore_exc: result['restore_error']=str(restore_exc)
    finally:
        cleanup=driver.cleanup()
        if result is not None:
            result['cleanup']=cleanup
            if not cleanup.get('verified'): result['status']='FAIL'
            save(driver.root/'result.json',result)
            trace.event('finished',status=result['status'],cleanup=cleanup)
    return result
