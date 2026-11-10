"""Isolated phrase renderer and read-only capture of the current Diva source context."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import time

import numpy as np
from adaptive_sound import digest, save

HERE=Path(__file__).resolve().parent
HOST=HERE/'native/wrapper/build-adaptive/ScrunchHostTest_artefacts/Release/ScrunchHostTest'
PLUGIN=Path('/Library/Audio/Plug-Ins/VST3/Diva.vst3')


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_state(path):
    from synth_state import juce_state
    parts,xml=juce_state.parse(Path(path).read_bytes())
    for tag,raw in parts.items():
        n=struct.unpack('<I',raw[:4])[0]
        lines=raw[4:4+n].decode('latin1').splitlines(keepends=True)
        section=''; found=0
        for i,line in enumerate(lines):
            if line.startswith('#cm='): section=line.strip()[4:]
            if section=='PCore' and line.startswith('PProj='):
                lines[i]='PProj=0'+ ('\n' if line.endswith('\n') else '')
                found+=1
        if found!=1: raise ValueError('Unrecognised Diva load-counter framing')
        body=''.join(lines).encode('latin1')
        parts[tag]=struct.pack('<I',len(body))+body+raw[4+n:]
    return hashlib.sha256(juce_state.build(parts,xml)).hexdigest()


def fingerprints():
    preferences={}
    for name in ('com.u-he.Diva.Preferences.txt','com.u-he.Diva.midiassign.txt','com.u-he.Diva.plist'):
        p=Path.home()/'Library/Application Support/u-he'/name
        if not p.is_file(): raise ValueError('Diva preference fingerprint missing: '+str(p))
        preferences[name]=sha(p)
    return {'host_sha256':sha(HOST),'diva_binary_sha256':sha(PLUGIN/'Contents/MacOS/Diva'),
            'preferences':preferences,'source_sha256':{name:sha(HERE/name) for name in (
                'adaptive_sound.py','adaptive_gp.py','adaptive_render.py','brooklyn_rose.py',
                'adaptive_match.py','run_hidden_diva.py','warm_gp_engine.py','reference_dsp.py',
                'native/wrapper/src/SearchScope.hpp','native/wrapper/tests/ControlMap.cpp',
                'native/wrapper/third_party/JUCE/modules/juce_audio_processors_headless/format_types/juce_VST3PluginFormatImpl.h')}}


def phrase_from_clip(clip,region,tempo,signature):
    if clip.get('kind')!='midi' or clip.get('view')!='arrangement': raise ValueError('An actual Arrangement MIDI phrase is required')
    if not tempo>0 or signature!='4/4': raise ValueError('Unsupported phrase clock; no timing guessed')
    start,end=map(float,region)
    if not clip['start']<=start<end<=clip['start']+clip['span']: raise ValueError('Loop is not inside the source clip')
    loop_start=float(clip.get('loopStart') or 0); loop_end=float(clip.get('loopEnd') or clip['length'])
    length=loop_end-loop_start
    if length<=0: raise ValueError('Invalid clip loop')
    duration=(end-start)*60/tempo; rate=48000; notes=[]
    for note in clip['notes']:
        # Translate clip content through its offset and looping, preserving a cropped note-off.
        origin=float(clip['start'])-float(clip.get('offset') or 0)
        repeats=range(-1,int(np.ceil(clip['span']/length))+2) if clip.get('looping') else (0,)
        for repeat in repeats:
            on=origin+float(note['t'])+repeat*length
            off=on+float(note['d'])
            if start<=on<end:
                notes.append({'on':round((on-start)*60/tempo*rate),
                              'off':round((min(off,end)-start)*60/tempo*rate),
                              'pitch':int(note['p']),'velocity':int(note['v'])})
            elif on<start<off: raise ValueError('Phrase starts inside a sounding note; explicit preroll required')
    notes.sort(key=lambda n:(n['on'],n['pitch'],n['off']))
    if not notes: raise ValueError('The current phrase has no notes')
    return {'schema':1,'clip':clip,'region_beats':[start,end],'tempo':float(tempo),'numerator':4,'denominator':4,
            'ppq_origin':start,'sample_rate':rate,'block_size':512,'flush_frames':48000,
            'musical_frames':round(duration*rate),'tail_frames':96000,'frames':round(duration*rate)+96000,
            'notes':notes,'expression_fidelity':'basic note tuple snapshot; Live expression/automation not established'}


def capture_source(out):
    """Read-only APIs only. Do not use WrappedAdapter's potentially writing link handshake."""
    import wrapper_client as wc
    from daw_remote_script import RemoteScriptAdapter
    out=Path(out); out.mkdir(parents=True,exist_ok=True)
    a=RemoteScriptAdapter(set_name=lambda:'read-only Diva source')
    try:
        session=a.session(); selected=a.selected_track(); transport=a.transport()
        hosts=[(t,d) for t in session['tracks'] for d in a.devices(t['id']) if d.get('deviceName')=='Scrunch Host']
        records=[r for r in wc.instances() if r.get('child')=='Diva' and r.get('host')=='Ableton Live']
        if len(hosts)!=1 or len(records)!=1 or hosts[0][0]['id']!=selected:
            raise ValueError('Cannot identify a unique selected hosted Diva without mutation')
        track,device=hosts[0]; detail=a.track(track['id'],clips=True)
        loop=transport.get('loop')
        if not loop: raise ValueError('No explicit audition region')
        clips=[c for c in detail['clips'] if c['start']<=loop['start'] and c['start']+c['span']>=loop['end']]
        if len(clips)!=1: raise ValueError('Cannot identify one current MIDI clip')
        clip=a.clip(clips[0]['id'])
        phrase=phrase_from_clip(clip,(loop['start'],loop['end']),session['tempo'],session['timeSignature'])
        with wc.Wrapper(records[0]) as w:
            hello=w.hello(); lanes=w.lanes()
            if lanes: raise ValueError('Current wrapper automation must be captured, not discarded')
            blob=base64.b64decode(w.state()['data'])
            parameters=w.params()
        (out/'source.state').write_bytes(blob)
        context={'track':track,'device':device,'wrapper':hello,'transport':transport,'phrase':phrase,
                 'parameters':parameters,'captured_at':time.time(),'read_only':True}
        save(out/'context.json',context)
        return context
    finally: a.close()


