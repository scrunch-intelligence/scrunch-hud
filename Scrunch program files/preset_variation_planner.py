"""One explicitly requested engineer call; exact response retained in RAM.

No listener judge, audio upload, model switch, repair call or automatic retry.
The saved Sound design engineer is selected by the producer in existing Settings.
"""
import copy
import time


def propose(request):
    import engine
    import providers
    import sound_language
    with engine.LOCK:
        profile=copy.deepcopy(sound_language.selected(engine.STATE))
    if profile is None:raise ValueError('Choose a saved Sound design engineer in Settings.')
    captured={}
    def reply(text,usage):captured.update(reply=text,usage=copy.deepcopy(usage))
    started=time.monotonic()
    error=None
    try:providers.structured(profile,request['system'],request['input'],request['responseSchema'],timeout=180,on_reply=reply)
    except Exception as exception:error=type(exception).__name__+': '+str(exception)
    if 'reply' not in captured and error is None:error='The engineer adapter did not capture its exact reply. No automatic retry.'
    return {**captured,'seconds':round(time.monotonic()-started,3),
            'provider':{'id':profile['id'],'kind':profile['kind'],'model':profile['model']},
            'error':error,'basis':'Configured adapter invocation; musical usefulness is not inferred.'}
