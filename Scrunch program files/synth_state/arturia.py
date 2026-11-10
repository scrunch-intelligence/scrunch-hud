"""Arturia (Pigments, Jup-8000 V) component state: a Boost text archive whose first map holds every
internal value by name ("<len> <name> <number>"). That map is editable; everything else is kept verbatim."""
import re


class ArturiaCodec:
    def __init__(self, data):
        self.text = data.decode('latin-1')
        m = re.search(r' (\d{2,6}) 0 0 0 (\d+) ([A-Za-z_])', self.text)
        if not m or not self.text.startswith('22 serialization::archive'):
            raise ValueError('not an Arturia state archive')
        count = int(m.group(1))
        i = m.start(2)
        self.head = self.text[:i]
        self.items = []                        # [name, value text]
        for _ in range(count):
            sp = self.text.index(' ', i); n = int(self.text[i:sp])
            name = self.text[sp + 1:sp + 1 + n]; i = sp + 2 + n
            sp = self.text.index(' ', i); value = self.text[i:sp]; i = sp + 1
            if not re.fullmatch(r'-?\d+(\.\d+)?(e[+-]?\d+)?', value):
                raise ValueError('unexpected value %r for %s' % (value, name))
            self.items.append([name, value])
        self.tail = self.text[i:]
        self.index = {name: k for k, (name, _) in enumerate(self.items)}

    def values(self):
        return {name: (float(v) if ('.' in v or 'e' in v) else int(v)) for name, v in self.items}

    def set(self, path, value):
        if path not in self.index:
            raise KeyError(path)
        k = self.index[path]
        old = self.items[k][1]
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int) or (float(value).is_integer() and '.' not in old and 'e' not in old):
            text = str(int(value))
        else:
            text = '%.8g' % float(value)
            if 'e' not in text and '.' not in text:
                text += '.0'
        self.items[k][1] = text

    def dump(self):
        body = ''.join('%d %s %s ' % (len(name), name, value) for name, value in self.items)
        return (self.head + body + self.tail).encode('latin-1')
