import re
import system_health
import capture_adapter
import live_ears_dsp
import session_plans,observation,transaction_store,transaction_recovery,session_architecture,sidechain_plan,daw_adapter
from live_ears import LiveEarsSource,start_housekeeping,HOUSEKEEPING
"""Local companion service. Run: python3 server.py; bind loopback only."""
import json, secrets, mimetypes, os, atexit, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse,unquote
import engine
import engineer
import checkpoints
from text_safety import sanitize
import instruments
import ingest
import ambient_analysis
import reference_ab
import preset_index
import production_feedback
import arrangement_profile,blind_experiment
from voice import transcribe, speak
import voice_settings

import sound_coach,plugin_knobs,fresh_session,coach_sessions,sound_language
COACH={}

def coach(prepare=False):
    """Coach the instrument selected in the current Live Set."""
    import current_sound
    import preset_navigation
    # Fast path: keep the synth already being worked on unless John selected another instrument
    # track in Live. Rediscovery scans every track (17 s on an 80-track Set).
    held=COACH.get('coach')
    if held is not None and COACH.get('presetRevision') != preset_navigation.NAVIGATOR.patches.get(held.device,{}).get('revision'):
        COACH.clear();held=None
    if held is not None:
        try:
            import wrapper_backend
            host=wrapper_backend.wrap(engine.host());sel=host.selected_track();sel=sel.get('id') if isinstance(sel,dict) else sel
            if current_sound.PINNED:
                if held.track==current_sound.PINNED and any(d.get('id')==held.device for d in host.devices(held.track)):return held
                raise LookupError('Match Reference target is another track')
            named=current_sound._named([(t,None) for t in host.session()['tracks']],current_sound.TEXT_HINT)
            if named and all(t['id']!=held.track for t,_ in named):raise LookupError('John named another track')
            # Steering words always mean the synth already being worked on.
            if not named and (sel in (None,held.track) or not any(d.get('role')=='instrument' for d in host.devices(sel))):
                if any(d.get('id')==held.device for d in host.devices(held.track)):return held
        except Exception:pass
    try:path=fresh_session.current_path()
    except ValueError:path=None
    import wrapper_backend
    host=wrapper_backend.wrap(engine.host())   # a synth inside Scrunch Host shows all its controls
    # Live state only: a synth whose controls Live does not expose is reported with its one hosting step.
    manifest,vendor=current_sound.discover(host,path)
    patch=preset_navigation.NAVIGATOR.patches.get(manifest['liveTarget'],{})
    if patch:
        manifest['preset']={'name':patch['name']}
        manifest.pop('fxSlots',None)
    identity=(str(path),manifest['liveTarget'],tuple((k['id'],k['name']) for k in manifest['exposedKnobs']),patch.get('revision'))
    if COACH.get('identity')!=identity:
        import re as _re;knowledge_file=engine.DATA/'knowledge'/'arturia'/(_re.sub(r'[^a-z0-9]+','-',manifest['plugin'].lower()).strip('-')+'.json')
        knowledge=json.loads(knowledge_file.read_text()) if knowledge_file.is_file() else None
        def interpreter(words,ctx):
            profile=sound_language.selected(engine.STATE)
            if not profile:raise ValueError('No model is set up for the engineer; choose one in Settings.')
            return sound_language.interpret(profile,words,ctx)
        COACH.clear();COACH.update(path=str(path or 'Untitled'),identity=identity,presetRevision=patch.get('revision'),coach=sound_coach.Coach(host,manifest,vendor,engine.DATA,
                                                                          knowledge=knowledge,interpreter=interpreter))
        COACH['coach'].device=manifest['liveTarget'];COACH['coach'].track=manifest['liveTrack']
    return COACH['coach']

def current_sound_refusal(what):
    return ('%s needs the Set saved and reopened, which the live engineer never does. Synths in Scrunch Host '
            'are changed directly instead.'%what[:1].upper()+what[1:])

