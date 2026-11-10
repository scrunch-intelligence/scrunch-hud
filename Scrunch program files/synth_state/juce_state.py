"""JUCE's hosted-VST3 state blob: copyXmlToBinary(<VST3PluginState><IComponent>..</IComponent>
<IEditController>..</IEditController></VST3PluginState>) with JUCE's own base64 variant."""
import re, struct

_B64 = '.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+'
_INDEX = {c: i for i, c in enumerate(_B64)}


def decode64(text):
    """JUCE MemoryBlock::fromBase64Encoding: 6-bit groups, least significant first; 4 chars = 3 bytes."""
    size, _, body = text.partition('.')
    size = int(size)
    out = bytearray()
    idx = _INDEX
    for i in range(0, len(body), 4):
        chunk = body[i:i + 4]
        v = 0
        for j, ch in enumerate(chunk):
            v |= idx[ch] << (6 * j)
        out += v.to_bytes(3, 'little')
    return bytes(out[:size])


def encode64(data):
    out = []
    b64 = _B64
    nchars = (len(data) * 8 + 5) // 6
    padded = data + b'\0' * (-len(data) % 3)
    for i in range(0, len(padded), 3):
        v = padded[i] | (padded[i + 1] << 8) | (padded[i + 2] << 16)
        out.append(b64[v & 63] + b64[(v >> 6) & 63] + b64[(v >> 12) & 63] + b64[(v >> 18) & 63])
    return f'{len(data)}.' + ''.join(out)[:nchars]


def parse(blob):
    """(parts, xml): the component/controller bytes and the XML they came in (kept for exact rebuilding)."""
    if blob[:4] != b'VC2!':
        raise ValueError('not a JUCE state blob')
    n = struct.unpack('<I', blob[4:8])[0]
    xml = blob[8:8 + n].rstrip(b'\0').decode('utf-8')
    return {tag: decode64(body) for tag, body in re.findall(r'<(IComponent|IEditController)>([^<]*)</\1>', xml)}, xml


def build(parts, xml=None):
    if xml is None:
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n\n<VST3PluginState>' + ''.join(
            '<%s></%s>' % (t, t) for t in ('IComponent', 'IEditController') if t in parts) + '</VST3PluginState>'
    for tag, data in parts.items():
        xml = re.sub(r'<%s>[^<]*</%s>' % (tag, tag), lambda m: '<%s>%s</%s>' % (tag, encode64(data), tag), xml)
    body = xml.encode()
    return struct.pack('<II', 0x21324356, len(body)) + body + b'\0'   # JUCE counts the text without its null
