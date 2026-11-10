"""Serum 2 state: "XferJson\\0" + u64 JSON length + JSON header, then u32 uncompressed size, u32 format and
a zstd frame holding CBOR. Paths are "/"-joined keys and list indices into that CBOR document
(e.g. "ModSlot0/plainParams/kParamAmount", "Oscillator0/WTOsc0/relativePathToWT")."""
import ctypes, ctypes.util, os, struct
from . import cbor

_LIB_PATHS = [os.environ.get('SCRUNCH_LIBZSTD', ''), ctypes.util.find_library('zstd') or '',
              os.path.expanduser('~/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib/libzstd.1.dylib'),
              '/Applications/RipX DAW.app/Contents/Frameworks/libzstd.1.dylib']
_lib = None


def _zstd():
    global _lib
    if _lib is None:
        for path in _LIB_PATHS:
            if path and os.path.exists(path):
                _lib = ctypes.CDLL(path); break
        if _lib is None:
            raise RuntimeError('libzstd not found (set SCRUNCH_LIBZSTD)')
        for f in ('ZSTD_decompress', 'ZSTD_compress', 'ZSTD_compressBound'):
            getattr(_lib, f).restype = ctypes.c_size_t
        _lib.ZSTD_isError.restype = ctypes.c_uint
    return _lib


def decompress(src, size):
    lib = _zstd(); out = ctypes.create_string_buffer(size)
    n = lib.ZSTD_decompress(out, size, src, len(src))
    if lib.ZSTD_isError(n): raise ValueError('zstd decompression failed')
    return out.raw[:n]


def compress(src, level=3):
    lib = _zstd(); cap = lib.ZSTD_compressBound(len(src)); out = ctypes.create_string_buffer(cap)
    n = lib.ZSTD_compress(out, cap, src, len(src), level)
    if lib.ZSTD_isError(n): raise ValueError('zstd compression failed')
    return out.raw[:n]


def _walk(node, prefix, out):
    if isinstance(node, dict):
        for k, v in node.items(): _walk(v, prefix + [str(k)], out)
    elif isinstance(node, list):
        for i, v in enumerate(node): _walk(v, prefix + [str(i)], out)
    else:
        out['/'.join(prefix)] = node


class SerumCodec:
    def __init__(self, data):
        if not data.startswith(b'XferJson\0'):
            raise ValueError('not a Serum 2 state')
        n = struct.unpack('<Q', data[9:17])[0]
        self.header = data[:17 + n]
        rest = data[17 + n:]
        self.size, self.fmt = struct.unpack('<II', rest[:8])
        self.frame = rest[8:]
        self.raw = decompress(self.frame, self.size)
        self.doc = cbor.loads(self.raw)
        self.changed = False

    def values(self):
        out = {}; _walk(self.doc, [], out); return out

    def set(self, path, value):
        keys = path.split('/'); node = self.doc
        for k in keys[:-1]:
            node = node[int(k)] if isinstance(node, list) else node[k]
        last = int(keys[-1]) if isinstance(node, list) else keys[-1]
        old = node[last]
        if isinstance(old, float) and not isinstance(value, str):
            value = type(old)(float(value))
        elif isinstance(old, int) and not isinstance(old, bool) and not isinstance(value, str):
            value = cbor.Int(int(value))
        node[last] = value
        self.changed = True

    def dump(self):
        if not self.changed:
            return self.header + struct.pack('<II', self.size, self.fmt) + self.frame
        raw = cbor.dumps(self.doc)
        return self.header + struct.pack('<II', len(raw), self.fmt) + compress(raw)