def coach_action(name,b):
    import preset_navigation
    navigator=preset_navigation.NAVIGATOR
    if navigator.state['scanning'] and name not in ('knobs','instances','learned','presets'):
        kept=navigator.stop()
        if name in ('keep','keep-move','stop','halt'):
            engine.stop()
            return {**kept,'playing':False,'say':'Kept the current preset.',
                    'to':{'display':kept.get('preset','Current preset')}}
    if name=='effect':
        raise ValueError(current_sound_refusal('changing an effect slot'))
    if name=='switch':
        return coach(prepare=True).load_preset(str(b['path']))
    if name in ('adopt-plan','adopt'):
        raise ValueError(current_sound_refusal('rewriting the Set file to expose controls'))
    if name=='adopt-scan':
        # Read-only list of synths in the open Set (live state only).
        here=fresh_session.current_path()
        if name=='adopt-scan':
            tracks=[]
            for tr in engine.host().session()['tracks']:
                for d in engine.host().devices(tr['id']):
                    if d['name'] in coach_sessions.SUPPORTED:
                        tracks.append({'track':tr['id'],'trackName':tr.get('name'),'plugin':d['name'],'device':d['id']})
            return {'set':str(here),'name':here.stem,'candidates':tracks,
                    'alreadyPrepared':bool((fresh_session.session_manifest(here) or {}).get('exposedKnobs'))}
    c=coach(prepare=name not in ('knobs','instances','learned','presets'))
    # A request may declare itself an automated run; it can never declare a test session back into
    # the producer's own history (Coach.source refuses the downgrade). Absent means the producer.
    if b.get('source') is not None or c.source is None:
        c.source=b.get('source','user')
    if name=='presets':return {'presets':coach_sessions.presets(str(b.get('q','')),c.m.get('plugin'))}
    if name=='knobs':
        knobs=c.knobs_view();return {'session':Path_name(COACH['path']),'plugin':c.m.get('plugin'),'preset':(c.m.get('preset') or {}).get('name'),'family':c.family,'knobs':knobs,
                                     'instances':c.instances()}
    actions={'start':lambda:c.start(str(b['parameter']),str(b.get('direction','up')),float(b.get('seconds',6))),
             'stop':c.stop,'back':lambda:c.back(float(b.get('fraction',.04))),'other':lambda:c.other_way(float(b.get('seconds',6))),
             'ab':lambda:c.ab_set(str(b['parameter']),float(b['a']),float(b['b'])),'side':lambda:c.ab_play(str(b['side'])),
             'keep':lambda:c.keep(str(b.get('words',''))),'undo':c.undo,'play':c.play,'halt':c.halt,
             'instruct':lambda:c.instruct(str(b.get('words','')),float(b['amount']) if b.get('amount') is not None else None),
             'steer':lambda:c.steer(str(b.get('direction','more'))),
             'keep-move':lambda:c.keep_move(str(b.get('words',''))),
             'level':lambda:c.level(),'compare-level':lambda:c.compare_level(),'match-level':lambda:c.match_level(),
             'hear':lambda:c.hear(),'verify':lambda:c.verify_move(),'compare-loudness':lambda:c.compare_loudness(),
             'learned':lambda:c.learned(),
             'forget-levels':lambda:c.forget_levels(str(b.get('why','asked'))),
             'save-sound':lambda:c.save_sound(str(b.get('name',''))),
             'instances':lambda:{'plugin':c.m.get('plugin'),'instances':c.instances()},
             'use':lambda:c.use(str(b['device']))}
    if name not in actions:raise ValueError('Unknown coach action.')
    return actions[name]()

def pattern_write(b):
    """Write a plain drum pattern into the arrangement: no model, no original needed."""
    import drum_patterns
    track=b.get('track')
    if isinstance(track,str) and not track.isdigit():
        names=[t.get('name') for t in engine.host().session()['tracks']]
        if track not in names:raise ValueError('This Set has no track called %r.'%track)
        index=names.index(track)
    else:
        index=int(track if track is not None else 0)
    return drum_patterns.write(engine.host(),index,str(b.get('drum','kick')),str(b.get('pattern','four on the floor')),
                               bars=int(b.get('bars',1)),beats_per_bar=int(b.get('beatsPerBar',4)),
                               velocity=int(b.get('velocity',100)),length=float(b.get('noteLength',.25)),
                               swing=float(b.get('swing',0)),start=float(b.get('start',0)),
                               name=b.get('name') or None)

SCOPE={}

def scope():
    import scope_stream
    if 'scope' not in SCOPE:SCOPE['scope']=scope_stream.Scope()
    return SCOPE['scope']

def scope_action(name,b):
    """The multi-track scope: lanes of live audio from every Live Ears tap in the session."""
    import scope_dsp
    s=scope()
    if name=='start':
        out=s.start()
        try:out['bound']=s.bind()
        except Exception as e:out['bindError']=str(e)
        return {**out,'lanes':s.lane_list()}
    if name=='stop':return s.stop()
    if name=='name':return s.name_lane(str(b['stream']),str(b.get('label','')),b.get('color'))
    if name=='taps':return {'taps':s.taps_in_set()}
    if name=='measure-lanes':
        import scope_lanes
        return scope_lanes.measure(float(b.get('seconds',2)))
    if name=='sync-lanes':
        # Tell the native Scope plug-in which track each of its lanes is, and in what order.
        import scope_lanes
        return scope_lanes.sync(engine.host().session()['tracks'])
    if name=='identify':return s.identify(accept_hung_note_risk=bool(b.get('acceptHungNoteRisk')))
    if name=='lanes':
        s.attach()
        try:s.bind()
        except Exception:pass
        return {'running':s.running(),'lanes':s.lane_list()}
    if name=='frame':
        seconds=max(.1,min(10.,float(b.get('seconds',4))))
        columns=int(max(16,min(2000,int(b.get('columns',600)))))
        streams=b.get('streams') or None
        window=s.window(seconds,streams)
        analyse=bool(b.get('analyse'))
        lanes=[]
        for index,(lane,audio) in enumerate(window['lanes']):
            lo,hi,rms=scope_dsp.envelope(audio,columns)
            row={**lane.view(index=index),'min':[round(float(v),4) for v in lo],'max':[round(float(v),4) for v in hi],
                 **scope_dsp.level(audio)}
            if analyse:
                row['bands']=scope_dsp.spectrum(audio,lane.rate)
                row['mono']=scope_dsp.mono_compatibility(audio,lane.rate)
            lanes.append(row)
        return {'running':s.running(),'seconds':window['seconds'],'aligned':window['aligned'],
                'columns':columns,'lanes':lanes,'at':time.time()}
    if name=='relationship':
        seconds=max(.5,min(10.,float(b.get('seconds',4))))
        first,second=str(b.get('a','')),str(b.get('b',''))
        window=s.window(seconds,[first,second])
        found={lane.stream:(lane,audio) for lane,audio in window['lanes']}
        if first not in found or second not in found:raise ValueError('Pick two taps that are both running.')
        (lane_a,audio_a),(lane_b,audio_b)=found[first],found[second]
        out=scope_dsp.relationship(audio_a,audio_b,lane_a.rate,names=(lane_a.name,lane_b.name))
        return {**out,'aligned':window['aligned'],'seconds':seconds}
    raise ValueError('Unknown scope action.')

