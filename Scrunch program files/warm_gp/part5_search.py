"""Independent warm GP, limited to the same local controls and four paid proposals."""
import numpy as np
from part3_search import FeatureGP


def propose(stored,observed,target,guess,dimensions,seed):
    rng=np.random.default_rng(seed)
    lo=np.maximum(0,np.array(guess)-.15);hi=np.minimum(1,np.array(guess)+.15)
    pool=rng.uniform(lo,hi,(512,2))
    if dimensions==1:pool[:,1]=guess[1]
    seen=np.array([r['x'] for r in observed])
    pool=pool[np.all(np.any(np.abs(pool[:,None,:]-seen[None,:,:])>.001,axis=2),axis=1)]
    rows=stored+observed
    x=np.column_stack([np.array([r['x'] for r in rows]),np.zeros(len(rows))])
    xp=np.column_stack([pool,np.zeros(len(pool))])
    y=np.array([r['features'] for r in rows]);target=np.array(target['features'])
    means=[];variances=[];fits=[]
    for j in range(11):
        gp=FeatureGP(x,y[:,j]);m,v=gp.predict(xp);means.append(m);variances.append(v)
        fits.append({'theta':gp.theta.tolist(),'nll':gp.nll,'initial_nll':gp.initial_nll,'amplitude':gp.amplitude})
    mean=np.array(means).T;sd=np.sqrt(np.array(variances).T)
    draws=mean[None,:,:]+sd[None,:,:]*rng.normal(size=(32,len(pool),11))
    best=min(np.sqrt(np.mean((np.array(r['features'])-target)**2)) for r in observed)
    ei=np.maximum(best-np.sqrt(np.mean((draws-target)**2,axis=-1)),0).mean(axis=0)
    index=int(np.argmax(ei)) if np.max(ei)>0 else int(np.argmin(np.mean((mean-target)**2,axis=1)))
    return pool[index].tolist(),fits
