"""Dream phone server: the only Scrunch surface a phone reaches. Run: python3 dream_server.py (port 3352).

Binds 127.0.0.1 only. Phones reach it through `tailscale serve` (HTTPS with a real ts.net certificate, which
iPhone Safari needs for the microphone), so only devices on John's tailnet get here, and every API call also
needs the Dream key (data/dream/key; the phone link carries it once and the app remembers it). It can save
and transcribe takes and mark them sent; it cannot touch Live or any other Scrunch route.
"""
import json, mimetypes, os, re, secrets
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import dream

PORT = int(os.environ.get('DREAM_PORT', '3352'))
DIST = Path(__file__).resolve().parent / 'dist' / 'dream'
FILES = {'/': 'index.html', '/app.js': 'app.js', '/synth.js': 'synth.js', '/style.css': 'style.css',
         '/manifest.webmanifest': 'manifest.webmanifest', '/sw.js': 'sw.js', '/icon.svg': 'icon.svg'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, status, body, ctype='application/json', extra=None):
        raw = json.dumps(body).encode() if ctype == 'application/json' else body
        self.send_response(status); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(raw)))
        if 'Cache-Control' not in (extra or {}): self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(raw)

    def host_ok(self):
        h = (self.headers.get('Host') or '').lower()
        return h in ('127.0.0.1:%d' % PORT, 'localhost:%d' % PORT) or bool(re.fullmatch(r'[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net(:\d+)?', h))

    def key_ok(self):
        return secrets.compare_digest(self.headers.get('X-Dream-Key', ''), dream.key())

    def do_GET(self):
        if not self.host_ok(): return self.send(403, {'error': 'Open Dream through its Tailscale address.'})
        u = urlparse(self.path); path = u.path
        if path in FILES:
            f = DIST / FILES[path]
            return self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or ('application/manifest+json' if f.suffix == '.webmanifest' else 'text/plain'))
        # instrument samples: Salamander Grand (CC-BY 3.0, Alexander Holm) and MusyngKite GM (midi-js-soundfonts)
        m = re.fullmatch(r'/(piano|sounds/[a-z0-9_]+)/([A-G](?:s|b)?[0-8])\.mp3', path)
        if m:
            f = dream.ROOT / 'data/dream' / m.group(1) / (m.group(2) + '.mp3')
            if f.is_file(): return self.send(200, f.read_bytes(), 'audio/mpeg', {'Cache-Control': 'max-age=31536000'})
            return self.send(404, {'error': 'No such sample'})
        if not self.key_ok(): return self.send(401, {'error': 'This phone is not paired. Open the Dream link John sent.'})
        try:
            if path == '/api/takes': return self.send(200, {'takes': dream.takes()})
            m = re.fullmatch(r'/api/takes/([^/]+)(/audio|/midi)?', path)
            if m:
                t = dream.load(m.group(1))
                if m.group(2) == '/audio':
                    return self.send(200, (dream.INBOX / t['id'] / 'take.wav').read_bytes(), 'audio/wav')
                if m.group(2) == '/midi':
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    data = dream.midi_file(t, q.get('mode', 'pulse'), int(q.get('octave', 0)), q.get('quantize', ''), float(q['bpm']) if q.get('bpm') else None, q.get('snap') == '1')
                    return self.send(200, data, 'audio/midi', {'Content-Disposition': 'attachment; filename="%s.mid"' % re.sub(r'[^\w -]', '', t['name'])})
                return self.send(200, t)
            return self.send(404, {'error': 'Not found'})
        except (ValueError, KeyError) as e:
            return self.send(400, {'error': str(e)[:300]})

    def do_POST(self):
        if not self.host_ok() or not self.key_ok(): return self.send(401, {'error': 'This phone is not paired.'})
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if n > dream.MAX_BYTES: return self.send(413, {'error': 'Recording too long.'})
            self.connection.settimeout(60)
            body = self.rfile.read(n)
            if path == '/api/takes':
                click = self.headers.get('X-Click-Bpm')
                t = dream.ingest(body, self.headers.get('Content-Type', ''), unquote(self.headers.get('X-Take-Name', '')),
                                 float(click) if click else None, unquote(self.headers.get('X-Who', '')))
                return self.send(200, t)
            b = json.loads(body or b'{}')
            if path == '/api/combine':
                return self.send(200, dream.combine([str(i) for i in b.get('ids', [])][:12], b.get('name', '')))
            m = re.fullmatch(r'/api/takes/([^/]+)/(send|rename|delete)', path)
            if not m: return self.send(404, {'error': 'Not found'})
            tid, act = m.groups()
            if act == 'delete' and dream.load(tid)['status'] != 'phone':
                raise ValueError('Already sent to Scrunch; John can remove it there.')
            r = {'send': lambda: dream.send(tid, int(b.get('octave', 0)), b.get('quantize', ''), b.get('note', ''), bool(b.get('snap'))), 'rename': lambda: dream.rename(tid, b.get('name', '')), 'delete': lambda: dream.remove(tid)}[act]()
            return self.send(200, r)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            return self.send(400, {'error': str(e)[:300]})
        except Exception as e:
            return self.send(500, {'error': 'Dream could not finish: ' + type(e).__name__ + ': ' + str(e)[:200]})


if __name__ == '__main__':
    dream.key()
    print('Dream phone server on 127.0.0.1:%d' % PORT, flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
