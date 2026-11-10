"""Compact, read-only presentation of recorded mix trials. Never queries Live."""
import copy
import hashlib
import json
from pathlib import Path
import arrangement_automation as aa


def automation_summary(automation):
    record=(automation or {}).get('record')
    if not record:return None
    result={'originalCheckPassed':bool(record.get('verified')),'requestedValuesPassed':None,
            'basis':'Saved readbacks reassessed by the current verifier; not a new Live measurement.',
            'points':[]}
    try:
        q=aa.verify_samples(record['lanes'],record['before'],record['post'],record['beats'],record['tempo'])
        result.update(requestedValuesPassed=bool(record.get('recorded') and not record.get('status',{}).get('error') and q['reproductionPassed']),
                      samplesComplete=q['samplesComplete'],maxErrorFractionOfRequestedMove=q['maxErrorFractionOfRequestedMove'],
                      method=q['verificationMethod'])
        checks=q['checks']
    except (KeyError,TypeError,ValueError):
        checks=record.get('checks',[])
        result['basis']='Saved record lacks the fields needed for current verification. Historical readbacks only.'
    for lane in (automation or {}).get('lanes',[]):
        for p in lane.get('points',[]):
            c=next((c for c in checks if c.get('id')==lane.get('id') and c.get('beat')==p.get('beat')),None)
            result['points'].append({'control':lane.get('label',lane.get('name','Control')),
                'beat':p.get('beat'),'requested':p.get('value'),'unit':lane.get('unit',''),
                'readDisplay':c.get('display') if c else None,
                'withinTolerance':c.get('ok') if c and result['requestedValuesPassed'] is not None else None})
    return result


def source_association(run, feedback_root=None):
    """Verify a recorded identity against the saved source, never against Live."""
    ident=run.get('feedbackId');index=run.get('comment')
    result={'reportId':ident, 'commentIndex':index, 'verifiedSavedSource':False,
            'basis':'No verified saved source association. Text similarity is not identity.',
            'cue':copy.deepcopy(run.get('cue')), 'currentSetVerified':False}
    if feedback_root is None or not isinstance(ident,str) or type(index) is not int or index<0:
        return result
    try:
        import production_feedback
        if not (Path(feedback_root)/(ident+'.json')).is_file():
            raise ValueError('Saved source missing.')
        report=production_feedback.load(feedback_root,ident)
        row=report['comments'][index];note=run.get('note') or {}
        if row['text']!=note.get('text') or report['sourceLabel']!=note.get('source'):
            result['basis']='Recorded source identity disagrees with the saved note; no association verified.'
            return result
        result.update(verifiedSavedSource=True,sessionName=report.get('sessionName'),
                      observationFingerprint=report.get('observationFingerprint'),
                      bounceStartBar=report.get('bounceStartBar',1),section=copy.deepcopy(row.get('section')),
                      basis='Exact report ID, note index, quoted text and source verified against the integrity-checked saved report. Historical snapshot only; current Set and tempo-map alignment are unverified.')
    except (ValueError,KeyError,IndexError,TypeError,OSError,AttributeError):
        result['basis']='Saved source is missing, corrupt or incomplete; association remains unverified.'
    return result


def summarize(run, feedback_root=None):
    note=run.get('note') or {};trials=[]
    saved_trials=run.get('candidateTrials',[])
    if not saved_trials and isinstance(run.get('result'),dict):
        offer=run.get('offer') or {}
        saved_trials=[{'result':run['result'],'plan':offer.get('plan'),'applied':offer.get('applied')}]
    for trial in saved_trials:
        result=trial.get('result') or {};plan=trial.get('plan') or {}
        cleanup=result.get('rigCleanup') or {}
        trials.append({'bars':plan.get('auditionBars'),
                       'mainChange':copy.deepcopy(result.get('mainProblemBand')),
                       'captureCoverage':copy.deepcopy(result.get('captureCoverage')),
                       'automation':automation_summary((trial.get('applied') or {}).get('automation')),
                       'restoredDuringTrial':bool(result.get('revertCheck')),
                       'captureTracksRestored':cleanup.get('setTracksRestored')})
    recovery=run.get('recovery') or {}
    return {'id':run['id'],'createdAt':run.get('createdAt'),'status':run.get('status','unknown'),
            'note':note.get('text',''),'source':note.get('source','Not recorded'),
            'sourceAssociation':source_association(run,feedback_root),
            'replayed':bool(run.get('replayOf')),'modelCalls':run.get('modelCalls'),
            'error':run.get('error'),'trials':trials,
            'failedAutomation':copy.deepcopy(recovery.get('automationReadback')),
            'rollbackError':recovery.get('rollbackError'),
            'basis':'Historical trial evidence. It does not describe the current Set or establish musical acceptance.'}


def replay_origin(run, root):
    """Only an explicit replay reference plus the exact quoted note proves lineage."""
    if not run.get('replayOf'):return run,None
    try:
        origin,h=_read(root,run['replayOf'])
        if origin.get('replayOf'):raise ValueError('Nested replay lineage is not verified.')
        if not isinstance(run.get('note'),dict) or run['note']!=origin.get('note'):
            raise ValueError('Replay note differs from its recorded original.')
        return origin,h
    except ValueError:return None,None


def replay_source_association(run, root):
    source_root=Path(root).parent/'production-feedback'
    origin,_=replay_origin(run,root)
    if origin is None:
        result=source_association(run)
        result['basis']='Replay lineage is missing or disagrees with the original note; no saved source attributed.'
        return result
    result=source_association(origin,source_root)
    if run.get('replayOf'):
        result['originRunId']=origin['id']
        result['originalCue']=result.pop('cue',None)
        result['cue']=copy.deepcopy(run.get('cue'))
        result['basis']='Replay linked through the explicit original run ID and exact quoted note. '+result['basis']+' Original timestamp mapping is not a new replay alignment measurement.'
    return result