class OfflineDriver:
    offline=True
    def __init__(self,root,source,phrase,scope=None,stop=None,case='hidden-1',split='evaluation',baseline_paths=None):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.source=Path(source).resolve(); self.phrase=phrase; self.stop=stop
        self.case,self.split=case,split; self.provenance=fingerprints(); self.children=set(); self.records=[]
        self.baseline_paths=baseline_paths or []
        self.canonical=canonical_state(self.source); self.restore_verified=True
        if scope is None:
            result,_=self._invoke({},'metadata',0,describe=True)
            self.scope=result['scope']
        else: self.scope=scope
        self.original={c['id']:c['original'] for c in self.scope['controls']}

    def _invoke(self,values,role,index,describe=False,scope_tests=False):
        out=self.root/'renders'/f'{role}-{index:03d}'
        out.mkdir(parents=True,exist_ok=False)
        p=self.phrase
        req={'plugin':str(PLUGIN),'state':str(self.source),'adaptive_search':True,'describe_only':describe,
             'sample_rate':p['sample_rate'],'block_size':p['block_size'],'frames':p['frames']+4096,
             'flush_frames':p['flush_frames'],'notes':p['notes'],'tempo':p['tempo'],
             'numerator':p['numerator'],'denominator':p['denominator'],'ppq_origin':p['ppq_origin'],
             'phrase_sha256':digest(p),'expected_canonical_sha256':self.canonical,
             'scope_tests':scope_tests,
             'changes':[{'id':i,'value':float(v)} for i,v in values.items()]}
        save(out/'request.json',req)
        policy='(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath '+json.dumps(str(out.resolve()))+'))'
        started=time.monotonic(); status='running'; code=None
        try:
            with (out/'stdout.log').open('wb') as stdout,(out/'stderr.log').open('wb') as stderr:
                proc=subprocess.Popen(['/usr/bin/sandbox-exec','-p',policy,str(HOST),'--control-map',str(out/'request.json'),str(out)],
                                      stdout=stdout,stderr=stderr,start_new_session=True)
                self.children.add(proc)
                try:
                    while proc.poll() is None:
                        if (self.stop and self.stop()) or time.monotonic()-started>45:
                            os.killpg(proc.pid,signal.SIGKILL); proc.wait()
                            raise RuntimeError('Render cancelled or exceeded 45 seconds')
                        time.sleep(.03)
                    code=proc.returncode
                    if code:
                        err=json.loads((out/'error.json').read_text()) if (out/'error.json').exists() else {}
                        raise RuntimeError('Diva renderer failed: '+str(err.get('error') or (out/'stderr.log').read_text()[-500:]))
                finally:
                    if proc.poll() is None: os.killpg(proc.pid,signal.SIGKILL); proc.wait()
                    self.children.discard(proc)
            result=json.loads((out/'result.json').read_text()); status='ok'
            return result,out
        except BaseException:
            status='failed'; self.restore_verified=False
            raise
        finally:
            supervisor={'status':status,'exit_code':code,'seconds':time.monotonic()-started,'reaped':not self.children,
                        'network_denied':True,'writes_only_render_directory':True}
            save(out/'supervisor.json',supervisor)
            record={'id':f'{self.case}/{role}-{index:03d}','role':role,'case':self.case,'split':self.split,
                    'method':'adaptive-gp','try':index,'request':str(out/'request.json'),'request_sha256':sha(out/'request.json'),
                    'base_state':{'file':str(self.source),'sha256':sha(self.source),'canonical_sha256':self.canonical},
                    'phrase_sha256':digest(self.phrase),'provenance':self.provenance,'supervisor':supervisor,
                    'parent':self.records[-1]['id'] if self.records else 'base-state:'+self.canonical}
            if (out/'result.json').exists(): record['result']=str(out/'result.json'); record['result_sha256']=sha(out/'result.json')
            if (out/'audio.f32').exists(): record['raw_pcm_sha256']=sha(out/'audio.f32')
            self.records.append(record)
            with (self.root/'manifest.jsonl').open('a') as f: f.write(json.dumps(record,sort_keys=True)+'\n')

    def render(self,values,role,index):
        if set(values)!=set(self.original): raise ValueError('Candidate must name the complete allowed coordinate vector')
        if role=='baseline' and index<len(self.baseline_paths):
            if any(abs(values[k]-self.original[k])>1e-7 for k in values): raise ValueError('Cached baseline has different controls')
            out=Path(self.baseline_paths[index]); result=json.loads((out/'result.json').read_text())
            if result['phrase_sha256']!=digest(self.phrase): raise ValueError('Cached baseline phrase mismatch')
        else: result,out=self._invoke(values,role,index)
        if not result.get('restored') or not result.get('locked_readback_ok'): raise ValueError('Native restore/locked readback failed')
        actual={p['id']:p['value'] for p in result['parameters_after'] if p.get('automatable')}
        raw=np.fromfile(out/'audio.f32',dtype='<f4').reshape(-1,2)
        latency=result['latency_samples']
        audio=raw[latency:latency+self.phrase['frames']]
        if len(audio)!=self.phrase['frames']: raise ValueError('Incomplete latency-aligned phrase')
        import soundfile as sf
        import reference_dsp
        if not (out/'audio.wav').exists(): sf.write(out/'audio.wav',audio,self.phrase['sample_rate'],subtype='FLOAT')
        measurement={'pcm_sha256':sha(out/'audio.f32'),'audio_sha256':sha(out/'audio.wav'),
                     'latency_removed_samples':latency,'dsp_code_sha256':sha(HERE/'reference_dsp.py'),
                     'scope_sha256':digest(result['scope']),'report':reference_dsp.analyse(reference_dsp.prepare(audio,self.phrase['sample_rate']))}
        if not (out/'measurement.json').exists(): save(out/'measurement.json',measurement)
        return audio,self.phrase['sample_rate'],{'values':actual,'audio':str(out/'audio.wav'),'record':str(out/'result.json')}

    def restore(self):
        # Every isolated render has already restored and read back its own original before exiting.
        self.restore_verified=False
        verified=True
        for record in self.records:
            out=self.root/'renders'/f'{record["role"]}-{record["try"]:03d}'
            if record['supervisor']['status']!='ok': verified=False
            if (out/'audio.f32').exists():
                path=record.get('result')
                if not path or not Path(path).is_file(): verified=False; continue
                result=json.loads(Path(path).read_text())
                verified=verified and result.get('restored',False) and result.get('canonical_restored_sha256')==self.canonical
        self.restore_verified=bool(verified)
        if not self.restore_verified: raise ValueError('An isolated instance did not verify restoration')

    def retain(self,values): raise ValueError('Offline adapter cannot alter Live')

    def cleanup(self):
        for proc in list(self.children):
            if proc.poll() is None: os.killpg(proc.pid,signal.SIGKILL)
            proc.wait(); self.children.discard(proc)
        result={'verified':self.restore_verified and not self.children,'reaped':not self.children,
                'restored':self.restore_verified,'audio_retained':True,'manifest':str(self.root/'manifest.jsonl')}
        save(self.root/'cleanup.json',result)
        return result


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['capture']); p.add_argument('output')
    args=p.parse_args(); c=capture_source(args.output)
    print(json.dumps({'track':c['track']['name'],'clip':c['phrase']['clip']['name'],
                      'notes':len(c['phrase']['notes']),'tempo':c['phrase']['tempo'],'read_only':True}))
