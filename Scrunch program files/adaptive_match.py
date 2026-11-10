"""Match Reference entry and phrase-synchronised production Host driver."""
import base64
import json
from pathlib import Path
import time
import uuid

import numpy as np
from adaptive_sound import digest, save, search
from adaptive_render import phrase_from_clip


class LiveDriver:
    offline=False
    def __init__(self,c,target,stop):
        import engine
        self.c,self.stop=c,stop
        self.root=engine.DATA/'adaptive-search'/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.conn=c.a.wrapper(c.device)
        hello=self.conn.hello()
        if hello.get('child')!='Diva' or hello.get('searchProtocol')!=1:
            raise ValueError('This Host does not have the adaptive Diva search protocol; no fallback search ran')
        self.instance=hello['instance']; self.before_transport=c.a.transport()
        if self.before_transport['playing']:
            raise ValueError('Exact playback-position restore is not exposed by the bridge; adaptive live playback cannot start while it is already playing')
        loop=self.before_transport.get('loop')
        if not loop: raise ValueError('No pinned phrase region')
        track=c.a.track(c.track,clips=True)
        clips=[x for x in track['clips'] if x['start']<=loop['start'] and x['start']+x['span']>=loop['end']]
        if len(clips)!=1: raise ValueError('No unique MIDI phrase on the pinned target')
        self.clip=c.a.clip(clips[0]['id'])
        session=c.a.session()
        self.phrase=phrase_from_clip(self.clip,(loop['start'],loop['end']),session['tempo'],session['timeSignature'])
        self.phrase['tail_frames']=0; self.phrase['frames']=self.phrase['musical_frames']
        if abs(self.phrase['frames']/48000-target['durationSeconds'])>1/48000:
            raise ValueError('Reference cue and actual phrase lengths differ; nothing was cropped silently')
        # Phrase fidelity: the pinned notes must be everything that shapes playback at the instrument (bridge 2026-09-27.1).
        expression=self.clip.get('expression')
        if expression is None:
            raise ValueError('This bridge does not report phrase expression completeness (ScrunchBridge 2026-09-27.1 or newer); no live search ran')
        if self.clip.get('expressionComplete') is not True:
            raise ValueError('The pinned phrase has playback detail its notes do not carry: '+
                             '; '.join(expression['differs']+['unreadable: '+x for x in expression['unreadable']])+'; no live search ran')
        self.token=None; self.original=None; self.expected=None; self.restored=False; self.closed=False
        self.provenance={'wrapper':hello,'trackId':c.track,'deviceId':c.device,'clipId':self.clip['id'],'expression':expression}
        rec=self.conn.call('search_begin')
        self.token=rec['search_token']; self.scope=rec['scope']
        try:
            # Which of the Host's safe controls this sound uses, measured offline on the pinned phrase from the
            # Host's own current state; the search spends its budget only on those (search_activity).
            import search_activity
            from adaptive_render import OfflineDriver
            state=self.root/'source.state'; state.write_bytes(base64.b64decode(self.conn.state()['data']))
            offline=OfflineDriver(self.root/'activity',state,self.phrase,scope=self.scope,case='activity',split='activity')
            try: self.activity=search_activity.measure(offline)
            finally: offline.cleanup()
            save(self.root/'activity.json',self.activity)
            self.scope=search_activity.restrict(self.scope,self.activity)
            if not self.scope['controls']: raise ValueError('No control in the safe scope changes this sound; no live search ran')
        except BaseException:
            self.conn.call('search_finish',search_token=self.token); self.token=None
            raise
        self.provenance['activity']={k:self.activity[k] for k in ('repeatDifferenceDb','threshold')}
        self.provenance['activeControls']=[c['id'] for c in self.scope['controls']]
        self.original={x['id']:x['original'] for x in self.scope['controls']}; self.expected=dict(self.original)

    def render(self,values,role,index):
        if self.stop and self.stop(): raise InterruptedError('Cancelled')
        if self.conn.hello()['instance']!=self.instance: raise ValueError('Host identity changed')
        if self.c.a.clip(self.clip['id'])!=self.clip: raise ValueError('Phrase changed')
        changes=[{'id':k,'value':v,'expect':self.expected[k]} for k,v in values.items() if abs(v-self.expected[k])>1e-7]
        if changes: self.conn.call('set',search_token=self.token,changes=changes)
        actual={p['id']:p['value'] for p in self.conn.params()}
        if any(abs(actual[k]-v)>1e-6 for k,v in values.items()): raise ValueError('Unverified candidate write')
        self.expected={k:actual[k] for k in self.original}
        start,end=self.phrase['region_beats']
        self.conn.call('search_arm',search_token=self.token,beat=start,tempo=self.phrase['tempo'])
        self.c.a.play_arrangement(start,loop=None)
        try:
            cap=self.conn.call('search_capture',search_token=self.token,seconds=self.phrase['frames']/48000,timeout=25)
        finally: self.c.a.stop_playback()
        audio=np.frombuffer(base64.b64decode(cap.pop('pcm')),dtype='<f4').reshape(-1,2)
        out=self.root/'audio'/f'{role}-{index:03d}'; out.mkdir(parents=True)
        import soundfile as sf
        sf.write(out/'audio.wav',audio,int(cap['sampleRate']),subtype='FLOAT')
        save(out/'record.json',{'capture':cap,'values':actual,'phrase_sha256':digest(self.phrase)})
        return audio,int(cap['sampleRate']),{'values':actual,'audio':str(out/'audio.wav'),'record':str(out/'record.json')}

    def restore(self):
        if self.token:
            rec=self.conn.call('search_restore',search_token=self.token)
            self.restored=bool(rec.get('exact'))
            if not self.restored: raise ValueError('Original was not restored exactly')

    def retain(self,values):
        changes=[{'id':k,'value':v,'expect':self.expected[k]} for k,v in values.items() if abs(v-self.expected[k])>1e-7]
        if changes: self.conn.call('set',search_token=self.token,changes=changes)
        self.expected=dict(values)

    def cleanup(self):
        try:
            if self.token and not self.closed:
                self.conn.call('search_finish',search_token=self.token); self.closed=True
            if self.c.a.transport()!=self.before_transport:
                raise ValueError('Transport/loop restoration was not verified')
            return {'verified':True,'scope_closed':self.closed,'restored':self.restored}
        except Exception as exc: return {'verified':False,'why':str(exc)}


