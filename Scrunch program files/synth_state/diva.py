"""Diva (u-he) component state: a 4-byte length, then a text preset. Values live in "#cm=<section>" blocks
as "key=value" lines; the trailing compressed binary section is kept verbatim. Paths are "<section>:<key>"."""
import re, struct


class DivaCodec:
    def __init__(self, data):
        n = struct.unpack('<I', data[:4])[0]
        self.rest = data[4 + n:]
        self.lines = data[4:4 + n].decode('latin-1').split('\n')
        self.index = {}
        section = ''
        for k, line in enumerate(self.lines):
            if line.startswith('#cm='):
                section = line[4:]
            elif line.startswith('$$$$') or line.startswith('// Section for ugly'):
                break
            elif '=' in line and section and not line.startswith(('#', '/', '*')):
                key = line.split('=', 1)[0]
                self.index['%s:%s' % (section, key)] = k
        if not self.lines or not self.lines[0].startswith('#pgm='):
            raise ValueError('not a Diva state')

    @staticmethod
    def _value(text):
        try:
            return int(text) if re.fullmatch(r'-?\d+', text) else float(text)
        except ValueError:
            return text

    def values(self):
        return {path: self._value(self.lines[k].split('=', 1)[1]) for path, k in self.index.items()}

    def set(self, path, value):
        k = self.index[path]
        key, old = self.lines[k].split('=', 1)
        if isinstance(value, str):
            text = value
        elif isinstance(value, int) or (float(value).is_integer() and re.fullmatch(r'-?\d+', old)):
            text = str(int(value))
        else:
            places = len(old.split('.')[1]) if '.' in old else 2
            text = '%.*f' % (places, float(value))
        self.lines[k] = '%s=%s' % (key, text)

    def dump(self):
        body = '\n'.join(self.lines).encode('latin-1')
        return struct.pack('<I', len(body)) + body + self.rest
