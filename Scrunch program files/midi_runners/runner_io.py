"""Shared plumbing for model runners. Each runner runs in its model's own venv (data/midi-models/venvs).

Protocol (midi_bench.CommandCandidate): the request JSON arrives on stdin; the runner prints one JSON
object on stdout with `midi_base64`, `timebase` ('beats' or 'seconds') and `meta`. Everything else
goes to stderr. Model downloads (text encoders) are cached inside data/midi-models/hf-cache, never
in the user's global cache, so removing that folder removes them.
"""
import json, os, random, sys, time
from pathlib import Path

MODELS = Path(os.environ.get('SCRUNCH_MIDI_MODELS', str(Path(__file__).resolve().parent.parent / 'data' / 'midi-models')))
os.environ.setdefault('HF_HOME', str(MODELS / 'hf-cache'))
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK', '1')


_REPLY = sys.stdout


def request():
    """Read the request, then route every later print (model code prints freely) to stderr so that
    stdout carries only the JSON reply."""
    req = json.load(sys.stdin)
    sys.stdout = sys.stderr
    return req


def seed(value):
    import numpy, torch
    random.seed(value); numpy.random.seed(value % (2 ** 32)); torch.manual_seed(value)


def device():
    import torch
    forced = os.environ.get('SCRUNCH_MIDI_DEVICE')
    if forced:
        return forced
    return 'mps' if torch.backends.mps.is_available() else 'cpu'


def token_budget(req, default=1024, cap=2048):
    """Tokens to generate: `tokensPerBar` x requested bars (+64 slack) when configured, else maxTokens.
    Generation cost grows with length, and full-song models run past the bars asked for."""
    per_bar = req.get('tokensPerBar')
    if per_bar:
        return int(min(cap, req['bars'] * float(per_bar) + 64))
    return int(req.get('maxTokens') or default)


def emit(midi_bytes, timebase, meta):
    import base64
    out = {'midi_base64': base64.b64encode(midi_bytes).decode(), 'timebase': timebase, 'meta': meta}
    _REPLY.write(json.dumps(out)); _REPLY.flush()


class Timer:
    def __init__(self):
        self.t = time.perf_counter(); self.marks = {}

    def mark(self, name):
        now = time.perf_counter(); self.marks[name] = round(now - self.t, 3); self.t = now


def log(*args):
    print(*args, file=sys.stderr, flush=True)
