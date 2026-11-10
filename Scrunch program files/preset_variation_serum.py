"""Selected Serum preset -> RAM-only branching, using the existing owned host.

No library search, reference reconstruction, proxy ranking, provider call, Live
operation or saved audio. The caller selects the file and supplies the actual MIDI.
"""
import copy
import hashlib
import math
import struct
from pathlib import Path

from preset_variations import Session,VariationError,validate_phrase


def host_phrase(phrase):
    """Match ControlMap's actual bounded format before launching the plug-in."""
    import serum_design as sd
    phrase=validate_phrase(phrase);rate=phrase['sample_rate']
    maximum=int(rate*30)-int(sd.TAIL_S*rate)-4096
    if phrase['musical_frames']>maximum:
        raise VariationError(f'This Serum renderer accepts up to {maximum/rate:.2f} seconds of MIDI plus its tail; no notes were shortened.')
    for key,maximum_value in (('block_size',4096),('flush_frames',rate*10)):
        if type(phrase.get(key)) is not int or not 0<phrase[key]<=maximum_value:
            raise VariationError('Phrase '+key+' is outside the actual host range.')
    if any(type(phrase.get(key)) is not int or phrase[key]<=0 for key in ('numerator','denominator')):
        raise VariationError('Supply a valid phrase time signature.')
    if type(phrase.get('ppq_origin')) not in (int,float) or not math.isfinite(phrase['ppq_origin']):
        raise VariationError('Supply a finite phrase beat origin.')
    return phrase


def architecture(doc):
    """Stored scalar settings and routes; omit waveform/sample blobs and arrays.

    This is read-only source evidence, not a new structural control map. Published
    parameters remain the executable catalogue; routing changes are not guessed.
    """
    def scalar_tree(value):
        if isinstance(value,dict):
            return {str(k):scalar_tree(v) for k,v in value.items() if isinstance(v,(dict,str,int,float,bool)) or v is None}
        return copy.deepcopy(value)
    fields={k:scalar_tree(v) for k,v in doc.items()
            if isinstance(v,dict) and (k.startswith(('Oscillator','ModSlot','LFO','Env','Filter','Macro')))}
    # FX chains are lists: preserve their scalar settings explicitly.
    for key in ('FXRack0','FXRack1','FXRack2'):
        rack=doc.get(key,{})
        if isinstance(rack,dict):
            fields[key]={**scalar_tree(rack),'FX':[scalar_tree(v) for v in rack.get('FX',[]) if isinstance(v,dict)]}
    return {'basis':'Decoded settings from the caller-selected preset, without inferred audibility.',
            'storedModules':fields,'limits':['Embedded sample/wavetable arrays omitted.',
            'Asset availability and active modulation are not certified by stored settings.',
            'This variation executor currently edits published controls, not structural routing.']}


class SerumSession(Session):
    def _render(self,state,changes):
        host_phrase(self.phrase)
        return super()._render(state,changes)

    def context(self,words):
        import serum_design as sd
        return {**super().context(words),'patchArchitecture':architecture(sd.patch_doc(self.source)),
                'sourceFileSha256':getattr(self,'source_file_hash',None)}

    def branch(self,batch_id,name):
        child=super().branch(batch_id,name)
        child.source_file_hash=getattr(self,'source_file_hash',None)
        return child

    def source_preset_bytes(self):
        """The current starting sound (the favourite, or the take developed from it) as preset bytes; never written to disk."""
        import serum_design as sd
        if not self.lock.acquire(blocking=False):raise VariationError('Wait for the active render before exporting.')
        try:
            name=self.source_label.split(' / ')[-1]
            return sd.state_preset(self.source,name,description='Starting sound in Scrunch: '+self.source_label)
        finally:self.lock.release()

    def preset_bytes(self,batch_id,name):
        """Explicit export request only. Return bytes; never write a preset file."""
        import serum_design as sd
        if not self.lock.acquire(blocking=False):raise VariationError('Wait for the active render before exporting.')
        try:
            if self.batch is None or self.batch['id']!=batch_id:raise VariationError('This listening batch is no longer current.')
            row=next((v for v in self.batch['variants'] if v['name']==name),None)
            if row is None:raise VariationError('Unknown variation to export.')
            return sd.state_preset(row['state'],name,description='Variation of '+self.source_label)
        finally:self.lock.release()


def checked_state(raw):
    """Bound declared decompression before the codec allocates its buffer."""
    import serum_design as sd
    if len(raw)<25 or not raw.startswith(b'XferJson\0'):raise VariationError('This is not a complete Serum 2 preset.')
    header_length=struct.unpack('<Q',raw[9:17])[0]
    if header_length>65536 or 25+header_length>=len(raw):raise VariationError('The Serum preset header is incomplete or too large.')
    size=struct.unpack('<I',raw[17+header_length:21+header_length])[0]
    if not 0<size<=64*1024*1024:raise VariationError('This preview accepts presets expanding to at most 64 MB; nothing was decoded.')
    try:return sd.preset_state_bytes(raw)
    except (ValueError,KeyError,IndexError,struct.error,UnicodeError) as error:
        raise VariationError('The selected Serum preset could not be decoded: '+str(error)) from None


def selected_preset(path,phrase,*,locked_controls=(),meter=None):
    import serum_design as sd
    phrase=host_phrase(phrase)
    if not isinstance(path,(str,Path)):raise VariationError('Select an existing Serum 2 preset file.')
    path=Path(path)
    if path.suffix!='.SerumPreset' or not path.is_file():raise VariationError('Select an existing Serum 2 preset file.')
    if path.stat().st_size>200*1024*1024:raise VariationError('Selected preset exceeds the 200 MB input limit.')
    raw=path.read_bytes();fingerprint=hashlib.sha256(raw).hexdigest()
    state=checked_state(raw)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=fingerprint:
        raise VariationError('Selected preset changed while it was being loaded.')
    result=SerumSession(state,phrase,sd.render,source_label=path.stem,locked_controls=locked_controls,meter=meter)
    result.source_file_hash=fingerprint
    return result


def selected_bytes(raw,name,phrase,*,locked_controls=(),meter=None):
    import serum_design as sd
    phrase=host_phrase(phrase)
    if not isinstance(raw,bytes) or not 0<len(raw)<=8*1024*1024:
        raise VariationError('Choose a Serum 2 preset of up to 8 MB for this preview.')
    if not isinstance(name,str) or len(name)>500 or not name.endswith('.SerumPreset'):
        raise VariationError('Choose a Serum 2 .SerumPreset file.')
    state=checked_state(raw)
    result=SerumSession(state,phrase,sd.render,source_label=Path(name).stem,locked_controls=locked_controls,meter=meter)
    result.source_file_hash=hashlib.sha256(raw).hexdigest()
    return result
