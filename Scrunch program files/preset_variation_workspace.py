"""App-facing, RAM-only favourite-preset workflow. No Live calls.

The engineer receives the exact prepared request and supplies its exact reply.
Only the explicit generate action invokes one configured provider, with no retry.
Audition is explicit; these endpoints only return WAV/preset bytes. One source
and one listening round are retained. Clear drops the entire workspace.
"""
import copy
import io
import threading
import time
import uuid

from preset_variation_serum import selected_preset,selected_bytes
from preset_variations import VariationError


class Busy(VariationError):pass


# Identifies this run of the app service. Everything here lives in RAM, so a new BOOT means any earlier comparison,
# conversation and unsaved preset is gone; the page compares it with the one it last saw to say so plainly.
BOOT=uuid.uuid4().hex
STARTED=time.time()
LONG={'select','select-upload','generate','reply','branch'}
WORDS={'select':'Loading your preset and checking the MIDI','select-upload':'Loading your preset and checking the MIDI',
       'generate':'Asking your engineer for three ideas','reply':'Rendering the three ideas','branch':'Making your chosen take the new starting point'}


class Workspace:
    def __init__(self,loader=selected_preset,planner=None,upload_loader=selected_bytes):
        self.loader=loader;self.upload_loader=upload_loader;self.planner=planner;self.lock=threading.Lock();self.session=None;self.favorite=None;self.id=None;self.prepared=None;self.preview=None
        self.status=None   # replaced, never mutated in place, so a reader without the lock sees a whole value

    def _step(self,step):
        if self.status is not None:self.status={**self.status,**step,'at':time.time()}

    def progress(self):
        """What a long operation is doing now; readable while the workspace lock is held."""
        return copy.deepcopy(self.status)

    def _failure(self):
        p=self.prepared or {}
        if not p.get('executionError'):return None
        reply=bool((p.get('response') or {}).get('reply')) and not (p.get('response') or {}).get('error')
        return {'error':p['executionError'],'kind':p.get('executionErrorKind','execution'),
                'replyRetained':reply,'canFinishWithRetainedReply':reply and p.get('executionErrorKind')=='studio-connection'}

    def _current(self,body):
        if self.session is None or body.get('sessionId')!=self.id:raise VariationError('This selected preset session is no longer current.')
        return self.session

    def snapshot(self):
        if not self.lock.acquire(blocking=False):
            return {'busy':True,'progress':self.progress(),'bootId':BOOT,'serviceStarted':STARTED}
        try:return {'sessionId':self.id,'sourcePreset':self.session.source_label if self.session else None,
                    'report':self.session.report() if self.session else None,
                    'preparedRequest':self.prepared,'audioStored':False,'busy':False,'bootId':BOOT,'serviceStarted':STARTED,
                    'lastFailure':self._failure(),
                    'canReturnToFavourite':self.favorite is not None and self.session is not self.favorite}
        finally:self.lock.release()

    def action(self,name,body):
        if not isinstance(body,dict):raise VariationError('Supply an object for this preset operation.')
        if not self.lock.acquire(blocking=False):raise Busy('The preset workspace is busy; no second render was started.')
        if name in LONG:self.status={'action':name,'phase':WORDS[name],'started':time.time(),'at':time.time()}
        if self.session is not None:self.session.progress=self._step
        try:
            if name in ('select','select-upload'):
                # Failed selection leaves the preceding usable session intact.
                if name=='select-upload':
                    from preset_variation_inputs import decode,phrase
                    midi=decode(body.get('midiBase64'),256*1024,'MIDI')
                    new=self.upload_loader(decode(body.get('presetBase64'),8*1024*1024,'Preset'),body.get('presetName'),phrase(midi))
                else:new=self.loader(body.get('presetPath',''),body.get('phrase'),locked_controls=body.get('retainedControlIds',()))
                self.session=new;self.favorite=new;self.id=uuid.uuid4().hex;self.prepared=None;self.preview=None
                return {'sessionId':self.id,'sourcePreset':new.source_label,'sourceSha256':new.source_hash,
                        'phraseSha256':new.phrase_hash,'audioStored':False,'modelCalls':0}
            if name=='clear':
                self.session=None;self.favorite=None;self.id=None;self.prepared=None;self.preview=None
                return {'cleared':True,'storage':'memory','modelCalls':0,'liveChanges':False}
            session=self._current(body)
            if name=='restart-favourite':
                self.session=self.favorite;self.session.clear();self.id=uuid.uuid4().hex;self.prepared=None;self.preview=None
                return {'sessionId':self.id,'sourcePreset':self.session.source_label,'modelCalls':0,'liveChanges':False}
            if name=='request':
                words=body.get('words')
                if not isinstance(words,str) or not words.strip() or len(words)>12000:raise VariationError('Describe the variations you want.')
                self.prepared={'id':uuid.uuid4().hex,'request':session.request(words)}
                return {'sessionId':self.id,**self.prepared,'modelCalls':0}
            if name=='reply':
                if self.prepared is None or self.prepared['id']!=body.get('requestId'):
                    raise VariationError('This engineer request is no longer current.')
                # Discard cached preview buffers; batch IDs identify valid results.
                self.preview=None
                captured=self.prepared.get('response')
                if captured is not None:
                    if captured.get('error') or body.get('reply')!=captured.get('reply'):
                        raise VariationError('Recovery must use the exact retained engineer reply.')
                    provenance='fresh-provider'
                else:provenance=body.get('provenance')
                try:session.run_reply(self.prepared['request'],body.get('reply'),provenance=provenance)
                except (ValueError,OSError,RuntimeError) as error:
                    self._record(error)
                    raise
                if captured is not None:
                    session.batch['modelEvidence'].update(usage=captured.get('usage'),seconds=captured.get('seconds'),
                        provider=captured.get('provider'),verificationBasis=captured.get('basis'),
                        recoveredRendering=True)
                self.prepared=None
                return {'sessionId':self.id,**session.report(),'modelCallsByExecutor':0}
            if name in ('generate','claim-request'):
                if name=='generate' and self.planner is None:raise VariationError('The engineer adapter is unavailable for this workspace.')
                if self.prepared is None or self.prepared['id']!=body.get('requestId'):
                    raise VariationError('This engineer request is no longer current.')
                if self.prepared.get('attempted'):raise VariationError('This engineer request was already attempted. Inspect its result before requesting another call.')
                self.prepared['attempted']=True  # Commit before dispatch, even on interruption/failure.
                if name=='claim-request':return self.prepared['request']
                response=self.planner(self.prepared['request'])
                return self._finish(session,response)
            if name=='finish-request':
                if self.prepared is None or self.prepared['id']!=body.get('requestId') or not self.prepared.get('attempted') or 'response' in self.prepared:
                    raise VariationError('This claimed engineer request is no longer available.')
                return self._finish(session,body['response'])
            if name=='choose':return session.choose(body.get('batchId'),body.get('name'),words=body.get('words'))
            if name=='branch':
                child=session.branch(body.get('batchId'),body.get('name'))
                # Retain one original anchor, not every rendered generation.
                self.favorite.clear()
                self.session=child;self.id=uuid.uuid4().hex;self.prepared=None;self.preview=None
                return {'sessionId':self.id,'sourcePreset':child.source_label,'sourceSha256':child.source_hash,
                        'phraseSha256':child.phrase_hash,'modelCalls':0}
            if name=='report':return {'sessionId':self.id,'report':session.report(),'audioStored':False}
            if name=='source-preset':
                if not hasattr(session,'source_preset_bytes'):raise VariationError('This starting sound cannot be exported as a preset.')
                return session.source_preset_bytes(),'application/octet-stream'
            if name in ('audio','preset','comparison'):
                if name=='audio' and session.batch is None and body.get('name')=='original':
                    from preset_variations import matched_previews
                    import soundfile as sf
                    preview=matched_previews([session.original['audio']],session.phrase['sample_rate'],meter=session.meter)
                    output=io.BytesIO();sf.write(output,preview['audio'][0],session.phrase['sample_rate'],format='WAV',subtype='FLOAT')
                    return output.getvalue(),'audio/wav'
                if session.batch is None or body.get('batchId')!=session.batch['id']:raise VariationError('This listening batch is no longer current.')
                if name=='preset':return session.preset_bytes(body['batchId'],body.get('name')),'application/octet-stream'
                if self.preview is None:self.preview=session.previews()
                if name=='comparison':return {k:v for k,v in self.preview.items() if k!='audio'}
                ident=body.get('name')
                if ident not in self.preview['labels']:raise VariationError('Unknown comparison sound.')
                import soundfile as sf
                output=io.BytesIO();sf.write(output,self.preview['audio'][self.preview['labels'].index(ident)],
                                           self.preview['sampleRate'],format='WAV',subtype='FLOAT')
                return output.getvalue(),'audio/wav'
            raise VariationError('Unknown favourite-preset operation.')
        finally:
            self.status=None
            self.lock.release()

    def _record(self,error):
        if self.prepared is None:return
        from preset_variation_studio_render import StudioUnavailable
        self.prepared['executionError']=str(error)
        self.prepared['executionErrorKind']='studio-connection' if isinstance(error,StudioUnavailable) else 'execution'

    def _finish(self,session,response):
        self.prepared['response']=response
        if response.get('error'):raise VariationError('No new variations were created. The engineer could not process your request: '+str(response['error'])+' Your starting sound is unchanged.')
        self.preview=None
        self._step({'phase':'Engineer replied; rendering the three ideas on the Mac Studio' if 'Studio' in type(self).__name__ else 'Engineer replied; rendering the three ideas'})
        try:session.run_reply(self.prepared['request'],response['reply'],provenance='fresh-provider')
        except (ValueError,OSError,RuntimeError) as error:
            self._record(error)
            raise
        session.batch['modelEvidence'].update(usage=response.get('usage'),seconds=response.get('seconds'),
            provider=response.get('provider'),verificationBasis=response.get('basis'))
        self.prepared=None
        return {'sessionId':self.id,**session.report(),'configuredProviderCalls':1,'automaticRetries':0}


from preset_variation_planner import propose
def configured_workspace():
    import engine
    if engine.STATE.get('presetExecution')=='studio':
        from preset_variation_studio_render import selected,uploaded
        import preset_variation_studio_render as studio
        class StudioWorkspace(Workspace):
            def snapshot(self):return {**super().snapshot(),'executionComputer':'Mac Studio','listeningComputer':'This laptop'}
            def action(self,name,body):
                if name=='generate':
                    # Reach the Studio before spending an engineer call on ideas it could not render.
                    try:studio.check()
                    except studio.StudioUnavailable as error:
                        raise studio.StudioUnavailable(str(error)+' No engineer call was made; your starting sound is unchanged.') from None
                value=super().action(name,body)
                return {**value,'executionComputer':'Mac Studio','listeningComputer':'This laptop'} if isinstance(value,dict) else value
        return StudioWorkspace(loader=selected,planner=propose,upload_loader=uploaded)
    return Workspace(planner=propose)

WORKSPACE=configured_workspace()
