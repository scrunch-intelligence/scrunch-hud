"""Bounded DAW actions. Resolve tracks afresh; never accept arbitrary tool calls."""
import json, math, re
import engine, daw_adapter, workflow_requirements



def validate(spec):
    if not isinstance(spec, dict): raise ValueError('DAW command must be an object.')
    op=spec.get('op')
    allowed={'volume':{'op','track','db'},'mute':{'op','track','enabled'},'solo':{'op','track','enabled'},
             'play_track':{'op','track'},'play_loop':{'op'},'rewind':{'op'},'loop':{'op','enabled','start','end'}}
    if op not in allowed or set(spec)!=allowed[op]: raise ValueError('Unsupported DAW command fields.')
    if op in ('volume','mute','solo','play_track') and (not isinstance(spec['track'],str) or not spec['track'].strip()):
        raise ValueError('Name a track or its displayed track number.')
    if op=='volume' and (type(spec['db']) not in (int,float) or not math.isfinite(spec['db']) or not -70<=spec['db']<=6):
        raise ValueError('Track volume must be between -70 and +6 dB.')
    if op in ('mute','solo','loop') and type(spec['enabled']) is not bool: raise ValueError('Specify on or off.')
    if op=='loop' and (type(spec['start']) is not int or type(spec['end']) is not int or not 1<=spec['start']<spec['end']<=10000):
        raise ValueError('Loop needs a start bar and an exclusive end bar, for example 1 to 5 for four bars.')
    return spec


def execute(spec):
    validate(spec)
    daw_adapter.require(engine.host(),workflow_requirements.daw_command(spec,bool(engine.AUDITION) and spec['op']!='play_loop'),'run this DAW command')
    with engine.PLAY_LOCK:
        # End temporary audition before changing mixer state: its restoration must not undo the command.
        if engine.AUDITION: engine.stop()
        ls=engine.lab_session();op=spec['op'];host=engine.host()
        numerator,denominator=(int(v) for v in (ls.get('timeSignature') or '4/4').split('/'))
        beats_per_bar=numerator*4/denominator
        if op=='play_track':
            label=spec['track'].strip();number=re.fullmatch(r'(?:track|channel)\s+(\d+)',label,re.I)
            matches=[t for t in ls['tracks'] if (t['trackIndex']==int(number[1])-1 if number else t['name'].casefold()==label.casefold())]
            if len(matches)!=1:raise ValueError('That track is not in the current set. Choose one of the displayed Ableton track numbers.')
            target=host.track(matches[0]['id'],clips=True,mixer=True)
            starts=[c['start'] for c in target.get('clips',[]) if c.get('start') is not None]
            start=min(starts) if starts else 0
            for track in ls['tracks']:
                host.update_track(track['id'],solo=track['id']==target['id'])
            host.update_track(target['id'],mute=False)
            after=host.track(target['id'],mixer=True)
            if not (after.get('soloed') or after.get('mutedViaSolo')):raise ValueError('Track solo did not apply. Try the track number again after Ableton finishes updating.')
            playback=host.play_arrangement(start)
            if not playback.get('playing'):raise ValueError('Ableton transport did not start. Check whether a dialog is open, then say play again.')
            return 'Playing track '+str(matches[0]['trackIndex']+1)+' from bar '+str(int(start//4)+1)+' until Stop.'
        if op=='play_loop':
            region=engine.STATE.get('arrangementLoop')
            if not region or not region['enabled']:raise ValueError('Set a loop range first.')
            after=host.play_arrangement((region['start']-1)*beats_per_bar,loop=((region['start']-1)*beats_per_bar,(region['end']-1)*beats_per_bar))
            if not after.get('playing') or not after.get('loop'):raise ValueError('Ableton did not confirm looping playback.')
            return 'Playing the Arrangement loop until Stop.'
        if op in ('volume','mute','solo'):
            label=spec['track'].strip();m=re.fullmatch(r'(?:track|channel)\s+(\d+)',label,re.I)
            matches=[t for t in ls['tracks'] if (t['trackIndex']==int(m[1])-1 if m else t['name'].casefold()==label.casefold())]
            if len(matches)!=1: raise ValueError('Track name is missing or ambiguous; use its exact name or displayed track number.')
            t=host.track(matches[0]['id'],mixer=True)
            key='gainDb' if op=='volume' else op;value=spec['db'] if op=='volume' else spec['enabled']
            host.update_track(t['id'],**{key:value})
            after=host.track(t['id'],mixer=True)
            gain=after.get('gainDb')
            if op=='volume' and (type(gain) not in (int,float) or abs(gain-value)>.15): raise ValueError('Volume readback did not match; inspect Ableton.')
            if op!='volume':
                via_solo=after.get('mutedViaSolo')
                observed=(after.get('muted') or via_solo) if op=='mute' else (after.get('soloed') or via_solo)
                if observed!=value: raise ValueError('Mixer readback did not match; inspect Ableton.')
            result=f"{t['name']}: {str(value)+' dB' if op=='volume' else op+' '+('on' if value else 'off')}."
        else:
            if op=='rewind':
                engine.stop();after=host.move_playhead(0)
            else:after=host.set_arrangement_loop(spec['enabled'],(spec['start']-1)*beats_per_bar,(spec['end']-1)*beats_per_bar)
            if op=='loop':
                expected={'start':(spec['start']-1)*beats_per_bar,'end':(spec['end']-1)*beats_per_bar}
                if spec['enabled'] and after.get('loop')!=expected:raise ValueError('Ableton loop readback did not match.')
                engine.STATE['arrangementLoop']=dict(spec);engine.save()
            result='Returned to bar 1; playback stopped.' if op=='rewind' else f"Loop {'on' if spec['enabled'] else 'off'}: bars {spec['start']}–{spec['end']} (end exclusive)."
        engine.DATA.joinpath('last-daw-action.json').write_text(json.dumps({'command':spec,'readback':after},indent=2))
        return result
