"""CC contract and Arturia configuration inspection; delivery is not enabled."""
from pathlib import Path
import hashlib,xml.etree.ElementTree as ET

def encode_cc(channel,controller,value):
    for name,v,lo,hi in [('channel',channel,1,16),('controller',controller,0,119),('value',value,0,127)]:
        if type(v)!=int or not lo<=v<=hi:raise ValueError(f'{name} outside supported range {lo}..{hi}')
    return bytes([0xB0+channel-1,controller,value])

def arturia_configs(path):
    raw=Path(path).read_bytes();root=ET.fromstring(raw)
    return {'source':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'activeInstanceVerified':False,
       'configs':[{'name':c.get('name'),'assignments':[dict(a.attrib) for a in c.findall('assignment')]} for c in root.findall('midiconfig')]}

def capability():
    return {'transport':'midi-cc','encodingImplemented':True,'deliveryEnabled':False,
      'assignmentScope':'per-instance/per-config; never assumed universal',
      'prerequisites':['verified destination and isolated track routing','verified active assignment and MIDI channel','previous value/readback and restoration'],
      'limitations':['Producer Pal exposes no raw CC-send operation in the inspected interface.','CoreMIDI probe found no destinations.','Arturia factory XML is not active-instance readback.','Automatic MIDI Learn/UI or configuration import has not been verified.']}

def send_cc(*args,**kwargs):
    raise ValueError('MIDI CC delivery is not verified; no message sent.')