def run(c,target,rid=None,generation=None,*,driver=None,stop=None,tries=None,restore=False,plan_only=False):
    import brooklyn_rose
    if plan_only:
        return {'plan':{'engine':'adaptive-gp','status':'requires measured baseline'},'result':None,
                'say':'Adaptive search uses the pinned phrase and native timbre allow-list; no candidate has been measured.'}
    if stop is None:
        def stop():
            if brooklyn_rose._cancel_requested(): return True
            if rid is not None:
                import action_layer
                return not action_layer.cancel_clear(rid,generation)[0]
            return False
    before=None
    if driver is None:
        import reference_target, audition_pair
        before=audition_pair.snapshot(c)
        driver=LiveDriver(c,target,stop)
        audio=reference_target._read_cue(target)
    else: audio=target
    result=search(driver,audio,max_proposals=min(56,tries if tries is not None else 56),stop=stop,fade_refine=16)
    if before is not None and result.get('cleanup',{}).get('verified') and result.get('convergence'):
        import audition_pair
        pair=audition_pair.commit(c,before,'adaptive-search',rid or '')
        if pair: pair['scope']=driver.scope; audition_pair._save()
    plan={'engine':'adaptive-gp','planId':driver.root.name,'controlsInSpace':len(driver.scope['controls']),
          'phrase_sha256':digest(driver.phrase),'scope_sha256':digest(driver.scope),'preset_blends':False}
    return {'plan':plan,'result':result,'say':('Adaptive search '+result['status']+'. '+
            ('Original restored.' if result.get('cleanup',{}).get('restored') else 'See verified candidate and component measurements.'))}
