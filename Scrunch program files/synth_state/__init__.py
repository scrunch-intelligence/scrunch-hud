"""Synth patch state beyond published parameters (brief red-team point 5).

`open_state(synth, blob)` takes the state blob Scrunch Host returns (JUCE's VST3PluginState: component +
controller data) and gives a `PatchState`: every stored value under a readable path, editable, and written
back byte-identically where nothing changed. Codecs: Diva (u-he text preset), Serum 2 (XferJson + zstd CBOR),
Pigments / Jup-8000 V (Arturia text archive). Unknown synths raise ValueError.
"""
from . import juce_state
from .diva import DivaCodec
from .serum2 import SerumCodec
from .arturia import ArturiaCodec

CODECS = {'Diva': DivaCodec, 'Serum 2': SerumCodec, 'Serum2': SerumCodec, 'Pigments': ArturiaCodec, 'Jup-8000 V': ArturiaCodec}


class PatchState:
    def __init__(self, synth, blob):
        codec = CODECS.get(synth)
        if codec is None:
            raise ValueError('No patch-state codec for %s.' % synth)
        self.synth = synth
        self.parts, self._xml = juce_state.parse(blob)    # {'IComponent': bytes, 'IEditController': bytes}
        self.docs = {k: codec(v) for k, v in self.parts.items() if v}
        self.main = self.docs['IComponent']

    def values(self):
        """{path: value} of every editable stored value in the component (processor) state."""
        return self.main.values()

    def set(self, path, value):
        """Change one stored value. The controller copy is kept in step where it holds the same value."""
        self.main.set(path, value)
        other = self.docs.get('IEditController')
        if other is not None and other is not self.main and path in other.values():
            other.set(path, value)

    def blob(self):
        return juce_state.build({k: d.dump() for k, d in self.docs.items()}, self._xml)


def open_state(synth, blob):
    return PatchState(synth, blob)