def history(root,limit=8):
    files=sorted(Path(root).glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    reports=[];unreadable=0
    for path in files:
        if len(reports)>=limit:break
        try:
            run=json.loads(path.read_text())
            if not isinstance(run,dict) or not run.get('id'):continue
            summary=summarize(run,Path(root).parent/'production-feedback')
            summary['sourceAssociation']=replay_source_association(run,root)
            reports.append(summary)
        except (OSError,ValueError,KeyError,TypeError):unreadable+=1
    return {'reports':reports,'unreadableReports':unreadable}


def _read(root,ident):
    if not isinstance(ident,str) or len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):
        raise ValueError('Unknown evidence record.')
    path=Path(root)/(ident+'.json')
    try:
        raw=path.read_bytes();record=json.loads(raw)
        if not isinstance(record,dict) or record.get('id')!=ident:raise ValueError('Record ID mismatch.')
        return record,hashlib.sha256(raw).hexdigest()
    except (OSError,ValueError):raise ValueError('Evidence record is missing or unreadable.') from None


def trace(run_root,call_root,ident):
    """Read recorded decisions and their journals without re-running any action.

    Older journals have no direct run reference. An unambiguous exact note/reply
    match within the original step's time interval is labelled reconstructed.
    Replays resolve against their original run, never pretend to be fresh calls.
    """
    run,run_hash=_read(run_root,ident);origin=run;origin_hash=run_hash
    lineage_verified=True
    if run.get('replayOf'):
        linked,linked_hash=replay_origin(run,run_root)
        lineage_verified=linked is not None
        if lineage_verified:origin,origin_hash=linked,linked_hash
    journals=[];unreadable=0
    for path in Path(call_root).glob('*.json'):
        try:
            j,h=_read(call_root,path.stem)
            if j.get('purpose')=='mix-engineer':journals.append((j,h))
        except ValueError:unreadable+=1
    decisions=[]
    for n,step in enumerate(run.get('steps',[]),1):
        answer=step.get('answer');source_step=step
        if run.get('replayOf'):
            matches=[s for s in origin.get('steps',[]) if s.get('answer')==answer]
            source_step=matches[0] if len(matches)==1 else {}
        candidates=[];basis='No exact input journal linked.'
        for j,h in journals:
            if not lineage_verified:continue
            assoc=j.get('association') or {}
            direct=(j['id'] in origin.get('modelCallIds',[]) and assoc.get('runId')==origin['id']
                    and assoc.get('step')==source_step.get('n') and j.get('reply')==answer
                    and j.get('context',{}).get('note')==origin.get('note'))
            at=source_step.get('at');ms=source_step.get('modelMs')
            legacy=(not origin.get('modelCallIds') and isinstance(at,(int,float)) and isinstance(ms,(int,float))
                    and at-ms/1000-1<=j.get('startedAt',0)<=at
                    and j.get('context',{}).get('note')==origin.get('note') and j.get('reply')==answer)
            if direct or legacy:candidates.append((j,h,'Direct recorded link.' if direct else 'Reconstructed: unique exact note/reply match within the original step time interval.'))
        item={'step':n,'answer':copy.deepcopy(answer),'executionError':step.get('error'),
              'executed':copy.deepcopy(step.get('executed')),'measured':copy.deepcopy(step.get('measured')),
              'journal':None,'linkBasis':basis if lineage_verified else 'Replay lineage unverified; no original model input attributed.'}
        if len(candidates)==1:
            j,h,basis=candidates[0]
            item.update(linkBasis=basis,journal={'id':j['id'],'sha256':h,'status':j['status'],
                'model':{k:j.get('provider',{}).get(k) for k in ('name','kind','model')},
                'startedAt':j.get('startedAt'),'elapsedMs':j.get('elapsedMs'),'usage':copy.deepcopy(j.get('usage')),
                'cost':j.get('cost'),'costStatus':j.get('costStatus'),
                'input':{k:copy.deepcopy(j.get(k)) for k in ('system','context','schema')},
                'reply':copy.deepcopy(j.get('reply'))})
        elif len(candidates)>1:item['linkBasis']='Ambiguous historical match; no input journal attributed.'
        decisions.append(item)
    # Failed provider calls may not have produced a step or a reply.
    used={d['journal']['id'] for d in decisions if d['journal']}
    unfinished=[{'id':j['id'],'status':j.get('status'),'sha256':h,'error':j.get('error')}
                for j,h in journals if j['id'] in origin.get('modelCallIds',[]) and j['id'] not in used]
    return {'runId':ident,'runSha256':run_hash,'originRunId':origin['id'] if lineage_verified else None,'originSha256':origin_hash if lineage_verified else None,
            'replayed':bool(run.get('replayOf')),'recordedModelCalls':run.get('modelCalls'),
            'sourceAssociation':replay_source_association(run,run_root),
            'replayLineageVerified':lineage_verified if run.get('replayOf') else None,
            'decisions':decisions,'unmatchedLinkedCalls':unfinished,'unreadableJournals':unreadable,
            'basis':'Read-only historical presentation. Model words are proposals or claims, not proof of execution or musical improvement. No model or Live action is run by opening this record.',
            'exactness':'Saved semantic system/context/schema and parsed replies; original HTTP bytes were not retained.'}