def Path_name(path):
    from pathlib import Path
    return Path(path).stem

TOKEN=secrets.token_urlsafe(32)
PORT=int(os.environ.get('SCRUNCH_PORT','3351'))

# Shown only for a genuine unhandled crash - not for the guided ValueError
# messages elsewhere, which already carry their own specific next step.
# Toggle via STATE['crashPersona'] = False for the plain form.
CRASH_PERSONA="We've got a serious fucking problem and I need to speak to the boss. "\
    "The head geek fuckers are going to sort it out. It's beer o'clock."
CRASH_PERSONA_PLAIN='An unexpected error occurred. This has been logged and flagged for review.'
ORIGIN=f'http://127.0.0.1:{PORT}'

def saved_preview(body):
    plan=session_plans.preview(planning_observation(body),body.get('actions'))
    transaction_store.Store(engine.DATA/'transactions').create(plan)
    return plan

def feedback_preparation(body):
    report=production_feedback.prepare(planning_observation(body),body)
    production_feedback.save(engine.DATA/'production-feedback',report)
    return {**report,'summary':production_feedback.english(report)}

def recovery_preview(body):
    store=transaction_store.Store(engine.DATA/'transactions')
    with store.execution_locked(),store.locked(body.get('planId')):
        return transaction_recovery.reconcile(store.inspect(body['planId']),observation.observe())

def architecture_proposal(body):
    proposal=session_architecture.propose(planning_observation(body),body.get('groups',[]),body.get('returns',[]),body.get('sends',[]))
    store=transaction_store.Store(engine.DATA/'architecture-proposals')
    store.write(store.root/(proposal['fingerprint']+'.json'),proposal)
    return proposal

def planning_observation(body):
    current=observation.observe()
    if 'observationFingerprint' in body:
        session_plans.require_fresh(body,current)
    return current

def session_state():
    current=observation.observe()
    if current.get('available'):current['fingerprint']=session_plans.fingerprint(current)
    return current

def mixing_workspace_state(state):
    """Bounded UI snapshot; unrelated MIDI, synth and media payloads stay out."""
    rows=[]
    for m in reversed(state.get('engineerChat',[])):
        if m.get('workspace')!='mix':continue
        message={k:m[k] for k in ('role','workspace','failed','model') if k in m}
        words=m.get('text','')
        message['text']=words[:8000] if isinstance(words,str) else ''
        if isinstance(words,str) and len(words)>8000:message['previewTruncated']=True
        if isinstance(m.get('timings'),dict):
            message['timings']={k:m['timings'][k] for k in ('modelCalls','interpretationMs') if k in m['timings']}
        rows.append(message)
        if len(rows)==8:break
    voice=state.get('voiceSettings') or {}
    return {'token':TOKEN,'engineerProvider':state.get('engineerProvider'),
            'providers':[{k:p[k] for k in ('id','name','model','kind') if k in p} for p in state.get('providers',[])],
            'voiceEngine':state.get('voiceEngine','on-device'),
            'voiceSettings':{k:voice[k] for k in ('service','voice','hasKey') if k in voice},
            'engineerChat':list(reversed(rows))}

def sidechain_preview(body):
    proposal=sidechain_plan.read_preview(body,observation.observe,
        daw_adapter.DEFAULT.read_device)
    store=transaction_store.Store(engine.DATA/'sidechain-proposals')
    store.write(store.root/(proposal['fingerprint']+'.json'),proposal)
    return proposal

