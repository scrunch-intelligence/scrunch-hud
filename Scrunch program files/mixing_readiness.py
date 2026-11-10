"""Read-only readiness checks for a Mixing demo; no credentials or audio retained."""
import importlib
import math
import platform
import plistlib
from pathlib import Path


def installed_live_versions(applications=Path('/Applications')):
    versions=[]
    for app in Path(applications).glob('Ableton Live*.app'):
        try:
            with (app/'Contents/Info.plist').open('rb') as f:info=plistlib.load(f)
            version=info.get('CFBundleShortVersionString') or info.get('CFBundleVersion')
            if isinstance(version,str):versions.append({'application':app.name,'version':version})
        except (OSError,ValueError,plistlib.InvalidFileException):continue
    return sorted(versions,key=lambda row:(row['application'],row['version']))


def check(*,adapter=None,applications=Path('/Applications'),audio_probe=None):
    checks=[]
    def record(name,ready,detail):checks.append({'name':name,'ready':ready,'detail':detail})
    record('Python runtime',__import__('sys').version_info>=(3,12),platform.python_version())
    dependencies=True
    for name in ('numpy','scipy','soundfile','cffi'):
        try:
            module=importlib.import_module(name);record(name,True,getattr(module,'__version__','installed'))
        except ImportError:
            dependencies=False;record(name,False,'Missing runtime dependency.')
    if dependencies:
        try:
            if audio_probe is None:
                import numpy as np
                from reference_dsp import meter
                t=np.arange(3*48000)/48000
                measured=meter((.1*np.sin(2*np.pi*440*t))[:,None],48000)
            else:measured=audio_probe()
            ready=all(type(measured.get(k)) in (int,float) and math.isfinite(measured[k]) for k in ('integratedLUFS','truePeakDbTP'))
            record('Local audio meter',ready,'Three-second RAM-only meter check.' if ready else 'Meter did not return finite loudness and true peak.')
        except Exception as exc:
            record('Local audio meter',False,'Meter unavailable: '+type(exc).__name__)
    else:record('Local audio meter',False,'Install the missing dependencies before testing the meter.')
    audio_ready=all(row['ready'] for row in checks)
    live={'checked':adapter is not None,'connected':False,'basis':'Live compatibility is unverified. An installed version is not connection or correction proof.'}
    if adapter is not None:
        try:
            connection=adapter.connect();transport=adapter.transport()
            live.update(connected=True,sessionName=connection.get('name'),tempo=connection.get('tempo'),
                        timeSignature=connection.get('timeSignature'),playing=transport.get('playing'),
                        basis='Read-only connection and transport succeeded. Routing, capture, correction and version compatibility remain unverified.')
        except Exception as exc:live['error']='Connection unavailable: '+type(exc).__name__
    root=Path(__file__).resolve().parent;models=root/'data/midi-models'
    venv=models/'venvs/muscriptor/bin/python'
    midi={'audioToMidi':venv.exists() and (models/'checkpoints/muscriptor-large/model.safetensors').exists()
                         and (models/'torch/hub/checkpoints/beat_this-final0.ckpt').exists(),
          'humToMidi':(root/'.venv-scrunch/bin/python').exists() or venv.exists(),
          'basis':'Model files present. A transcription has not been run by this check.'}
    record('Audio clip to MIDI',midi['audioToMidi'],'MuScriptor large is installed.' if midi['audioToMidi'] else
           'Not installed: run "1 Set up Scrunch (once)" again and follow the Audio-to-MIDI step.')
    record('Hum to MIDI',midi['humToMidi'],'Humming transcriber is installed.' if midi['humToMidi'] else
           'Not installed: run "1 Set up Scrunch (once)" again.')
    return {'platform':platform.system(),'architecture':platform.machine(),'checks':checks,'midi':midi,
            'localAnalysisReady':audio_ready,'installedLive':installed_live_versions(applications),
            'live':live,'audioStored':False,'providerCalls':0,
            'scope':'Runtime readiness only. No musical result, model conversation, installation or Ableton correction is certified.'}
