"""Stage 4 append-only architecture proposals. No host writes or inferred routing."""
import copy,hashlib,json
import session_plans

def propose(observation, groups, returns, sends):
    session_plans.fingerprint(observation)
    if not all(isinstance(x,list) for x in (groups,returns,sends)) or len(groups)+len(returns)>16 or len(sends)>64:
        raise ValueError('Bounded group, return and send lists required')
    tracks=observation['tracks'];by_id={t['id']:t for t in tracks}
    if len(by_id)!=len(tracks):raise ValueError('Ambiguous observed identities')
    masters=[t for t in tracks if t.get('type')=='master']
    if len(masters)!=1:raise ValueError('One observed Main track required')
    master=masters[0]['id'];nodes=[];edges=[];actions=[];members=set();names={t.get('name') for t in tracks}
    for t in tracks:
        if t.get('type') not in ('audio','midi','return','master','group'):raise ValueError('Unsupported observed track kind')
        nodes.append({'id':t['id'],'kind':'group' if t.get('isGroup') is True else t['type'],'name':t.get('name'),'existing':True})
    def new_node(name,kind,index):
        if not isinstance(name,str) or not name.strip() or len(name)>100 or name in names:
            raise ValueError('New track names must be explicit, unique and at most 100 characters')
        names.add(name);ident=f'new:{kind}:{index}'
        if ident in by_id:raise ValueError('Symbolic identity collision')
        nodes.append({'id':ident,'kind':kind,'name':name,'existing':False})
        actions.append({'kind':'create-'+kind,'symbolicId':ident,'name':name,'gainDb':-12,'status':'UNVERIFIED'})
        edges.append({'from':ident,'to':master,'kind':'output'})
        return ident
    for i,g in enumerate(groups):
        if not isinstance(g,dict) or set(g)!={'name','members'} or not isinstance(g['members'],list) or not g['members']:
            raise ValueError('Group requires a name and explicit member track IDs')
        ident=new_node(g['name'],'group',i)
        for member in g['members']:
            if not isinstance(member,str):raise ValueError('Member IDs must be strings')
            t=by_id.get(member)
            if not t or t['type'] not in ('audio','midi') or t.get('isGroup') or t.get('isGroupMember') or t.get('groupId') or member in members:
                raise ValueError('Group member missing, unsupported or assigned more than once')
            if t.get('isFrozen') is True or t.get('frozen') is True:
                raise ValueError('Frozen group member requires separate host verification')
            members.add(member);edges.append({'from':member,'to':ident,'kind':'output'})
            actions.append({'kind':'group-member','trackId':member,'group':ident,'beforeRouting':copy.deepcopy({k:t.get(k) for k in ('outputRoutingType','outputRoutingChannel','sends')}),'status':'UNVERIFIED'})
    new_returns={}
    for i,name in enumerate(returns):new_returns[name]=new_node(name,'return',i)
    for send in sends:
        if not isinstance(send,dict) or set(send)!={'trackId','returnName','gainDb'}:raise ValueError('Send requires explicit source, return name and gain')
        if not isinstance(send['trackId'],str) or not isinstance(send['returnName'],str):raise ValueError('Send identities must be strings')
        source=by_id.get(send['trackId']);target=new_returns.get(send['returnName'])
        if not source or source['type'] not in ('audio','midi') or not target:raise ValueError('Send source or proposed return is unavailable')
        edges.append({'from':source['id'],'to':target,'kind':'send','gainDb':send['gainDb']})
    topology=session_plans.architecture_preview(nodes,edges)
    result={'mode':'dry-run','observationFingerprint':session_plans.fingerprint(observation),'topology':topology,'operations':actions,
            'existingContext':copy.deepcopy(tracks),'executed':False,'actAvailable':False,
            'summary':f'Propose {len(groups)} groups, {len(returns)} returns and {len(sends)} sends. No Live changes.',
            'gates':['Proposed connections only, not a complete current routing graph; unmentioned routes are not removed.',
                     'Live group creation, membership, routing readback, PDC and rollback remain UNVERIFIED.',
                     'Return processors are not selected or loaded; no effect sound is promised.',
                     'New buses start at -12 dB; this changes gain structure and needs an approved audition.',
                     'No existing clip, device, automation or routing is overwritten by this preview.']}
    result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return result
