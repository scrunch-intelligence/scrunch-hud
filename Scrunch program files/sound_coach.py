"""Sound coach: the hands on the synth. The AI engineer (sound_language) decides; this applies and measures.

Moves: sweep a knob (the bridge moves it smoothly), stop ("that's it", verified position),
back a bit, other way, A/B between two positions, keep. Every change is a verified bridge
write; every kept choice is appended to <data>/coach/history.jsonl with the knob, the
before/after display values and the user's words, so the user's vocabulary can be learned later.

Lessons are short general synthesis explanations written for Scrunch (not vendor text and
not verified per preset). FX-slot knobs only act when that slot holds the matching effect,
which Live cannot read; the coach says so.
"""
import json,math,re,time
from pathlib import Path
import sound_moves,sound_audio,sound_prefs,sound_language,sound_levels,sound_export

def parse_db(display):
    """The dB number out of a knob's display text, or None when it is not shown in dB."""
    m=re.search(r'(-?\d+(?:\.\d+)?)\s*dB',str(display or ''))
    return float(m.group(1)) if m else None

def compensation(gap_db,slope_db_per_unit,span,limit=.25):
    """How far to move a volume knob to cancel a loudness gap, capped at a fraction of its range."""
    if not slope_db_per_unit:return 0.
    return max(-limit*span,min(limit*span,-gap_db/slope_db_per_unit))

def db(after,before):
    """Level change in dB between two meter readings; None when either is too quiet to compare."""
    if not after or not before or after<=1e-5 or before<=1e-5:return None
    return round(20*math.log10(after/before),2)

PHRASE_NAME='Scrunch Coach Phrase'
# Two bars of A-minor offbeats (quarter-note beats): a neutral test phrase, not a composition.
PHRASE=[{'p':p,'t':bar*4+beat+.5,'d':.25,'v':100} for bar,(p,q) in enumerate(((69,72),(67,71))) for beat in range(4) for p in (p,q)]
FX_NOTE='FX slot knob: it only changes the sound when that slot holds the matching effect in this preset.'
def fx_lesson(setting,kind):
    import re
    m=re.match(r'FX(\d)_(.*)$',setting or '')
    if not m:return None
    head='Effect slot %s holds %s. '%(m.group(1),kind) if kind else ''
    return head+'This knob belongs to that effect.' if kind else FX_NOTE

def suggestions(knowledge,preset_name,exposed_settings,limit=8):
    """Settings the loaded preset's analysed family distinguishes, limited to exposed knobs."""
    import arturia_presets
    entry=next((p for p in (knowledge or {}).get('presets',[]) if p['name']==preset_name),None)
    family=arturia_presets.family(entry) if entry else None
    distinctive=[d['parameter'] for d in ((knowledge or {}).get('families',{}).get(family,{}).get('distinctive',[]) if family else [])
                 if d['parameter'] in exposed_settings]
    picked=[]
    for s in distinctive:
        if s not in picked:picked.append(s)
    return family,picked[:limit]

def lesson(setting):
    if setting.startswith('FX'):return FX_NOTE
    return None

def clean(display):
    """A control's display text without the control characters some synths put in their units (Diva's \x01)."""
    if display is None:return None
    return ' '.join(''.join(ch for ch in str(display) if ch.isprintable()).split())

def _label(setting,name,kind=None):
    if setting[:2]=='FX' and setting[2:3].isdigit() and name.startswith('FX '):
        return 'FX %s %s%s'%(setting[2],name[3:],' (%s)'%kind if kind and setting.endswith(('Dry_Wet','SendLevel','Bypass','RoutingMode')) else '')
    return name

def synth_choices(synth):
    """Installed files a patch may refer to (Serum 2 wavetables). Relative to the synth's own content folder."""
    from pathlib import Path
    if synth in ('Serum 2','Serum2'):
        root=Path('/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Tables')   # patch paths are relative to this
        tables=sorted(str(p.relative_to(root)) for p in root.rglob('*.wav')) if root.is_dir() else []
        return {'wavetables':tables[:400]} if tables else None
    return None

def host_step(plugin,track):
    """Plain words when the synth is not in Scrunch Host: what that blocks and the one step that fixes it."""
    return ('%s on %s is not inside Scrunch Host, so Scrunch cannot read or change its controls from the live Set. '
            'One step moves it, without saving or reopening your Set: double-click "Scrunch Host This Synth" on the '
            'Desktop with that track selected (it runs host_migrate.py).'%(plugin or 'The synth',track or 'this track'))

