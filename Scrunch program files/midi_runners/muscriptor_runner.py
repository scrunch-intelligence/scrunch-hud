"""MuScriptor large (0.3.0, Kyutai/Mirelo, CC-BY-NC) audio-to-MIDI runner. John chose it by ear on
26 Sep 2026 over MuScriptor medium, YourMT3+, Basic Pitch and Klangio (evidence/audio-to-midi-decision-2026-09-27.md).

Request: {"audio": path, "startSeconds": float, "endSeconds": float}. The span is cut from the file
and transcribed with the library defaults (greedy decoding, prelude forcing on, best-effort tempo).
The weights and the beat-this tempo checkpoint are local files under data/midi-models, so nothing is
downloaded and no Hugging Face login is needed.

Reply: the model's MIDI untouched (`midi_base64`, timebase 'seconds') plus `notes` read from it in
seconds from the start of the span, one list per part the model wrote. Merging is Scrunch's job.
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_io as io

os.environ.setdefault('TORCH_HOME', str(io.MODELS / 'torch'))
os.environ.setdefault('HF_HUB_OFFLINE', '1')
WEIGHTS = io.MODELS / 'checkpoints' / 'muscriptor-large' / 'model.safetensors'


def main():
    req = io.request(); t = io.Timer()
    import pretty_midi, soundfile
    from muscriptor.transcription_model import TranscriptionModel
    model = TranscriptionModel.load_model(weights_path=WEIGHTS)
    t.mark('loadSeconds')
    info = soundfile.info(req['audio'])
    start = max(0.0, float(req.get('startSeconds') or 0.0))
    end = min(info.duration, float(req.get('endSeconds') or info.duration))
    if not end > start:
        raise ValueError('The audio span is empty (%.3f-%.3f s of a %.3f s file).' % (start, end, info.duration))
    audio, sr = soundfile.read(req['audio'], start=int(start * info.samplerate), stop=int(end * info.samplerate),
                               always_2d=True)
    with tempfile.TemporaryDirectory() as d:
        span = Path(d) / 'span.wav'
        soundfile.write(str(span), audio, sr, subtype='FLOAT')
        data = model.transcribe_to_midi(audio=span)
    t.mark('transcribeSeconds')
    import io as bytes_io
    pm = pretty_midi.PrettyMIDI(bytes_io.BytesIO(data))
    parts = [{'name': i.name, 'program': int(i.program), 'drum': bool(i.is_drum),
              'notes': [{'p': int(n.pitch), 's': float(n.start), 'e': float(n.end), 'v': int(n.velocity)} for n in i.notes]}
             for i in pm.instruments]
    io.emit(data, 'seconds', {'model': 'MuScriptor 0.3.0 large', 'device': str(model._device),
                              'spanSeconds': [start, end], 'parts': parts, **t.marks})


if __name__ == '__main__':
    main()