def preset_search(body):
    query=body.get('query')
    product=body.get('product')
    if not isinstance(query,str) or not query.strip() or len(query)>500:raise ValueError('Enter a preset description of 500 characters or fewer.')
    if product is not None and (not isinstance(product,str) or not product.strip() or len(product)>100):raise ValueError('Preset product filter is invalid.')
    return preset_index.search(query.strip(),body.get('limit',12),product.strip() if product else None)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,body,content='application/json'):
        raw=json.dumps(body).encode() if content=='application/json' else body
        self.send_response(status); self.send_header('Content-Type',content)
        self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers(); self.wfile.write(raw)
    def allowed(self):
        return self.headers.get('Host')==f'127.0.0.1:{PORT}'
    def do_GET(self):
        if not self.allowed(): return self.send(403,{'error':'Open the dashboard using 127.0.0.1.'})
        path=urlparse(self.path).path
        if path=='/api/preset-variations/state':
            import preset_variation_workspace
            try:return self.send(200,preset_variation_workspace.WORKSPACE.snapshot())
            except preset_variation_workspace.Busy as e:return self.send(503,{'error':str(e),'retryAutomatically':False})
            except (ValueError,OSError,RuntimeError) as e:return self.send(400,{'error':str(e),'retryAutomatically':False})
        if path=='/api/profiles':
            try:return self.send(200,arrangement_profile.inventory())
            except (ValueError,OSError):return self.send(400,{'error':'Current project profiles unavailable'})
        if path.startswith('/api/profile/'):
            try:
                parts=path.split('/')
                if len(parts)==6 and parts[4]=='cue':return self.send(200,arrangement_profile.cue(parts[3],parts[5]),'audio/wav')
                if len(parts)==4:return self.send(200,arrangement_profile.context(arrangement_profile.load(parts[3])))
                return self.send(404,{'error':'Unknown profile route'})
            except (ValueError,OSError):return self.send(400,{'error':'Profile or cue unavailable; no fallback used.'})
        if path=='/api/preset-scan':
            import preset_navigation
            with preset_navigation.NAVIGATOR.lock:
                return self.send(200,dict(preset_navigation.NAVIGATOR.state))
        if path=='/api/health':return self.send(200,system_health.MONITOR.snapshot())
        if path=='/api/session/state':
            with engine.LOCK:return self.send(200,session_state())
        if path=='/api/session/transactions':
            return self.send(200,transaction_store.Store(engine.DATA/'transactions').inventory())
        if path.startswith('/api/session/preview/'):
            try:return self.send(200,transaction_store.Store(engine.DATA/'transactions').inspect(path.rsplit('/',1)[-1]))
            except (ValueError,OSError):return self.send(404,{'error':'Saved preview unavailable; no action was run.'})
        if path=='/api/live-ears':return self.send(200,{**live_ears_dsp.SERVICE.status(),'streams':LiveEarsSource.discover(),'cleanup':dict(HOUSEKEEPING)})
        if path=='/api/export-recovery':return self.send(200,capture_adapter.recovery_status())
        if path=='/api/reference/state':return self.send(200,reference_ab.state())
        if path.startswith('/api/reference/report'):
            try:return self.send(200,reference_ab.report(path.rsplit('/',1)[-1] if path!='/api/reference/report' else None))
            except ValueError as e:return self.send(400,{'error':str(e)})
        if path.startswith('/api/reference/audio/'):
            try:
                ident,side=path.rsplit('/',2)[-2:];return self.send(200,reference_ab.audio(ident,side),'audio/wav')
            except ValueError as e:return self.send(400,{'error':str(e)})
        if path=='/api/analysis/session':
            r=ambient_analysis.snapshot();r['summary']=ambient_analysis.english(r);return self.send(200,r)
        if path=='/api/analysis/jobs':return self.send(200,{'jobs':ingest.jobs()})
        if path.startswith('/api/analysis/sidecar/'):
            try:return self.send(200,ingest.read(path.rsplit('/',1)[1]))
            except (ValueError,OSError):return self.send(404,{'error':'Sidecar not found.'})
        if path in ('/api/coach/knobs','/api/coach/presets'):
            try:
                from urllib.parse import parse_qs
                q=parse_qs(urlparse(self.path).query).get('q',[''])[0][:200]
                with engine.LOCK:return self.send(200,coach_action(path.rsplit('/',1)[1],{'q':q}))
            except ValueError as e:return self.send(400,{'error':sanitize(str(e)[:600])})
        if path=='/api/instruments':
            with engine.LOCK:return self.send(200,instruments.scan())
        if path=='/api/search/status':
            # A running search's visible status (stage, elapsed, heartbeat): never waits for the engine lock it holds.
            import brooklyn_rose
            return self.send(200,brooklyn_rose.status())
        if path=='/api/hud':
            # What the panel over Live shows: the synth being worked on and the engineer's last sound exchange.
            # While a search holds the engine lock (it can run minutes) the panel still gets the search status.
            import brooklyn_rose
            search=brooklyn_rose.status()
            mixp=__import__('mix_engineer').progress_view()
            if not engine.LOCK.acquire(timeout=.4):return self.send(200,{'busy':True,'search':search,'mixProgress':mixp})
            engine.LOCK.release()
            with engine.LOCK:
                held=COACH.get('coach');log=engine.STATE.get('engineerChat',[])
                last=next((m for m in reversed(log) if m.get('role')=='assistant' and m.get('workspace')=='sound'),None)
                said=next((m.get('text') for m in reversed(log) if last and m.get('role')=='user' and m.get('requestId')==last.get('requestId')),None)
                target=None
                if held is not None:target={'plugin':held.m.get('plugin'),'track':held.m.get('trackName'),'preset':(held.m.get('preset') or {}).get('name')}
                change=(last or {}).get('soundChange') or {}
                rows=[{'label':k.get('label'),'was':k.get('was'),'now':k.get('now')} for k in (change.get('knobs') or [])][:6]
                # The last Mixing exchange (what he typed or said, and the mix engineer's answer), for Mixing mode.
                mixed=next((m for m in reversed(log) if m.get('role')=='assistant' and m.get('workspace')=='mix'),None)
                mix={'words':next((m.get('text') for m in reversed(log) if m.get('role')=='user' and m.get('requestId')==mixed.get('requestId')),None),
                     'reply':mixed.get('text'),'failed':bool(mixed.get('failed'))} if mixed else None
                import decisions,ab_takes
                import audition_pair
                ab=(ab_takes.panel(held) or audition_pair.panel(held)) if held is not None else None
                return self.send(200,{'ab':ab,'target':target,'words':said,'reply':(last or {}).get('text'),'failed':(last or {}).get('failed'),
                                      'changes':rows,'verified':(change.get('verifiedResult') or {}).get('status'),
                                      'reference':(change.get('reference') or {}).get('result'),'decisions':decisions.summary(),
                                      'matchReference':__import__('match_reference').view(engine.STATE),'search':search,
                                      'mixOffer':__import__('mix_engineer').offer(),'mix':mix,'mixProgress':mixp})
        if path=='/api/mix-engineer/capabilities':
            import arrangement_automation,mix_engineer,mix_capture_evidence
            return self.send(200,{'automation':arrangement_automation.recording_limits(),
                                  'measurementVersion':mix_engineer.MEASUREMENT_VERSION,
                                  'waveformVerificationVersion':mix_engineer.WAVEFORM_VERIFICATION_VERSION,
                                  'captureCoverageVersion':mix_capture_evidence.VERSION})
        if path=='/api/mix-engineer/evidence':
            import mix_evidence
            return self.send(200,mix_evidence.history(engine.DATA/'mix-engineer'))
        if path=='/api/mix-engineer/offer':
            import mix_engineer
            return self.send(200,{'offer':mix_engineer.offer()})
        if path=='/api/mix-audit/latest':
            import mix_audit
            root=engine.DATA/'mix-audits'
            reports=mix_audit.history(root)['reports']
            return self.send(200,{'audit':mix_audit.load(reports[0]['id'],root) if reports else None})
        if path=='/api/mix-audit/history':
            import mix_audit
            return self.send(200,mix_audit.history(engine.DATA/'mix-audits'))
        if path=='/api/mix-engineer/feedback':
            return self.send(200,production_feedback.saved_catalog(engine.DATA/'production-feedback'))
        if path=='/api/match-reference':
            import match_reference
            with engine.LOCK:return self.send(200,match_reference.view(engine.STATE))
        if path=='/api/usage':
            import memory_page
            with engine.LOCK:return self.send(200,memory_page.usage())
        if path=='/api/memory':
            import memory_page
            try:return self.send(200,memory_page.view())
            except Exception as e:return self.send(200,{'error':str(e)[:300]})
        if path.startswith('/api/dream/'):   # Dream inbox (phone humming takes); dream.py owns the files
            import dream
            try:
                parts=path.split('/')
                if path=='/api/dream/takes':return self.send(200,{'takes':dream.takes(('inbox','placed'))})
                if len(parts)==5 and parts[3]=='take':return self.send(200,dream.load(parts[4]))
                if len(parts)==5 and parts[3]=='audio':return self.send(200,(dream.INBOX/dream.load(parts[4])['id']/'take.wav').read_bytes(),'audio/wav')
                return self.send(404,{'error':'Not found'})
            except (ValueError,OSError) as e:return self.send(400,{'error':str(e)[:300]})
        if path=='/api/checkpoints':return self.send(200,{'checkpoints':checkpoints.inventory()})
        if path=='/api/state':
            with engine.LOCK: return self.send(200,{'token':TOKEN,**engine.STATE})
        if path=='/api/mix-audit/workspace':
            if not engine.LOCK.acquire(timeout=.4):
                return self.send(503,{'error':'Scrunch is busy with another operation. Reopen Mixing when it finishes; no request was sent to your engineer.'})
            try:state=mixing_workspace_state(engine.STATE)
            finally:engine.LOCK.release()
            return self.send(200,state)
        if path.startswith('/midi/'):
            try:
                a=engine.find_attempt(path.split('/')[-1]); data=engine.midi_bytes(a['notes'],a['source']['length'],a['source']['tempo'])
                return self.send(200,data,'audio/midi')
            except ValueError as e: return self.send(404,{'error':str(e)})
        files={'/mix-offer.js':'mix-offer.js','/viewport.css':'viewport.css','/viewport.js':'viewport.js','/header-key.js':'header-key.js','/coach':'coach.html','/memory':'memory.html','/memory.js':'memory.js','/coach.css':'coach.css','/coach.js':'coach.js','/scope':'scope.html','/scope.css':'scope.css','/scope.js':'scope.js','/preproof.js':'preproof.js','/desktop.js':'desktop.js','/desktop.css':'desktop.css','/thinking-head.png':'thinking-head.png','/':'index.html','/app.js':'app.js','/session-planning.js':'session-planning.js','/style.css':'style.css','/voice-commands.js':'voice-commands.js'}
        files.update({'/dream':'dream-inbox.html','/dream-inbox.js':'dream-inbox.js'})
        files.update({'/mix-audit':'mix-audit.html','/mix-audit.js':'mix-audit.js','/mix-audit.css':'mix-audit.css','/mix-listening.js':'mix-listening.js'})
        files.update({'/preset-variations':'preset-variations.html','/preset-variations.js':'preset-variations.js','/preset-variations.css':'preset-variations.css'})
        if path not in files: return self.send(404,{'error':'Not found'})
        f=engine.ROOT/'dist'/files[path]
        return self.send(200,f.read_bytes(),mimetypes.guess_type(f)[0] or 'text/plain')
    def do_POST(self):
        if not self.allowed() or self.headers.get('Origin',ORIGIN)!=ORIGIN or not secrets.compare_digest(self.headers.get('X-Scrunch-Token',''),TOKEN):
            return self.send(403,{'error':'Reload the local dashboard to authenticate.'})
        if self.path=='/api/mix-audit/upload':
            try:
                import mix_audit
                self.connection.settimeout(30)
                with mix_audit.analysis_slot():
                    report=mix_audit.analyse(self.rfile,int(self.headers.get('Content-Length','0')),
                        label=unquote(self.headers.get('X-File-Name','')),
                        stage=self.headers.get('X-Mix-Stage','unspecified'))
                    mix_audit.save(report,engine.DATA/'mix-audits')
                return self.send(200,report)
            except mix_audit.AnalysisBusy as e:
                # Finish receiving/discarding encoded chunks before replying.
                # Closing mid-upload produces a broken pipe instead of the
                # useful busy response. Never decode or retain this audio.
                remaining=int(self.headers.get('Content-Length','0'))
                if 0<remaining<=mix_audit.MAX_BYTES:
                    while remaining:
                        block=self.rfile.read(min(remaining,1024*1024))
                        if not block:break
                        remaining-=len(block)
                return self.send(503,{'error':str(e),'retryAutomatically':False})
            except (ValueError,OSError,RuntimeError) as e:return self.send(400,{'error':str(e)})
        if self.path=='/api/profile/upload':
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=1_500_000_000:raise ValueError('Master file exceeds 1.5 GB limit.')
                self.connection.settimeout(30)
                return self.send(200,arrangement_profile.upload(self.rfile,size,unquote(self.headers.get('X-File-Name',''))))
            except (ValueError,OSError) as e:return self.send(400,{'error':str(e)})
        if self.path=='/api/reference/upload':
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=250_000_000:raise ValueError('Reference must be between 1 byte and 250 MB.')
                name=unquote(self.headers.get('X-File-Name',''))
                return self.send(200,reference_ab.upload(self.rfile,size,name))
            except (ValueError,OSError) as e:return self.send(400,{'error':str(e)})
        if self.headers.get('Content-Type')!='application/json': return self.send(415,{'error':'JSON required'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            limit=12_000_000 if self.path=='/api/preset-variations/select-upload' else 8_100_000 if self.path=='/api/transcribe' else 1_000_000 if self.path.startswith('/api/preset-variations/') else 300000
            if not 0<size<=limit: raise ValueError('Request too large or empty.')
            b=json.loads(self.rfile.read(size))
            if not isinstance(b,dict): raise ValueError('JSON object required.')
            if self.path.startswith('/api/preset-variations/'):
                import preset_variation_workspace
                try:
                    result=preset_variation_workspace.WORKSPACE.action(self.path.rsplit('/',1)[-1],b)
                    if isinstance(result,tuple):return self.send(200,result[0],result[1])
                    return self.send(200,result)
                except preset_variation_workspace.Busy as e:return self.send(503,{'error':str(e),'retryAutomatically':False})
                except (ValueError,OSError,RuntimeError) as e:return self.send(400,{'error':str(e)[:600],'retryAutomatically':False})
            if self.path=='/api/mix-engineer/trace':
                import mix_evidence
                return self.send(200,mix_evidence.trace(engine.DATA/'mix-engineer',engine.DATA/'mix-model-calls',b.get('runId')))
            if self.path=='/api/mix-audit/compare':
                import mix_audit
                root=engine.DATA/'mix-audits'
                with engine.LOCK:
                    comparison=mix_audit.compare(mix_audit.load(b.get('beforeId'),root),mix_audit.load(b.get('afterId'),root))
                    mix_audit.remember_comparison(comparison,root)
                return self.send(200,comparison)
            if self.path=='/api/mix-audit/select':
                import mix_audit
                return self.send(200,mix_audit.select(b.get('auditId'),engine.DATA/'mix-audits'))
            if self.path=='/api/mix-audit/clear':
                import mix_audit
                try:
                    with engine.LOCK,mix_audit.analysis_slot():
                        return self.send(200,mix_audit.clear_memory(engine.DATA/'mix-audits'))
                except mix_audit.AnalysisBusy as e:
                    return self.send(503,{'error':str(e),'retryAutomatically':False})
            if self.path=='/api/mix-audit/capture':
                import mix_audit
                # Keep the ordinary Stop endpoint available during real-time capture.
                with engine.LOCK:
                    report=mix_audit.capture_live(engine.host(),b.get('startBar'),b.get('endBar'))
                    mix_audit.save(report,engine.DATA/'mix-audits')
                return self.send(200,report)
            if self.path=='/api/mix-audit/section':
                import mix_audit
                with engine.LOCK:
                    root=engine.DATA/'mix-audits'
                    result=mix_audit.section(mix_audit.load(b.get('auditId'),root),b.get('startSeconds'),b.get('endSeconds'))
                    mix_audit.remember_section(result,root)
                return self.send(200,result)
            if self.path=='/api/transcribe':
                with engine.LOCK:
                    profile=next((dict(p) for p in engine.STATE['providers'] if p['id']==b.get('provider') and p['kind']=='gemini'),None)
                    engine_choice=engine.STATE.get('voiceEngine','on-device')
                voice_started=time.perf_counter()
                # His choice of transcriber: OpenAI (Settings → Voice key), on-device, or his Gemini profile.
                if engine_choice=='openai':
                    from voice import transcribe_openai
                    result=transcribe_openai(voice_settings.config(),b)
                elif engine_choice=='on-device':
                    try:result=transcribe(None,b)
                    except ValueError as local_error:
                        if not profile:raise
                        result=dict(transcribe(profile,b),localError=str(local_error)[:200])
                else:
                    if not profile: raise ValueError('Select a saved Gemini profile for voice.')
                    result=transcribe(profile,b)
                result["transcriptionMs"]=round((time.perf_counter()-voice_started)*1000)
            elif self.path=='/api/voice-settings':
                with engine.LOCK:
                    result=voice_settings.set_transcriber(b['transcriber']) if set(b)=={'transcriber'} else voice_settings.save(b)
            elif self.path=='/api/speak':
                with engine.LOCK:
                    profile=voice_settings.config()
                result=speak(profile,b.get('text',''),b.get('speed',1.0))
            elif self.path=='/api/engineer':
                with engine.LOCK:
                    result=engineer.chat(b,coach_factory=lambda:coach(prepare=True)) if b.get('workspace') in ('sound','auto') else engineer.chat(b)
            elif self.path=='/api/mix-engineer/run':
                # A mastering engineer's note (saved through /api/session/feedback) through Scrunch's engineer model.
                import mix_engineer
                with engine.LOCK:
                  try:result=mix_engineer.run(b)
                  except Exception:mix_engineer.progress('',done='Stopped: the reason is in the reply');raise
                mix_engineer.progress('',done='Done: candidate ready. Compare Original and Candidate, then Keep or Revert' if result.get('status')=='offered'
                  else 'Stopped: the reason is in the reply' if result.get('status')=='failed' else 'Done: answered, no change made')
            elif self.path=='/api/mix-engineer/choose' or (self.path=='/api/ab' and str(b.get('offerId','')).startswith('mix:')):
                # Original / Candidate / Keep / Revert / Neither on the waiting mix candidate: host writes and read-back only.
                import mix_engineer
                with engine.LOCK:result=mix_engineer.choose(str(b.get('choice') or b.get('action') or '').lower(),b.get('offerId'))
            elif self.path=='/api/ab':
                # The A/B buttons on the panel over Live: the same engineer path (and requestId dedupe) as his voice.
                action=str(b.get('action',''));rid=str(b.get('requestId',''))
                words={'original':'original','A':'A','B':'B','neither':'neither','keep':'keep','candidate':'candidate'}.get(action)
                if words is None:raise ValueError('Unknown A/B button.')
                import ab_takes
                held=COACH.get('coach')
                with engine.LOCK:
                  if action in ('original','candidate') and not (held is not None and ab_takes.pending(held)):
                    # No A/B offer waiting: the stored before/after (host writes + readback only, no model call).
                    result=engineer.chat({'text':words,'requestId':rid,'workspace':'sound','button':action,'source':'user'},
                                         coach_factory=lambda:coach(prepare=True))
                  else:
                    result=engineer.chat({'text':words,'requestId':rid,'workspace':'sound','abPanel':action,'source':'user'},
                                         coach_factory=lambda:coach(prepare=True))
            elif self.path=='/api/engineer/listen-ahead':
                # Only the synth already being worked on: never discover, switch or prepare before his words exist.
                with engine.LOCK:
                    import sound_chat,preset_navigation
                    held=COACH.get('coach')
                    if held is not None and COACH.get('presetRevision')!=preset_navigation.NAVIGATOR.patches.get(held.device,{}).get('revision'):held=None
                    if preset_navigation.NAVIGATOR.state.get('scanning'):held=None   # presets change every bar
                    result=sound_chat.listen_ahead(held)
            elif self.path.startswith('/api/coach/'):
                import sound_chat
                if self.path.rsplit('/',1)[1] not in ('knobs','instances','learned','presets','hear'):
                    sound_chat.forget_ahead()   # this coach action can move controls
                with engine.LOCK:result=coach_action(self.path.rsplit('/',1)[1],b)
            elif self.path.startswith('/api/scope/'):
                # Deliberately outside engine.LOCK: the scope only reads audio the taps publish,
                # and a frame must not wait behind a knob write to Live.
                result=scope_action(self.path.rsplit('/',1)[1],b)
            elif self.path.startswith('/api/memory/'):
                import memory_page
                action=self.path.rsplit('/',1)[1]
                handler={'forget':memory_page.forget,'word':memory_page.add_word,'restore':memory_page.restore,
                         'export':lambda b:memory_page.export()}.get(action)
                if handler is None:raise ValueError('Unknown memory action.')
                with engine.LOCK:result=handler(b)
            elif self.path=='/api/reference-refine':
                # His switch for measure -> adjust -> re-measure toward the reference (off unless he turns it on).
                with engine.LOCK:
                    engine.STATE['referenceRefine']=b.get('on') is True;engine.save()
                    result={'referenceRefine':engine.STATE['referenceRefine']}
            elif self.path in ('/api/fx/load','/api/fx/inspect','/api/fx/migrate'):
                # Effects into Scrunch Host FX (fx_migrate.py): a new effect, or moving an existing instance with its
                # current settings. Explicit requests only; nothing here is called by a model.
                import fx_migrate
                with engine.LOCK:
                    a=engine.host()
                    if self.path=='/api/fx/load':result=fx_migrate.load_new(a,str(b['track']),str(b['plugin']),after_device=b.get('afterDevice'))
                    elif self.path=='/api/fx/inspect':result=fx_migrate.inspect(a,str(b['track']),str(b['device']),preset=b.get('preset'))
                    else:
                        try:result=fx_migrate.migrate(a,str(b['track']),str(b['device']),b.get('startBeat'),b.get('endBeat'),preset=b.get('preset'),
                                                      accept_unverified=tuple(b.get('acceptUnverified') or ()))
                        except fx_migrate.MigrationRefused as exc:result={'refused':str(exc),'report':exc.report}
            elif self.path=='/api/match-reference':
                # John's Match Reference mode switch (panel, dashboard): ON needs a reference and the synth hosted.
                import match_reference
                import current_sound
                with engine.LOCK:
                    current_sound.PINNED=None; current_sound.TEXT_HINT=''   # ON pins the synth selected in Live now
                    try:result=match_reference.toggle(engine.STATE,b,lambda:coach(prepare=True));engine.save()
                    finally:current_sound.PINNED=match_reference.pinned(engine.STATE)
            elif self.path=='/api/search':
                # Brooklyn Rose: plan (predicted) -> measured search toward the reference on the hosted synth.
                import brooklyn_rose,reference_target,action_layer,uuid as _uuid
                with engine.LOCK:
                    target=reference_target.set_target({},b['target']) if isinstance(b.get('target'),dict) else engine.STATE.get('referenceTarget')
                    c=coach(prepare=True);c.ensure_device()
                    rid=str(b.get('requestId') or _uuid.uuid4().hex);generation=action_layer.aar_state()[1]
                    result=brooklyn_rose.run(c,target,b.get('controls') or None,rid,generation,plan_only=b.get('planOnly') is True,
                                             restore=b.get('restoreAfter') is True)
            elif self.path=='/api/search/cancel':
                # Cancel/Stop: outside the engine lock (the search holds it); the search stops at its next safe point
                # and puts the original back with verified writes.
                import brooklyn_rose
                result=brooklyn_rose.cancel(b.get('runId') or None)
            elif self.path=='/api/search/undo':
                import brooklyn_rose
                with engine.LOCK:
                    held=COACH.get('coach')
                    import audition_pair
                    if held is not None and audition_pair.get(held):result=audition_pair.toggle(held,'original')
                    else:result=brooklyn_rose.put_back(held) if held is not None else {'status':'nothing to put back'}
            elif self.path=='/api/reference-target':
                import reference_target
                with engine.LOCK:
                    result=reference_target.set_target(engine.STATE,b);engine.save()
            elif self.path=='/api/stop':
                __import__('mix_audit').cancel_capture()
                engineer.serum_workflow.interrupt_playback()
                result=engine.stop()
            else:
                with engine.LOCK:
                    routes={'/api/profile/build':lambda:arrangement_profile.build(b),'/api/experiment/create':lambda:blind_experiment.create(arrangement_profile.root()/'experiments',arrangement_profile.load(b['profileId'])),'/api/experiment/seal':lambda:blind_experiment.seal(arrangement_profile.root()/'experiments',b['id'],arrangement_profile.load(b['profileId']),b['findings']),'/api/experiment/reveal':lambda:blind_experiment.reveal(arrangement_profile.root()/'experiments',b['id'],b['feedback']),'/api/session/feedback':lambda:feedback_preparation(b),'/api/session/sidechain-preview':lambda:sidechain_preview(b),'/api/session/architecture-proposal':lambda:architecture_proposal(b),'/api/session/recovery-preview':lambda:recovery_preview(b),'/api/session/preview':lambda:saved_preview(b),'/api/session/architecture-preview':lambda:session_plans.architecture_preview(b.get('nodes'),b.get('edges')),'/api/live-ears/reference':lambda:live_ears_dsp.select_reference(b),'/api/live-ears/start':lambda:live_ears_dsp.SERVICE.start(str(b['streamId'])),'/api/live-ears/stop':lambda:live_ears_dsp.SERVICE.stop(),'/api/live-ears/cleanup':lambda:LiveEarsSource.cleanup_stale(),'/api/export-recovery':lambda:capture_adapter.resolve_recovery(b),'/api/reference/capture':lambda:reference_ab.capture(b),'/api/reference/compare':lambda:reference_ab.compare(b),'/api/analysis/storage':lambda:ambient_analysis.configure(b.get('directory','')),'/api/presets/search':lambda:preset_search(b),'/api/pattern/write':lambda:pattern_write(b),'/api/engineer':lambda:engineer.chat(b),'/api/dream/place':lambda:__import__('dream').place(b['id'],b.get('bar',1),b.get('mode','pulse'),int(b.get('octave',0)),b.get('quantize',''),b.get('instrument','Grand Piano.adg'),snap=bool(b.get('snap'))),'/api/dream/delete':lambda:__import__('dream').remove(b['id']),'/api/connect':lambda:engine.list_clips(),'/api/source':lambda:engine.load_source(str(b['id'])),
                      '/api/checkpoint':lambda:checkpoints.capture('manual'),'/api/revert':lambda:checkpoints.restore(str(b['id'])),
                      '/api/provider':lambda:engine.provider_save(b),'/api/generate':lambda:engine.attempt(b),
                      '/api/generate-and-audition':lambda:engine.generate_and_audition(b),
                      '/api/import':lambda:engine.import_attempt(str(b['id'])),
                      '/api/recover-import':lambda:engine.import_attempt(str(b['id']),recover=True),
                      '/api/audition':lambda:engine.audition(b.get('id'))}
                    if self.path=='/api/feedback':
                        a=engine.find_attempt(b['id']); status=b['status']
                        if status not in ('accepted','rejected','candidate'): raise ValueError('Invalid decision.')
                        if a.get('replacedBy') and status!='rejected': raise ValueError('This take is archived. Download its MIDI to recover it.')
                        a['status']=status; a['feedback']=str(b.get('feedback',''))[:6000]; engine.save(); result=a
                        checkpoints.event('decision',take=a['id'],status=status,feedback=a['feedback'],sessionId=engine.STATE.get('sessionId','lab'))
                    elif self.path=='/api/memory':
                        engine.STATE['memory']=str(b['memory'])[:12000]; engine.save(); result={'saved':True}
                    elif self.path in routes: result=routes[self.path]()
                    else: return self.send(404,{'error':'Unknown action'})
            self.send(200,result)
        except (ValueError,KeyError,TypeError) as e:
            checkpoints.event('request-error',route=self.path,error=str(e)[:600])
            self.send(400,{'error':sanitize(str(e)[:600])})
        except Exception as e:
            checkpoints.event('request-error',route=self.path,error=type(e).__name__+': '+str(e)[:500])
            checkpoints.event('escalation-triggered',route=self.path,error=type(e).__name__+': '+str(e)[:500])
            # Escalation is currently a logged marker, not a live handoff: no
            # second model is called here yet. Wiring that up (which provider,
            # what context it gets, what it's allowed to do) is separate work -
            # this only guarantees every crash is recorded so a manager model
            # or a human can pick it up, and the person at the keyboard is
            # never left with a dead end.
            crash=CRASH_PERSONA if engine.STATE.get('crashPersona',True) else CRASH_PERSONA_PLAIN
            self.send(500,{'error':crash,
                'detail':'Inspect the latest handoff and checkpoint before retrying; a host write may have completed.'})

if __name__=='__main__':
    system_health.MONITOR.start()
    start_housekeeping()
    ingest.recover()
    reference_ab.recover_capture_settings()
    ambient_analysis.start()
    engine.save()
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    atexit.register(engine.safe_stop)
    print('Scrunch MIDI Engineer: '+ORIGIN,flush=True)
    checkpoints.event('server-start',pid=os.getpid(),port=PORT)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: engine.safe_stop(); server.server_close()
