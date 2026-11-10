"""Minimal CBOR (RFC 8949) codec that re-encodes exactly what it decoded (definite lengths, same widths).

Floats keep their encoded width (Float32/Float64 wrappers) and integers their head size, so an unchanged
structure re-encodes byte-identically. Only the major types Serum 2 uses are supported.
"""
import struct


class Float32(float):
    width = 4


class Float64(float):
    width = 8


class Float16(float):
    width = 2


class Int(int):
    """An integer that remembers its encoded head length (for byte-identical re-encoding)."""
    head = None


def _head(data, pos):
    ib = data[pos]; major, info = ib >> 5, ib & 31; pos += 1
    if info < 24:
        return major, info, pos, 0
    n = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
    if n is None:
        raise ValueError('unsupported CBOR length form %d' % info)
    return major, int.from_bytes(data[pos:pos + n], 'big'), pos + n, n


def loads(data):
    value, pos = _decode(data, 0)
    if pos != len(data):
        raise ValueError('trailing CBOR data')
    return value


def _decode(data, pos):
    ib = data[pos]
    major, info = ib >> 5, ib & 31
    if major == 7:
        if info == 20: return False, pos + 1
        if info == 21: return True, pos + 1
        if info == 22: return None, pos + 1
        if info == 25: return Float16(struct.unpack('>e', data[pos + 1:pos + 3])[0]), pos + 3
        if info == 26: return Float32(struct.unpack('>f', data[pos + 1:pos + 5])[0]), pos + 5
        if info == 27: return Float64(struct.unpack('>d', data[pos + 1:pos + 9])[0]), pos + 9
        raise ValueError('unsupported CBOR simple value %d' % info)
    major, arg, pos, n = _head(data, pos)
    if major in (0, 1):
        v = Int(arg if major == 0 else -1 - arg); v.head = n; return v, pos
    if major == 2:
        return bytes(data[pos:pos + arg]), pos + arg
    if major == 3:
        return data[pos:pos + arg].decode('utf-8'), pos + arg
    if major == 4:
        out = []
        for _ in range(arg):
            v, pos = _decode(data, pos); out.append(v)
        return out, pos
    if major == 5:
        out = {}
        for _ in range(arg):
            k, pos = _decode(data, pos); v, pos = _decode(data, pos); out[k] = v
        return out, pos
    raise ValueError('unsupported CBOR major type %d' % major)


def _enc_head(major, arg, n=None):
    if n is None:
        n = 0 if arg < 24 else 1 if arg < 256 else 2 if arg < 65536 else 4 if arg < 2 ** 32 else 8
    if n == 0:
        return bytes([(major << 5) | arg])
    return bytes([(major << 5) | {1: 24, 2: 25, 4: 26, 8: 27}[n]]) + arg.to_bytes(n, 'big')


def dumps(value):
    out = bytearray(); _encode(value, out); return bytes(out)


def _encode(v, out):
    if v is False: out.append(0xf4)
    elif v is True: out.append(0xf5)
    elif v is None: out.append(0xf6)
    elif isinstance(v, float):
        w = getattr(v, 'width', 8)
        head, fmt = {2: (b'\xf9', '>e'), 4: (b'\xfa', '>f'), 8: (b'\xfb', '>d')}[w]     # only the stored width is packed
        out += head + struct.pack(fmt, v)
    elif isinstance(v, int):
        n = getattr(v, 'head', None)
        out += _enc_head(0, v, n) if v >= 0 else _enc_head(1, -1 - v, n)
    elif isinstance(v, bytes): out += _enc_head(2, len(v)) + v
    elif isinstance(v, str):
        b = v.encode('utf-8'); out += _enc_head(3, len(b)) + b
    elif isinstance(v, list):
        out += _enc_head(4, len(v))
        for x in v: _encode(x, out)
    elif isinstance(v, dict):
        out += _enc_head(5, len(v))
        for k, x in v.items(): _encode(k, out); _encode(x, out)
    else:
        raise TypeError('cannot encode %r' % type(v))