class Coach:
    """One coaching session on one plug-in device. `adapter` is the bridge adapter, `manifest` the session.json."""
    def __init__(self,adapter,manifest,vendor,data_dir,clock=time.time,knowledge=None,source='user',interpreter=None):
        self.a=adapter;self.m=manifest;self.clock=clock;self.knowledge=knowledge
        self._source=None;self.source=source                # 'test' keeps automated runs out of what is learned
        self.interpreter=interpreter                        # the AI engineer: decides every sound change
        self.levels=sound_levels.Cache(clock=clock)         # measured loudness, reused only while nothing has changed
        self.setting={i:n for i,n,_ in vendor}
        self.knobs=[(k['id'],k['name']) for k in manifest.get('exposedKnobs',[])]
        if not self.knobs:raise ValueError('This session exposes no plug-in knobs to coach.')
        self.data_dir=Path(data_dir)
        self.device=None;self.sweep=None;self.last=None;self.ab=None;self.origin=None;self.move=None
        self.prefs=sound_prefs.learn(sound_prefs.read(self.user_history))

    # ---- discovery ----
    def instances(self):
        """Every copy of this plug-in in the Set: which track it is on, and whether Scrunch can work it.

        A Set can hold the same synth several times - a bass, a lead, a pad. Only an instance whose
        knobs are exposed in this Set can be moved, so that is reported per instance rather than the
        whole Set being refused because of a copy nobody asked about.
        """
        import plugin_knobs
        out=[]
        for t in self.a.session()['tracks']:
            for d in self.a.devices(t['id']):
                if self.m.get('plugin') not in (d.get('deviceName'),d['name']):continue
                # A plug-in update can rename knobs without moving them, so readiness is judged on
                # how many Scrunch knobs the instance exposes, not on their labels.
                live=self.a.device(d['id'])['parameters']
                live=[x for x in live if not (x['name']=='Device On' and x is live[0])]
                ready=len(live)==len(self.knobs)
                out.append({'track':t['id'],'trackName':t.get('name'),'device':d['id'],
                            'deviceName':d.get('name'),'knobsExposed':ready,
                            'inUse':d['id']==self.device})
        return out
    def ensure_device(self):
        """Check the instrument this coach is holding on to is still the one in Live.

        Live hands out fresh ids every time a Set is opened, so a coach that survives a restart
        points at devices that no longer exist - which surfaces as 'Track no longer exists' on
        the next thing the producer asks for. Checking costs one session read; being wrong is a
        confusing failure in the middle of working.
        """
        if not self.device:return None
        try:known=[i['id'] for i in self.a.devices(self.track)]
        except ValueError as exc:
            if 'no longer exists' not in str(exc):raise
            known=[]
        if self.device in known:return self.device
        self.device=None;self.track=None
        self.sweep=None;self.last=None;self.ab=None;self.origin=None;self.move=None
        self.chat_last_change={};self.chat_question=None;self.chat_changes={}
        self.levels.drop_all('the session was reopened, so everything measured is out of date')
        return self._device()

    def use(self,device):
        """Work on this instance from now on. Everything measured about the last one is dropped."""
        found=[i for i in self.instances() if i['device']==device]
        if not found:raise ValueError('That %s is not in this Set any more.'%self.m.get('plugin'))
        if not found[0]['knobsExposed']:
            raise ValueError('The knobs on %r are not exposed in this Set, so Scrunch cannot move them yet.'
                             %(found[0]['trackName'] or 'that track'))
        if device!=self.device:
            self.device=device;self.track=found[0]['track']
            self.sweep=None;self.last=None;self.ab=None;self.origin=None;self.move=None
            self.chat_last_change={};self.chat_question=None;self.chat_changes={}
            self.levels.drop_all('a different instrument is being worked on')
        return found[0]
    def _device(self):
        found=self.instances()
        plugin=self.m.get('plugin')
        if not found:raise ValueError('There is no %s in this Set for the coach to work on.'%plugin)
        ready=[f for f in found if f['knobsExposed']]
        if len(found)==1:pick=found[0]
        else:
            chosen=None
            try:
                sel=self.a.selected_track()                    # an id, or a track record carrying one
                chosen=sel.get('id') if isinstance(sel,dict) else sel
            except Exception:pass
            on_selected=[f for f in ready if f['track']==chosen] or [f for f in found if f['track']==chosen]
            pick=on_selected[0] if len(on_selected)==1 else ready[0] if len(ready)==1 else None
            if pick is None:
                names=', '.join(repr(f['trackName'] or f['track']) for f in found)
                raise ValueError('There are %d copies of %s in this Set (%s). Select the track you want to '
                                 'work on in Live, or pick one on the page.'%(len(found),plugin,names))
        if not pick['knobsExposed']:
            raise ValueError('The knobs on %r are not exposed in this Set, so Scrunch cannot move them yet.'
                             %(pick['trackName'] or 'that track'))
        self.track,self.device=pick['track'],pick['device']
        return self.device
    def _params(self):
        import plugin_knobs
        exposed=self.a.device(self.device or self._device())['parameters']
        try:return plugin_knobs.check_exposed(exposed,self.knobs)
        except ValueError:
            # A plug-in update can rename knobs (Pigments renamed 12 of them). Same ids, same
            # order, new labels: adopt them and record the renames rather than refusing to work.
            updated=plugin_knobs.adopt_live_names(self.m,exposed)
            self.m=updated;self.knobs=[(k['id'],k['name']) for k in updated['exposedKnobs']]
            record=Path(self.m['path']).parent/'session.json' if self.m.get('path') else None
            if record is not None and record.is_file():
                tmp=record.with_suffix('.tmp');tmp.write_text(json.dumps(updated,indent=2));tmp.replace(record)
            return plugin_knobs.check_exposed(exposed,self.knobs)
    def knobs_view(self):
        self.ensure_device()
        out=[]
        for p,(ident,name) in zip(self._params(),self.knobs):
            setting=self.setting.get(ident,'')
            kind=(self.m.get('fxSlots') or {}).get(setting[2:3]) if setting.startswith('FX') else None
            out.append({'parameter':p['id'],'name':name,'label':_label(setting,name,kind),'setting':setting,'value':p['value'],'group':p.get('group'),
                        'display':clean(p.get('display')),'min':p['min'],'max':p['max'],'stepped':bool(p.get('quantized')),
                        'lesson':(fx_lesson(setting,kind) if setting.startswith('FX') and (kind or self.m.get('fxSlots') is not None) else lesson(setting))})
        family,first=suggestions(self.knowledge,(self.m.get('preset') or {}).get('name'),{k['setting'] for k in out})
        for k in out:k['suggested']=first.index(k['setting'])+1 if k['setting'] in first else None
        self.family=family
        return out
    def _knob(self,parameter):
        k=next((k for k in self.knobs_view() if k['parameter']==parameter),None)
        if k is None:raise ValueError('That knob is not exposed in this session.')
        return k

    # ---- listening (Arrangement View only) ----
    def phrase(self):
        """Use the track's existing music; supply a phrase only when the track is empty."""
        self.device or self._device()
        info=self.a.track(self.track,clips=True)
        mine=[c for c in info.get('clips',[]) if c.get('name')==PHRASE_NAME]
        existing=mine or info.get('clips',[])
        if existing:
            clip=existing[0];start=clip.get('start',0)
            return {'clip':clip['id'],'created':False,'start':start,'length':clip.get('span',clip.get('length',8))}
        clip=self.a.create_arrangement_clip(info['trackIndex'],8,PHRASE,start=0,name=PHRASE_NAME)
        return {'clip':clip.get('id') if isinstance(clip,dict) else clip,'created':True}
    def play(self):
        self.ensure_device()
        phrase=self.phrase();start=phrase.get('start',0);length=phrase.get('length',8)
        r=self.a.play_arrangement(start,loop=(start,start+length))
        if not r.get('playing'):raise ValueError('Ableton did not confirm playback.')
        return {'playing':True,'loop':r.get('loop')}
    def halt(self):
        return {'playing':bool(self.a.stop_playback().get('playing'))}

    # ---- moves ----
    def start(self,parameter,direction='up',seconds=6.0):
        if direction not in ('up','down'):raise ValueError('Direction must be up or down.')
        k=self._knob(parameter)
        if k['stepped']:raise ValueError('%s is a switch or stepped control; use A/B instead of a sweep.'%k['label'])
        if not self.origin or self.origin['parameter']!=parameter:self.origin=k;self.ab=None
        target=k['max'] if direction=='up' else k['min']
        self.a.start_sweep(self.device,parameter,k['name'],target,seconds,expect_value=k['value'])
        self.sweep={'knob':k,'direction':direction,'seconds':seconds}
        return {'sweeping':True,'label':k['label'],'from':k['display'],'direction':direction,'lesson':k['lesson']}
    def stop(self):
        if not self.sweep:raise ValueError('Nothing is sweeping.')
        r=self.a.stop_sweep();k=self.sweep['knob']
        self.last={'knob':k,'direction':self.sweep['direction'],'value':r['value'],'display':r.get('display')};self.sweep=None
        return {'label':k['label'],'from':k['display'],'now':r.get('display'),'value':r['value'],'lesson':k['lesson']}
    def back(self,fraction=.04):
        """Step back against the last sweep direction (reaction time), by a fraction of the knob's range."""
        if not self.last:raise ValueError('Stop a sweep first.')
        k=self.last['knob'];span=k['max']-k['min'];step=span*fraction*(-1 if self.last['direction']=='up' else 1)
        value=min(k['max'],max(k['min'],self.last['value']+step))
        r=self.a.set_device_parameter(self.device,k['parameter'],k['name'],value=value,expect_value=self.last['value'])
        self.last.update(value=r['value'],display=r.get('display'))
        return {'label':k['label'],'now':r.get('display'),'value':r['value']}
    def other_way(self,seconds=None):
        if not self.last:raise ValueError('Stop a sweep first.')
        return self.start(self.last['knob']['parameter'],'down' if self.last['direction']=='up' else 'up',seconds or 6.0)

    # ---- A/B ----
    def ab_set(self,parameter,a,b):
        k=self._knob(parameter)
        for v in (a,b):
            if not k['min']<=v<=k['max']:raise ValueError('A and B must be within the knob\'s range.')
        if not self.origin or self.origin['parameter']!=parameter:self.origin=k
        self.ab={'knob':k,'A':a,'B':b};self.last=None;return self.ab_play('A')
    def ab_play(self,side):
        if not self.ab or side not in ('A','B'):raise ValueError('Set up A and B first.')
        k=self.ab['knob'];cur=self._knob(k['parameter'])['value']
        r=self.a.set_device_parameter(self.device,k['parameter'],k['name'],value=self.ab[side],expect_value=cur)
        self.ab['playing']=side;return {'label':k['label'],'side':side,'display':r.get('display')}

    # ---- instructions: one coordinated move, steered by ear ----
    def decide(self,words,conversation=None,heard=None,taste=None,after=None,knobs=None,extra=None):
        """Ask the AI engineer what to do. Returns (validated reply, usage). Nothing is written here."""
        if not self.interpreter:raise ValueError('No model is set up for the engineer; choose one in Settings.')
        knobs=knobs if knobs is not None else self.knobs_view()
        last=None
        if self.move:
            last={'instruction':self.move['instruction'],'says':self.move.get('says'),
                  'moved':[{'setting':s['setting'],'from':s['display'],'why':s['why']} for s in self.move['plan']['steps']],
                  'expect':self.move.get('expect')}
        elif getattr(self,'undo_patch',None) is not None and getattr(self,'last_patch',None):
            last=dict(self.last_patch,note='stored patch data you edited; revert puts the whole patch back as it was')
        elif getattr(self,'lanes_undo',None) is not None and getattr(self,'last_automation',None):
            last=dict(self.last_automation,note='automation you wrote; revert puts the lanes back as they were')
        ctx=sound_language.context(self,knobs,conversation=conversation,heard=heard,last=last,taste=taste,after=after,extra=extra)
        groups=[k.get('group') for k in knobs]
        focus,focus_used=sound_language.focus(words,ctx,self.fast_profile(),groups=groups)
        answer=self.interpreter(str(words),sound_language.focused(ctx,focus,groups) if focus else ctx)
        used=None
        if isinstance(answer,tuple):answer,used=answer                # (reply, usage) from the provider
        if focus and isinstance(answer,dict) and answer.get('action')=='look':
            # The engineer asked to see sections the quick pass left out: one more look, with them added.
            wanted=set(answer.get('sections') or [])
            answer=self.interpreter(str(words),sound_language.focused(ctx,{'sections':set(focus['sections'])|wanted,
                                                                           'patchSections':set(focus['patchSections'])|wanted},groups))
            if isinstance(answer,tuple):answer,second=answer;used=[u for u in (used,second) if u]
            if isinstance(answer,dict) and answer.get('action')=='look':      # still not enough: show everything
                answer=self.interpreter(str(words),ctx)
                if isinstance(answer,tuple):answer,third=answer;used=[u for u in (used if isinstance(used,list) else [used])+[third] if u]
        if focus_used:used=[u for u in ((used if isinstance(used,list) else [used])+[focus_used]) if u]
        return sound_language.validate(answer,knobs),used
    def fast_profile(self):
        """The producer's fast model on the engineer's provider, or None."""
        try:
            import engine
            st=engine.STATE;model=str(st.get('engineerFastModel') or '').strip()
            profile=sound_language.selected(st)
            return dict(profile,model=model) if model and profile else None
        except Exception:return None
    def change(self,reply,words,knobs=None):
        """Carry out the engineer's change: optionally put the last one back, then move to its targets."""
        out={}
        if reply.get('revertFirst') and self.move:out=self.steer('revert')
        elif self.move:self.move=None                           # building on the last change: it stays where it is
        targets=reply.get('targets') or []
        if not targets:return dict(out,interpreted=True,says=reply.get('say'))
        knobs=self.knobs_view()                                 # after any revert, positions have changed
        by={k['parameter']:k for k in knobs}
        targets=[dict(t,knob=by.get(t['knob']['parameter'],t['knob'])) for t in targets]
        plan=sound_moves.target_plan(targets,name=str(words)[:40].strip().lower() or 'change')
        if not plan['steps']:return dict(out,knobs=[],interpreted=True,says=reply.get('say'))
        self.move={'plan':plan,'instruction':str(words)[:300],'amount':1.,'trajectory':[],
                   'interpreted':True,'says':reply.get('say'),'expect':reply.get('expect'),
                   'snapshot':{k['setting']:{'value':k['value'],'display':k.get('display')} for k in knobs}}
        return dict(self._apply(1.,knobs),interpreted=True,says=reply.get('say'))
    def instruct(self,words,amount=None):
        """One request with no conversation around it (the older coach page): the AI decides, Scrunch applies."""
        reply,used=self.decide(words)
        if reply['action']=='question':
            return {'question':reply['question'],'instruction':str(words)[:300],'interpreted':True,'modelUsage':used}
        if reply['action']=='revert':return dict(self.steer('revert'),modelUsage=used)
        if reply['action']!='change':return {'says':reply.get('say'),'knobs':[],'modelUsage':used}
        return dict(self.change(reply,words),modelUsage=used)

    # ---- automation inside Scrunch Host (any control, beyond Live's 128) ----
    def _hosted(self):
        import wrapper_backend
        if isinstance(self.a,wrapper_backend.WrappedAdapter) and self.a.hosts(self.device or self._device()):
            return self.a.wrapper(self.device)
        return None
    def automation_context(self,knobs=None):
        """What the engineer needs to write automation: the song position and the lanes already there."""
        try:w=self._hosted()
        except Exception:return None
        if w is None:return None
        by={k['parameter']:(k['setting'] or k['name']) for k in (knobs if knobs is not None else self.knobs_view())}
        lanes=[{'setting':by.get('w:'+str(l['id']),l['name']),'points':[[round(b,3),round(v,3)] for b,v in l['points']][:64]}
               for l in w.lanes()]
        out={'songBeat':round(w.hello().get('beat') or 0.,2),'automation':lanes}
        try:
            loop=(self.a.transport() or {}).get('loop')
            if loop:out['loop']={'startBeat':loop['start'],'endBeat':loop['end']}
        except Exception:pass
        return out
    def automate(self,reply):
        """Write the engineer's lanes into the wrapper. The lanes it replaced are kept so revert puts them back."""
        w=self._hosted()
        if w is None:raise ValueError('Automation beyond Live\'s page needs this synth inside Scrunch Host.')
        before=w.lanes()
        self.values_undo=[{'id':l['knob']['parameter'],'name':l['knob']['name'],'value':l['knob']['value']} for l in reply['lanes']]
        lanes=[{'id':l['knob']['parameter'][2:],'points':[[b,to] for b,to in l['points']]} for l in reply['lanes']]
        w.set_lanes(lanes)
        self.lanes_undo=[{'id':l['id'],'points':l['points']} for l in before]
        self.move=None
        self.last_automation={'instruction':reply.get('say'),'automated':[{'setting':l['knob']['setting'] or l['knob']['name'],
                              'points':[[b,round(to,3)] for b,to in l['points']],'why':l['why']} for l in reply['lanes']]}
        rows=[{'label':l['knob']['label'],'points':len(l['points']),'fromBeat':l['points'][0][0] if l['points'] else None,
               'toBeat':l['points'][-1][0] if l['points'] else None,'why':l['why']} for l in reply['lanes']]
        return {'automation':rows}
    # ---- patch data that is not a parameter (mod routings, FX chain, wavetables, sequencer steps) ----
    PATCH_LIMIT=1500
    def _patch_state(self):
        import base64,synth_state
        w=self._hosted()
        if w is None:return None,None
        child=w.hello().get('child')
        try:return w,synth_state.open_state(child,base64.b64decode(w.state()['data']))
        except ValueError:return w,None
    def patch_context(self):
        """The stored values no control reaches, as "path = value" lines, plus allowed file choices."""
        import synth_state.catalogue as cat
        try:w,st=self._patch_state()
        except Exception:return None
        if st is None:return None
        known=cat.load(st.synth)
        only=set(known['patchOnly']) if known else None
        values=st.values()
        derived=('numFrames','numChannels','sampleRate','hash','product','productVersion','url','vendor','version','component')
        # Interface/MIDI-setup state, not sound: selected tabs, locked columns, pages, channels.
        interface=('Tab','Selected','View','UIParam','Page','Locked','LastActive','MidiChannel','MIDIChannel','Channel')
        rows=['%s = %s'%(k,v) for k,v in values.items() if (only is None or k in only) and isinstance(v,(int,float,str))
              and not isinstance(v,bool) and len(str(v))<=120 and v!='default' and k.split('/')[-1] not in derived and not any(t in k for t in interface)][:self.PATCH_LIMIT]
        out={'patchData':rows}
        choices=synth_choices(st.synth)
        if choices:out['patchChoices']=choices
        return out
    def patch(self,reply):
        """Apply the engineer's edits to the stored patch; everything is checked against what is really stored."""
        import base64
        w,st=self._patch_state()
        if st is None:raise ValueError('This synth\'s stored patch data cannot be edited here.')
        before=base64.b64decode(w.state()['data'])
        values=st.values();choices=synth_choices(st.synth) or {}
        allowed_files={f for files in choices.values() for f in files}
        rows=[]
        for e in reply['edits']:
            path,value=e['path'].split(' = ')[0].strip(),e['value']
            if path not in values:raise ValueError('%s is not stored in this patch; nothing was changed.'%path)
            old=values[path]
            if isinstance(old,str):
                if not isinstance(value,str):raise ValueError('%s takes a name, not a number; nothing was changed.'%path)
                if path.endswith(('relativePathToWT','relativePathToSample')) and value not in allowed_files:
                    raise ValueError('%s is not an installed file; nothing was changed.'%value)
            elif isinstance(old,bool) or not isinstance(value,(int,float)):
                raise ValueError('%s takes a number; nothing was changed.'%path)
            st.set(path,value);rows.append({'label':path,'was':str(old),'now':str(value),'why':e['why']})
        w.set_state(base64.b64encode(st.blob()).decode())
        import synth_state
        back=synth_state.open_state(st.synth,base64.b64decode(w.state()['data'])).values()
        for r,e in zip(rows,reply['edits']):
            r['now']=str(back.get(r['label']))
        self.undo_patch=before;self.move=None
        self.last_patch={'instruction':reply.get('say'),'edited':[{k:r[k] for k in ('label','was','now','why')} for r in rows]}
        return {'knobs':rows,'patch':True}
    def host_in_wrapper(self):
        """Whether this synth is in Scrunch Host; if not, the one step that puts it there (never run from here)."""
        if self._hosted() is not None:
            return {'say':'%s is already inside Scrunch Host.'%self.m.get('plugin')}
        return {'say':host_step(self.m.get('plugin'),self.m.get('trackName')),'hosted':False}
    def load_preset(self,path):
        """Load a preset into the synth inside Scrunch Host and read it back from the host. No save, no reload."""
        import base64,synth_state,uhe_presets
        plugin=self.m.get('plugin');w=self._hosted()
        if w is None:raise ValueError(host_step(plugin,self.m.get('trackName')))
        if plugin!='Diva':raise ValueError('Loading %s presets through Scrunch Host is not built yet (Diva only).'%plugin)
        preset=uhe_presets.load(path)
        before=base64.b64decode(w.state()['data'])
        blob=synth_state.juce_state.build({'IComponent':uhe_presets.state(Path(preset['path']).read_bytes())})
        w.set_state(base64.b64encode(blob).decode())
        import time as _t
        deadline=_t.time()+3.0
        while True:                    # Diva can apply a new state a moment after accepting it: read until it shows
            back=synth_state.open_state(plugin,base64.b64decode(w.state()['data'])).values()
            if _t.time()>deadline or all(back.get(k)==v or (isinstance(back.get(k),(int,float)) and abs(float(back[k])-v)<=1e-3)
                                         for k,v in preset['parameters'].items() if k in back and k not in uhe_presets.RENUMBERED):break
            _t.sleep(.1)
        # Diva renumbers its string-table references (MIDI-learn, microtuning, trimpot files) and revision stamp
        # every time it writes its state, so those are not compared; neither are keys the state does not hold.
        want={k:v for k,v in preset['parameters'].items() if k not in uhe_presets.RENUMBERED}
        wrong=sorted(k for k,v in want.items() if k in back and isinstance(back[k],(int,float)) and abs(float(back[k])-v)>1e-3)
        missing=sorted(k for k in want if k not in back)
        want={k:v for k,v in want.items() if k in back}
        missing=[]
        self.undo_patch=before;self.move=None
        self.last_patch={'instruction':'preset %s'%preset['name'],'edited':[]}
        self.m['preset']={'name':preset['name'],'path':preset['path']}
        return {'preset':preset['name'],'path':preset['path'],'settingsInPreset':len(want),
                'settingsReadBack':len(want)-len(wrong),'differing':wrong[:12],
                'verified':not wrong,
                'say':'Loaded %s into %s in Scrunch Host; %d of %d settings read back.'%(preset['name'],plugin,len(want)-len(wrong),len(want))}
    def revert_patch(self):
        import base64
        undo=getattr(self,'undo_patch',None);w=self._hosted()
        if undo is None or w is None:return {}
        w.set_state(base64.b64encode(undo).decode())
        rows=[{'label':r['label'],'was':r['now'],'now':r['was'],'why':''} for r in (self.last_patch or {}).get('edited',[])]
        self.undo_patch=None;self.last_patch=None
        return {'knobs':rows,'reverted':True}
    def revert_automation(self):
        w=self._hosted()
        undo=getattr(self,'lanes_undo',None)
        if w is None or undo is None:return {}
        w.set_lanes(undo,replace=True);self.lanes_undo=None;self.last_automation=None
        values=getattr(self,'values_undo',None) or []
        if values and not undo:            # no lanes left: the controls go back to where they were
            now={k['parameter']:k['value'] for k in self.knobs_view()}
            self.a.set_device_parameters_together(self.device,[dict(v,expect_value=now[v['id']]) for v in values if v['id'] in now])
        self.values_undo=None
        return {'automationReverted':True}

    def _apply(self,amount,knobs=None):
        if not self.move:raise ValueError('No move to apply.')
        knobs=knobs or self.knobs_view()
        writes=sound_moves.writes(self.move['plan'],amount,knobs)
        applied=self.a.set_device_parameters_together(self.device or self._device(),writes) if writes else []
        self.move['amount']=amount;self.move['applied']=applied
        self.move['trajectory'].append({'at':self.clock(),'amount':amount,
                                        'values':[{'setting':s['setting'],'display':p.get('display')} for s,p in zip(self.move['plan']['steps'],applied)]})
        self.origin=None;self.last=None;self.ab=None      # a move owns the knobs until it is kept or reverted
        return {'move':self.move['plan']['move'],'instruction':self.move['instruction'],'amount':amount,
                'knobs':[{'label':s['label'],'parameter':s['parameter'],'was':s['display'],'now':clean(p.get('display')),'why':s['why']}
                         for s,p in zip(self.move['plan']['steps'],applied)],
                'lines':sound_moves.explain(self.move['plan'],applied)}
    def steer(self,direction,step=.25):
        """more / less / wrong direction / revert, recomputed from the values the move started at."""
        if not self.move:raise ValueError('Ask for a change first, then steer it.')
        if direction=='revert':
            out=self._apply(0.);self._journal('revert')
            import level_match
            try:level_match.undo(self)              # the fader goes back with the change it made up for
            except Exception:pass
            self.move=None;return dict(out,reverted=True)
        if direction=='wrong direction':
            amount=self.move['amount'] or 1.
            self._apply(0.);self._journal('wrong-direction',turnedTo='reversed')
            for step in self.move['plan']['steps']:step['direction']*=-1
            for step in self.move['plan']['steps']:
                knob=next((k for k in self.knobs_view() if k['parameter']==step['parameter']),None)
                if knob:                                    # room the other way, from where it started
                    position=(knob['value']-knob['min'])/((knob['max']-knob['min']) or 1)
                    room=(1-position) if step['direction']>0 else position
                    step['travel']=min(step['travel'],room*(knob['max']-knob['min']))
            self.move['expect']=None                        # the audio test no longer applies
            out=self._apply(amount)
            return dict(out,turnedAround=True)
        if direction not in ('more','less'):raise ValueError('Steer with more, less, wrong direction or revert.')
        amount=self.move['amount']+(step if direction=='more' else -step)
        if amount<=0:return self.steer('revert')
        return self._apply(min(1.,amount))
    def _trim_step(self,delta):
        """Put a volume trim of `delta` into the move itself, so only the moved version changes and revert undoes it."""
        steps=self.move['plan']['steps']
        knob=next((k for k in self.knobs_view() if k['setting']=='Amp_Level'),None)
        if knob is None:raise ValueError('This patch exposes no Volume knob to trim.')
        step=next((s for s in steps if s['setting']=='Amp_Level'),None)
        if step is None:
            step={'parameter':knob['parameter'],'name':knob['name'],'setting':'Amp_Level','label':knob['label'],
                  'origin':knob['value'],'direction':1,'travel':0.,'display':knob['display'],
                  'why':'level-matched so the change is not just louder'}
            steps.append(step)
        step['direction']=1 if delta>=0 else -1
        step['travel']=abs(delta)/max(1e-6,sound_moves.REACH*max(.05,self.move['amount']))
        return step

    def forget_levels(self,why='patch changed'):
        """Throw away every measured offset, for when the patch or the session changes underneath."""
        return self.levels.drop_all(why)
    def keep_move(self,words=''):
        """Accept the move where it stands and journal it with the patch it started from and the whole trajectory."""
        if not self.move:raise ValueError('No move to keep.')
        m=self.move;event={'at':self.clock(),'kind':'move','session':self.m.get('sessionId'),'plugin':self.m.get('plugin'),
               'preset':(self.m.get('preset') or {}).get('name'),'instruction':m['instruction'],'move':m['plan']['move'],
               'amount':m['amount'],'words':str(words)[:500],'trajectory':m['trajectory'],
               'steps':[{'setting':s['setting'],'knob':s['label'],'from':s['display'],'why':s['why']} for s in m['plan']['steps']],
               'interpreted':bool(m.get('interpreted')),'patchBefore':m['snapshot']}
        self._write_journal(event);self.move=None
        return event
    def _journal(self,kind,**extra):
        m=self.move or {}
        self._write_journal({'at':self.clock(),'kind':kind,'session':self.m.get('sessionId'),'plugin':self.m.get('plugin'),
                             'preset':(self.m.get('preset') or {}).get('name'),'instruction':m.get('instruction'),
                             'move':(m.get('plan') or {}).get('move'),'amount':m.get('amount'),
                             'trajectory':m.get('trajectory'),'patchBefore':m.get('snapshot'),**extra})
    @property
    def source(self):
        return self._source

    @source.setter
    def source(self,value):
        """Where this session's journal entries come from: 'user' or 'test'.

        An unknown value is **refused** rather than quietly treated as the producer. That silent
        coercion - `'test' if body=='test' else 'user'` - is how automated runs got into what
        Scrunch had "learned" the first time: anything that was not exactly the magic word became
        the producer's own taste, including a typo and a forgotten flag.

        Changing this changes which journal is written (see `history`), so a run cannot be half in
        one and half in the other.
        """
        if value not in sound_prefs.SOURCES:
            raise ValueError('A journal source is one of %s, not %r.'%(', '.join(sound_prefs.SOURCES),value))
        self._source=value

    @property
    def history(self):
        """The journal this session appends to - a different file for automated runs.

        Belt and braces with the `source` label. Labelling alone failed once already, and the
        quarantined `history-scrunch-tests-*.jsonl` files show how it was patched up afterwards: by
        renaming the file by hand, leaving 71 entries with no label in them at all. Test runs now go
        to their own journal by construction, and `learn` only ever reads the producer's. A test that
        forgets to label itself still cannot reach what Scrunch learns, because it is not even in the
        file that gets read.
        """
        name='history.jsonl' if self._source==sound_prefs.LEARNS_FROM else 'history-%s.jsonl'%self._source
        return self.data_dir/'coach'/name

    @property
    def user_history(self):
        """The producer's own journal - the only one preferences are ever learned from.

        Named separately from `history` on purpose: what a session *writes* depends on whether it is
        a test, but what Scrunch *learns from* never does.
        """
        return self.data_dir/'coach'/'history.jsonl'

    def _write_journal(self,event):
        # Every entry says where it came from, and says it explicitly: `sound_prefs.teaches` is
        # fail-closed, so an unlabelled entry teaches nothing rather than counting as the producer.
        event['source']=self.source
        self.last_journal=event
        self.history.parent.mkdir(parents=True,exist_ok=True)
        with self.history.open('a') as f:f.write(json.dumps(event)+'\n')
        if event.get('kind') in ('move','wrong-direction','revert'):
            self.prefs=sound_prefs.learn(sound_prefs.read(self.user_history))   # learn as you go, never from a test run
    # ---- taking the sound with you ----
    def save_sound(self,name,pack=None,root=None,show_in_browser=True):
        """Save what the plug-in is making now as a real preset in its own library.

        The Set is saved first so the plug-in's state on disk is the sound in front of you; the
        preset is then written from that state, not from the file it started out as, which by now
        it may be a long way from. Every exposed knob is checked against the saved file afterwards,
        so what is reported as saved is what was read back.
        """
        import base64
        name=sound_export.safe_name(name)
        plugin=self.m.get('plugin')
        base_path=(self.m.get('preset') or {}).get('path')
        borrowed=False
        if (not base_path or not Path(base_path).is_file()) and plugin!='Diva':
            # An adopted track in the producer's own project never came from a Scrunch preset.
            base_path=str(sound_export.container(plugin));borrowed=True
        if not base_path or not Path(base_path).is_file():
            raise ValueError('Scrunch does not know which preset this sound started from, so it cannot save it as one.')
        knobs=self.knobs_view()
        # The sound playing now, straight from Scrunch Host; the Set is never saved for this.
        w=self._hosted()
        if w is None:raise ValueError(host_step(plugin,self.m.get('trackName')))
        import synth_state
        state=synth_state.juce_state.parse(base64.b64decode(w.state()['data']))[0]['IComponent']
        if plugin=='Diva':
            import uhe_presets
            text=uhe_presets.state_preset(state).decode('latin-1')
            path=sound_export.diva_path(name,root=root)
            written=sound_export.write(path,text)
            detail={'settingsCarried':len(uhe_presets.parse(text.encode('latin-1'))['parameters'])}
        else:
            data,detail=sound_export.arturia_from_state(Path(base_path).read_bytes(),state,name,pack)
            path=sound_export.arturia_path(plugin,name,root=root)
            import arturia_presets
            written=sound_export.write(path,data,None,None)
            stored=arturia_presets.parse(Path(path).read_bytes())['parameters']
            wrong=[k['setting'] for k in knobs
                   if k['setting'] in stored and abs(stored[k['setting']]-k['value'])>2e-4]
            if wrong:
                Path(path).unlink(missing_ok=True)
                raise ValueError('The saved preset does not match what the plug-in is playing (%s); '
                                 'nothing was left in your library.'%', '.join(sorted(wrong)[:4]))
            detail['knobsMatched']=len([k for k in knobs if k['setting'] in stored])
        browser=None
        if show_in_browser and plugin!='Diva':
            # Arturia's browser lists its database, not the folder, so the file alone is invisible.
            import arturia_db
            try:browser=arturia_db.register(path,base_path,name)
            except Exception as e:browser={'error':str(e)}
        self._write_journal({'kind':'save','at':self.clock(),'name':name,'plugin':plugin,
                             'from':(self.m.get('preset') or {}).get('name'),'path':str(path),
                             'inBrowser':bool(browser and not browser.get('error'))})
        listed=bool(browser and not browser.get('error'))
        # Arturia's browser keeps its own index in memory: a preset registered while the plug-in is
        # open shows up once its search box makes it look again. Verified on the JUP, 20 Sep 2026.
        say=('Saved as %r. It is in %s\u2019s preset browser under User \u2014 if it is not listed yet, '
             'type the name into the plug-in\u2019s search box and it will appear.'%(name,plugin) if listed
             else 'Saved as %r in %s\u2019s User folder.'%(name,plugin))
        if borrowed:
            say+=' Scrunch had no preset to start from here, so it built the file around %r \u2014 every value is '\
                 'this sound, but the kind of sound it is filed as came from that.'%Path(base_path).name
        return {**written,**detail,'plugin':plugin,'startedFrom':(self.m.get('preset') or {}).get('name'),
                'builtAround':Path(base_path).name if borrowed else None,'browser':browser,'say':say}
    def learned(self):
        """What the journal has taught Scrunch so far, in plain lines."""
        return {'lines':sound_prefs.summary(self.prefs),'evidence':self.prefs.get('evidence',{}),
                'amounts':self.prefs.get('amounts',{}),'phrases':self.prefs.get('phrases',{})}

    # ---- level ----
    def level(self,ticks=12):
        """How loud the track is right now. Only meaningful while it is playing."""
        self.device or self._device()
        r=self.a.meter(self.track,ticks)
        if not r['playing']:raise ValueError('Start the loop first: the meter only reads while the track plays.')
        return r
    def comparison_key(self):
        """What this loudness comparison depends on: both sounds, the move, and how it is being auditioned.

        Any difference here makes an earlier measurement wrong, so it is measured again.
        """
        if not self.move:raise ValueError('Ask for a change first.')
        knobs=self.knobs_view()
        moved=[(s['setting'],round(s['origin']+s['direction']*s['travel']*sound_moves.REACH*self.move['amount'],6))
               for s in self.move['plan']['steps']]
        track=None
        try:track=self.a.track(self.track)
        except Exception:pass
        return sound_levels.signature(
            plugin=self.m.get('plugin'),preset=(self.m.get('preset') or {}).get('name'),
            sound=sound_levels.sound_state(knobs,skip=[s['setting'] for s in self.move['plan']['steps']]),
            move=sorted(moved),amount=round(self.move['amount'],4),
            audition=sound_levels.audition_state(self.a.transport(),track))
    def compare_loudness(self,seconds=sound_audio.MIN_SECONDS,use_cache=True):
        """Loudness (LUFS) with and without the move, through the tap.

        A measurement is reused only while both sounds and the audition conditions are identical to
        when it was taken; anything else and it is measured again straight away. Nothing is measured
        on a timer, and no gain follows the sound while it plays.
        """
        if not self.move:raise ValueError('Ask for a change first.')
        key=self.comparison_key()
        if use_cache:
            kept=self.levels.get(key)
            if kept:return dict(kept,key=key[:8],cacheStats=self.levels.stats())
        amount=self.move['amount']
        self._apply(0.);before=self.hear(seconds)
        self._apply(amount);after=self.hear(seconds)
        if before['loudnessLUFS'] is None or after['loudnessLUFS'] is None:
            raise ValueError('Too quiet to measure loudness; play the loop with sound.')
        out={'beforeLUFS':round(before['loudnessLUFS'],2),'afterLUFS':round(after['loudnessLUFS'],2),
             'louderByDb':round(after['loudnessLUFS']-before['loudnessLUFS'],2),
             'basis':'EBU R128 through the Live Ears tap','cached':False,'key':key[:8]}
        self.levels.put(key,out)
        return dict(out,cacheStats=self.levels.stats())
    def compare_level(self,ticks=None):
        """Level with and without the move, each averaged over a whole loop so the two windows hear the same phrase."""
        if not self.move:raise ValueError('Ask for a change first.')
        ticks=ticks or self.loop_ticks()
        amount=self.move['amount']
        self._apply(0.);before=self.level(ticks)
        self._apply(amount);after=self.level(ticks)
        return {'beforeLevel':round(before['mean'],4),'afterLevel':round(after['mean'],4),'seconds':round(ticks/10.,1),
                'louderByDb':db(after['mean'],before['mean'])}
    def loop_ticks(self,beats=8):
        """Display ticks covering one pass of the coach loop at the Set's tempo (the meter samples once per tick)."""
        tempo=float(self.a.connect().get('tempo') or 120)
        return max(10,min(60,int(round(beats*60.0/tempo*10))))
    def volume_slope(self,knob,probe=.05):
        """dB per unit of the Volume knob, read off its own display around where it sits now."""
        here=parse_db(knob['display'])
        target=min(knob['max'],max(knob['min'],knob['value']+probe*(knob['max']-knob['min'])))
        if here is None or abs(target-knob['value'])<1e-9:return None
        moved=self.a.set_device_parameter(self.device,knob['parameter'],knob['name'],value=target,expect_value=knob['value'])
        there=parse_db(moved.get('display'))
        self.a.set_device_parameter(self.device,knob['parameter'],knob['name'],value=knob['value'],expect_value=moved['value'])
        if there is None:return None
        return (there-here)/(target-knob['value'])
    def match_level(self,tolerance=.5,tries=4,ticks=None,use_tap=True):
        """Make the move level-matched by trimming Volume as PART of the move, so only the moved version changes.

        Trimming the knob on its own lifts both versions equally and never closes the gap; the trim
        is added to the move's steps, so reverting also removes it.
        """
        def gap_now(use_cache=True):
            if use_tap:
                try:return self.compare_loudness(use_cache=use_cache)
                except ValueError:pass
                except Exception:pass
            return self.compare_level(ticks)
        trim_key='trim:'+self.comparison_key()             # the comparison as it stands, before any trim is added
        remembered=self.levels.get(trim_key)
        if remembered and remembered.get('trimDelta') is not None:
            # Same two sounds, same amount, same audition conditions as when this was measured:
            # put the known offset straight back on, with nothing re-measured.
            self._trim_step(remembered['trimDelta'])
            self._apply(self.move['amount'])
            return dict(remembered,reused=True)
        gap=gap_now()
        if gap['louderByDb'] is None:raise ValueError('Too quiet to measure: play the loop with sound before matching levels.')
        if abs(gap['louderByDb'])<=tolerance:
            return {'louderByDb':gap['louderByDb'],'basis':gap.get('basis'),'matched':True,'trim':None}
        knob=next((k for k in self.knobs_view() if k['setting']=='Amp_Level'),None)
        if knob is None:raise ValueError('This patch exposes no Volume knob to trim.')
        slope=self.volume_slope(knob)
        if not slope:raise ValueError('This Volume knob does not show dB, so the trim cannot be worked out.')
        ceiling=parse_db(knob['display'])
        if gap['louderByDb']<0 and ceiling is not None and ceiling>-.3:
            return {'louderByDb':gap['louderByDb'],'basis':gap.get('basis'),'matched':False,'trim':None,
                    'note':'The move is %.1f dB quieter and the synth Volume is already at its ceiling (%.2f dB); '
                           'there is no headroom to match it inside the instrument.'%(abs(gap['louderByDb']),ceiling)}
        total=0.
        for attempt in range(max(1,tries)):
            delta=compensation(gap['louderByDb'],slope,knob['max']-knob['min'])
            if abs(delta)<1e-5:break
            before_gap=gap['louderByDb'];total+=delta
            self._trim_step(total)
            self._apply(self.move['amount'])
            gap=gap_now(use_cache=False)       # the move just changed: the old measurement no longer applies
            if abs(gap['louderByDb'] or 0)<=tolerance:break
            achieved=(gap['louderByDb']-before_gap)/delta if delta else 0    # what the knob really gave us
            if achieved>1e-3:slope=achieved                                  # trust the measurement over the display
        left=gap['louderByDb']
        headroom=parse_db(next((k['display'] for k in self.knobs_view() if k['setting']=='Amp_Level'),None))
        out={'louderByDb':left,'basis':gap.get('basis'),'trim':knob['label'],'trimDelta':total,
             'matched':left is not None and abs(left)<=tolerance}
        if out['matched']:self.levels.put(trim_key,out)     # reuse it while these sounds and conditions hold
        if not out['matched'] and left is not None and left<0 and headroom is not None and headroom>-.3:
            out['note']=('The synth Volume is at its ceiling (%.2f dB), so %.1f dB of the difference cannot be '
                         'made up inside the instrument. Use the track fader if you want a strict match.'%(headroom,abs(left)))
        return out

    # ---- hearing the result ----
    def tap_stream(self):
        """The Live Ears stream of the tap on THIS track, matched by the identity it publishes.

        Never "the first stream": with taps on several tracks that would measure the wrong sound.
        """
        import live_ears_binding
        from live_ears import LiveEarsSource
        self.device or self._device()
        taps=[d for d in self.a.devices(self.track) if 'Live Ears Bound' in str(d.get('deviceName') or d.get('name') or '')]
        if not taps:raise ValueError('there is no Live Ears Bound on this track')
        ident=live_ears_binding.device_identity(self.a.device(taps[0]['id']))
        streams=LiveEarsSource.discover()
        if not ident:
            # Ableton is not showing the tap's identity controls. One tap in the whole Set and one
            # stream can only be each other; anything else cannot be told apart, so it is not measured.
            everywhere=[d for t in self.a.session()['tracks'] for d in self.a.devices(t['id'])
                        if 'Live Ears Bound' in str(d.get('deviceName') or d.get('name') or '')]
            if len(everywhere)==1 and len(streams)==1:return streams[0]
            raise ValueError('Ableton is not showing the Live Ears identity, and with %d taps and %d streams '
                             'Scrunch cannot tell which one is this track'%(len(everywhere),len(streams)))
        for stream in streams:
            try:
                if live_ears_binding.metadata(stream)['identity']==ident:return stream
            except (OSError,ValueError):continue
        raise ValueError('the Live Ears Bound on this track is not publishing audio')
    def hear(self,seconds=sound_audio.MIN_SECONDS):
        """Measure the track through its own Live Ears tap: loudness, brightness and band levels.

        A synth hosted in Scrunch Host is heard from inside the wrapper instead (no tap needed),
        through the same analysis, so the verdicts mean the same thing."""
        import wrapper_backend
        if isinstance(self.a,wrapper_backend.WrappedAdapter) and self.a.hosts(self.device or self._device()):
            return sound_audio.measure(*self.a.capture(self.device,max(sound_audio.MIN_SECONDS,float(seconds))))
        stream=self.tap_stream()
        return sound_audio.measure(*sound_audio.capture(seconds,discover=lambda:[stream]))
    def hear_later(self,seconds=sound_audio.MIN_SECONDS):
        """What hear() does, split so the capture can run without holding Live: finds this track's tap now
        and returns a function that captures and measures it. None when the synth is inside Scrunch Host,
        whose capture goes through the host connection."""
        import wrapper_backend
        if isinstance(self.a,wrapper_backend.WrappedAdapter) and self.a.hosts(self.device or self._device()):
            return None
        stream=self.tap_stream()
        return lambda:sound_audio.measure(*sound_audio.capture(seconds,discover=lambda:[stream]))
    def capture_raw(self,seconds):
        """(frames x channels float array, sample rate) of the sound as it is now, from the same place hear() listens."""
        import numpy as np,wrapper_backend
        if isinstance(self.a,wrapper_backend.WrappedAdapter) and self.a.hosts(self.device or self._device()):
            frames,rate,channels=self.a.capture(self.device,max(sound_audio.MIN_SECONDS,float(seconds)))
        else:
            stream=self.tap_stream()
            frames,rate,channels=sound_audio.capture(seconds,discover=lambda:[stream])
        y=np.asarray(frames,dtype=float)
        return (y.reshape(-1,channels) if channels>1 else y[:,None]),rate
    def _dry(self):
        """Temporarily take the effect slots out of the sound, so a note's own shape can be timed.

        A delay or reverb tail is louder than the note it follows; with the effects in, "pluckier"
        measures the effects, not the instrument. Returns the knobs it silenced so they go back.
        """
        silenced=[]
        for knob in self.knobs_view():
            if knob['setting'].startswith('FX') and knob['setting'].endswith('_Dry_Wet') and knob['value']>knob['min']:
                try:
                    self.a.set_device_parameter(self.device,knob['parameter'],knob['name'],value=knob['min'],expect_value=knob['value'])
                    silenced.append((knob,knob['value']))
                except Exception:pass
        return silenced
    def _restore(self,silenced):
        for knob,value in silenced:
            try:
                now=next(k for k in self.knobs_view() if k['parameter']==knob['parameter'])
                self.a.set_device_parameter(self.device,knob['parameter'],knob['name'],value=value,expect_value=now['value'])
            except Exception:pass
    def verify_move(self,seconds=sound_audio.MIN_SECONDS,correct=True):
        """Listen to the patch with and without the move and say whether the audio actually did it.

        One self-correction: if the audio went nowhere, the same move is pushed further once. If it
        went the wrong way, the move is reverted and reported rather than thrashed at.
        """
        if not self.move:raise ValueError('Ask for a change first.')
        amount=self.move['amount']
        measure=(self.move.get('expect') or ('',''))[0]
        timing=measure in ('decayMs','attackMs','sustainDb','holdRatio')
        silenced=self._dry() if timing else []
        try:
            self._apply(0.);before=self.hear(seconds)
            repeat=self.hear(seconds)                  # the same sound twice: how much the measurement wanders
            self._apply(amount);after=self.hear(seconds)
        finally:
            self._restore(silenced)
        expect=tuple(self.move['expect']) if self.move.get('expect') else None
        out=sound_audio.verdict(self.move['plan']['move'],before,after,repeat=repeat,expect=expect)
        out['amount']=amount
        if silenced:out['effectsMuted']=[k['label'] for k,_ in silenced]
        if out['result']=='no change' and correct and amount<1.:
            stronger=min(1.,amount+.35);self._apply(stronger)
            retry=sound_audio.verdict(self.move['plan']['move'],before,self.hear(seconds),repeat=repeat,expect=expect)
            retry.update(amount=stronger,selfCorrected=True)
            if silenced:retry['effectsMuted']=[k['label'] for k,_ in silenced]
            if retry['result']!='achieved':
                retry['note']='Pushed it further and the audio still barely moved; this patch may not have that in it.'
            self._journal('verify',audio=retry);return retry
        if out['result']=='wrong way':
            self._apply(0.);out['reverted']=True
            out['note']='The audio went the other way, so the move was put back. Tell me what you are hearing.'
        self._journal('verify',audio=out)
        return out

    # ---- decisions ----
    def keep(self,words=''):
        """Record the knob's current position as kept, with the user's words."""
        src=self.origin
        if not src or self.sweep:raise ValueError('Nothing to keep yet.' if not src else 'Stop the sweep first.')
        now=self._knob(src['parameter'])
        event={'at':self.clock(),'session':self.m.get('sessionId'),'plugin':self.m.get('plugin'),'setting':src['setting'],'knob':src['label'],
               'from':{'value':src['value'],'display':src['display']},'to':{'value':now['value'],'display':now['display']},
               'method':('ab-'+self.ab.get('playing','?')) if self.ab else 'sweep','words':str(words)[:500]}
        self._write_journal(event)
        self.ab=None;self.last=None;self.origin=None
        return event
    def undo(self):
        """Put the knob being worked on back where it was before this move."""
        src=self.origin
        if not src:raise ValueError('Nothing to undo.')
        if self.sweep:self.stop()
        cur=self._knob(src['parameter'])['value']
        r=self.a.set_device_parameter(self.device,src['parameter'],src['name'],value=src['value'],expect_value=cur)
        self.ab=None;self.last=None;self.origin=None
        return {'label':src['label'],'now':r.get('display')}
