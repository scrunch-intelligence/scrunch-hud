"""Complete backend requirements for each multi-step Scrunch workflow.

Every entry lists all adapter operations the workflow may perform, including
reads, so daw_adapter.require() can refuse the whole plan before its first
mutation (or paid generation). UI automation outside the adapter is not listed.
"""
import json
import daw_adapter

READ={'session','track','clip','transport'}
STOP={'stop_playback','update_track.solo','update_track.mute'}
AUDITION=READ|STOP|{'stop_session_clips','update_track.arm','play_arrangement'}
IMPORT=READ|{'duplicate_track','update_track.gainDb','update_track.arm','update_track.solo',
             'update_track.mute','update_track.monitoring','create_arrangement_clip'}
VERIFY_IMPORTED=READ
REPLACE=READ|STOP|{'update_track.arm','update_track.gainDb','update_track.name','replace_clip_notes'}
RECOVER_REPLACEMENT=READ|{'update_track.name'}
NEW_SYNTH_TRACK={'create_midi_track','update_track.gainDb','update_track.mute','update_track.arm'}
SERUM_SETUP=READ|NEW_SYNTH_TRACK|{'selected_track','select_track','host_schema_reads','create_arrangement_clip'}
FRESH_SESSION=READ|STOP

SERUM_STEPS={
    'variation':READ|NEW_SYNTH_TRACK|{'create_arrangement_clip'},
    'load':READ|NEW_SYNTH_TRACK|{'host_schema_reads','create_arrangement_clip','create_arrangement_clip.looping','selected_track','select_track'},
    'preset':READ|{'host_schema_reads','open_plugin_window'},
    'next':READ|{'host_schema_reads','open_plugin_window'},
    'show':READ|{'host_schema_reads','open_plugin_window'},
    'loop':AUDITION,
    'stop':STOP}

DAW_OPS={
    'volume':READ|{'update_track.gainDb'},
    'mute':READ|{'update_track.mute'},
    'solo':READ|{'update_track.solo'},
    'play_track':READ|{'update_track.solo','update_track.mute','play_arrangement'},
    'play_loop':READ|{'play_arrangement'},
    'loop':READ|{'set_arrangement_loop'},
    'rewind':READ|STOP|{'move_playhead'}}

EFFECT_ADD=READ|{'host_schema_reads','create_native_effect','set_device_parameters'}
EFFECT_REMOVE=READ|{'host_schema_reads','delete_device'}
SYNTH_SET=READ|{'host_schema_reads','set_rack_parameter'}
SYNTH_RESTORE=SYNTH_SET
# Scrunch-owned native device contract (no current workflow declares these yet).
NATIVE_DEVICE_READ={'session','devices','device','device_parameter'}
NATIVE_PARAMETER_SET=NATIVE_DEVICE_READ|{'set_device_parameter'}

# Recorded arrangement automation (automation_actions.py): proposal reads, then audition (armed check, record,
# read back) and his choice (Live undo of Scrunch's recording, or recording another side).
AUTOMATION_PROPOSE=READ|{'automation_target'}
AUTOMATION_AUDITION=AUTOMATION_PROPOSE|STOP|{'host_schema_reads','record_automation','automation_take_status',
                                             'stop_automation_take','sample_automation','play_span'}
AUTOMATION_CHOOSE=AUTOMATION_AUDITION|{'undo_automation_take'}
# Engineer-note mixing (mix_engineer.py): reads, captures and plays through its own verified paths; the chat only
# needs the Set readable, Live stoppable and span playback for the audition.
MIX_ENGINEER=READ|STOP|{'play_span'}

# Audio clip -> MuScriptor -> new MIDI track below it (audio_to_midi.py).
EXTRACT_MIDI=READ|{'clip.audio_source','create_midi_track','update_track.gainDb','update_track.arm','load_device','create_arrangement_clip'}

def serum_plan(steps):
    needed=set(READ)
    for step in steps:needed|=SERUM_STEPS[step['op']]
    return needed

def daw_command(spec,audition_active=False):
    return DAW_OPS[spec['op']]|(STOP if audition_active else set())

def placement(replacing):
    return (REPLACE|VERIFY_IMPORTED) if replacing else IMPORT

def for_action(action,replacing_generation=False):
    """Requirements for one validated engineer action."""
    kind=action['action']
    if kind in ('switch_model','accept','reject','show'):return set()
    if kind=='fresh_session':return set(FRESH_SESSION)
    if kind=='serum_workflow':return serum_plan(json.loads(action['prompt'])['steps'])
    if kind=='generate':return placement(replacing_generation)|AUDITION
    if kind=='play':return set(AUDITION)
    if kind=='edit':return READ|{'select_clip'}
    if kind=='daw':return daw_command(json.loads(action['prompt']),audition_active=True)
    if kind=='confirm_synth':return READ|{'host_schema_reads'}
    if kind in ('setup_serum','setup_diva','setup_jup'):return set(SERUM_SETUP)
    if kind=='native_effect':
        return EFFECT_REMOVE if json.loads(action['prompt']).get('op')=='remove' else EFFECT_ADD
    if kind in ('serum','diva','jup'):
        return SYNTH_RESTORE if json.loads(action['prompt']).get('op')=='restore' else SYNTH_SET
    if kind=='stop':return set(STOP)
    if kind=='extract_midi':return set(EXTRACT_MIDI)
    if kind in ('mix_note','mix_choose'):return set(MIX_ENGINEER)
    if kind=='automation':
        spec=json.loads(action['prompt'])
        if spec.get('op')=='choose':return set(AUTOMATION_CHOOSE)
        devices=any('device' in lane.get('target',{}) for lane in spec.get('lanes',[]))
        return (AUTOMATION_PROPOSE if spec.get('op')=='propose' else AUTOMATION_AUDITION)|({'device'} if devices else set())
    raise ValueError('No capability requirements are declared for action '+str(kind))

def check_plan(adapter,actions,replacing_generation=False):
    needed=set()
    for action in actions:needed|=for_action(action,replacing_generation)
    daw_adapter.require(adapter,needed,'run this plan')
    return needed
