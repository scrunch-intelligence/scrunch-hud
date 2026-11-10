"""Caller-selected preset and single-part MIDI bytes, without disk imports.

Reject rather than guess unsupported performance events or truncate a passage.
"""
import base64
import binascii
import math

import midi_file
from preset_variations import VariationError
from preset_variation_serum import host_phrase


def decode(value,maximum,label):
    if not isinstance(value,str) or not value or len(value)>4*((maximum+2)//3):
        raise VariationError(label+' is empty or exceeds the preview input limit.')
    try:raw=base64.b64decode(value,validate=True)
    except (ValueError,binascii.Error):raise VariationError(label+' could not be read.') from None
    if not 0<len(raw)<=maximum:raise VariationError(label+' exceeds the preview input limit.')
    return raw


def phrase(raw):
    """Preserve timing, velocities and initial silence; accept one note part."""
    if not isinstance(raw,bytes) or not 0<len(raw)<=256*1024:raise VariationError('Choose MIDI of up to 256 KB.')
    try:
        if sum(8+len(body) for _,body in midi_file._chunks(raw))!=len(raw):raise VariationError('The MIDI has a truncated or trailing chunk.')
        parsed=midi_file.read(raw)
        if parsed['format'] not in (0,1):raise VariationError('Export a simultaneous MIDI phrase, not format 2.')
        if parsed['declaredTracks']!=sum(kind==b'MTrk' for kind,_ in midi_file._chunks(raw)):
            raise VariationError('The MIDI track count is incomplete.')
        if len(parsed['parts'])!=1:raise VariationError('Export only the MIDI part you want this preset to play.')
        for kind,body in midi_file._chunks(raw):
            if kind!=b'MTrk':continue
            active={}
            for tick,event,payload in midi_file._track_events(body,emit_sysex=True):
                if event=='sysex':raise VariationError('System-exclusive MIDI is not supported by this preview; nothing was dropped.')
                if event=='channel' and payload[0] not in (0x80,0x90):
                    raise VariationError('This preview supports notes and velocity. MIDI controllers, pitch bends and program changes need a separate performance import; nothing was dropped.')
                if event=='channel':
                    channel_kind,channel,args=payload
                    if any(v>127 for v in args):raise VariationError('Invalid MIDI note data.')
                    key=(channel,args[0]);on=channel_kind==0x90 and args[1]>0
                    if on:active[key]=active.get(key,0)+1
                    elif not active.get(key):raise VariationError('A MIDI note ends without a matching start.')
                    else:active[key]-=1
                if event=='meta' and payload[0] in (0x51,0x58) and tick!=0:
                    raise VariationError('Export a phrase with a constant tempo and time signature; nothing was flattened.')
            if any(active.values()):raise VariationError('Some MIDI notes have no note-off; export a complete phrase.')
        if parsed['tempoChanges'] or parsed['meterChanges']:raise VariationError('Export one constant tempo and time signature.')
        tempo=parsed['tempo'];rate=48000
        notes=[{'on':round(n['t']*60/tempo*rate),'off':round((n['t']+n['d'])*60/tempo*rate),
                'pitch':n['p'],'velocity':n['v']} for n in parsed['parts'][0]['notes']]
        beats=parsed['parts'][0]['length']
        result={'notes':notes,'tempo':tempo,'sample_rate':rate,'musical_frames':math.ceil(beats*60/tempo*rate),
                'block_size':512,'flush_frames':rate,'numerator':parsed['timeSignature'][0],
                'denominator':parsed['timeSignature'][1],'ppq_origin':0}
        return host_phrase(result)
    except (ZeroDivisionError,IndexError,KeyError,midi_file.MidiFileError) as error:
        raise VariationError('The selected MIDI could not be read: '+str(error)) from None
