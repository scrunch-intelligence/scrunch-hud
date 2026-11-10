"""The producer's private MIDI reference library (master spec §38), behind a vetting gate.

Bulk-import .mid files or pasted MIDI, label each part by musical role, keep where it came from.
Everything lives under the local data folder (`data/` is not committed) and nothing is sent anywhere.

Vetting gate (John, 25 Sep 2026). Every import lands in **raw storage**: notes and provenance are
kept, but no analysis is run and nothing reads it for benchmarking, style profiling, copy indexes or
future fine-tuning. A file is promoted into the **active reference library** only by an explicit
approval of that file, or because it sits in a folder the producer has explicitly whitelisted.
Promotion runs the analysis; demotion or rejection removes it again. Every vetting action is kept in
an append-only log. Active-use reads (`parts()`, `reanalyse()`, copy indexes built from `parts()`)
see approved material only; raw or rejected material is visible only when asked for by status.

A file is stored once, by content hash, however often it is imported. Each track/channel in it
becomes its own part. Roles are labels the producer gives; they are never guessed from file names or
words, a label never changes what is measured, and labelling never promotes anything.
"""
import datetime, hashlib, json, os, sqlite3
from pathlib import Path

import midi_analysis, midi_file

DEFAULT_ROLES = ('bass', 'lead', 'arp', 'melody', 'chord', 'pad', 'percussion', 'drums',
                 'riff', 'acid', 'sequence')
STATUSES = ('raw', 'approved', 'rejected')
MAX_FILE_BYTES = 8 * 1024 * 1024
MIDI_SUFFIXES = ('.mid', '.midi', '.smf')
NO_ANALYSIS = '{}'


def default_root():
    return Path(os.environ.get('SCRUNCH_DATA', str(Path(__file__).resolve().parent / 'data'))) / 'midi-library'


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _role(name):
    role = ' '.join(str(name or '').strip().lower().split())
    if not role or len(role) > 40:
        raise ValueError('A role label needs 1-40 characters.')
    return role


def _folder(path):
    p = str(path or '').strip()
    if not p or not os.path.isabs(p):
        raise ValueError('A whitelisted folder must be an absolute path.')
    return os.path.realpath(p)  # stored file paths are resolved, so the folder must be too


def _under(path, folder):
    return bool(path) and (path == folder or path.startswith(folder.rstrip(os.sep) + os.sep))


class Library:
    def __init__(self, root=None):
        self.root = Path(root) if root else default_root()
        (self.root / 'files').mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.root / 'library.sqlite3'))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS sources (
                sha TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT, original_path TEXT,
                provenance TEXT, imported_at TEXT NOT NULL, bytes INTEGER, tempo REAL,
                time_signature TEXT, tempo_changes INTEGER, meter_changes INTEGER);
            CREATE TABLE IF NOT EXISTS parts (
                id TEXT PRIMARY KEY, sha TEXT NOT NULL REFERENCES sources(sha) ON DELETE CASCADE,
                track INTEGER, channel INTEGER, name TEXT, drums INTEGER NOT NULL,
                note_count INTEGER NOT NULL, length REAL NOT NULL, notes TEXT NOT NULL,
                analysis TEXT NOT NULL, analysis_version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS roles (name TEXT PRIMARY KEY, added_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS labels (
                part_id TEXT NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
                role TEXT NOT NULL REFERENCES roles(name), labelled_at TEXT NOT NULL,
                PRIMARY KEY (part_id, role));
            CREATE TABLE IF NOT EXISTS approved_folders (
                path TEXT PRIMARY KEY, added_at TEXT NOT NULL, added_by TEXT NOT NULL, note TEXT);
            CREATE TABLE IF NOT EXISTS vetting_log (
                at TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, by TEXT NOT NULL, note TEXT);
        ''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(sources)')}
        if 'status' not in columns:  # libraries made before the gate: everything becomes raw
            self.db.execute("ALTER TABLE sources ADD COLUMN status TEXT NOT NULL DEFAULT 'raw'")
            for col in ('vetted_at', 'vetted_by', 'vetting_rule'):
                self.db.execute('ALTER TABLE sources ADD COLUMN %s TEXT' % col)
            self.db.execute("INSERT INTO vetting_log VALUES (?,?,?,?,?)",
                            (_now(), 'gate-installed', '*', 'system', 'all existing sources set to raw'))
        # Invariant: only approved material carries analysis.
        self.db.execute("UPDATE parts SET analysis=?, analysis_version=0 WHERE analysis_version>0 AND "
                        "sha IN (SELECT sha FROM sources WHERE status!='approved')", (NO_ANALYSIS,))
        for r in DEFAULT_ROLES:
            self.db.execute('INSERT OR IGNORE INTO roles VALUES (?,?)', (r, _now()))
        self.db.commit()

    def close(self):
        self.db.close()

    def _log(self, action, target, by, note=''):
        self.db.execute('INSERT INTO vetting_log VALUES (?,?,?,?,?)', (_now(), action, target, by, note or ''))

    # --- roles -------------------------------------------------------------------------
    def roles(self):
        return [r['name'] for r in self.db.execute('SELECT name FROM roles ORDER BY name')]

    def add_role(self, name):
        role = _role(name)
        self.db.execute('INSERT OR IGNORE INTO roles VALUES (?,?)', (role, _now())); self.db.commit()
        return role

    # --- import (always raw unless the folder is whitelisted) ---------------------------
    def _whitelisted(self, original_path):
        for r in self.db.execute('SELECT path FROM approved_folders'):
            if _under(original_path, r['path']):
                return r['path']
        return None

    def _store(self, data, parsed, kind, name, original_path, provenance, roles):
        sha = hashlib.sha256(data).hexdigest()
        existing = self.db.execute('SELECT sha, status FROM sources WHERE sha=?', (sha,)).fetchone()
        if not existing:
            if not parsed['parts']:
                raise ValueError('%s has no notes to learn from.' % (name or 'This MIDI'))
            path = self.root / 'files' / (sha + ('.mid' if data[:4] == b'MThd' else '.json'))
            path.write_bytes(data)
            ts = parsed['timeSignature']
            self.db.execute('INSERT INTO sources (sha, kind, name, original_path, provenance, imported_at, bytes, tempo, '
                            'time_signature, tempo_changes, meter_changes, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (sha, kind, name, original_path, provenance, _now(), len(data), parsed['tempo'],
                             '%d/%d' % tuple(ts), parsed['tempoChanges'], parsed['meterChanges'], 'raw'))
            for i, part in enumerate(parsed['parts']):
                self.db.execute('INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                ('%s:%d' % (sha[:16], i), sha, part['track'], part['channel'], part['name'],
                                 int(part['drums']), len(part['notes']), part['length'],
                                 json.dumps(part['notes'], separators=(',', ':')), NO_ANALYSIS, 0))
            folder = self._whitelisted(original_path)
            if folder:
                self._promote(sha, 'user', 'folder:' + folder, 'imported inside a whitelisted folder')
        ids = [r['id'] for r in self.db.execute('SELECT id FROM parts WHERE sha=? ORDER BY id', (sha,))]
        for pid in ids:
            for role in roles or ():
                self._label(pid, role)
        self.db.commit()
        status = self.db.execute('SELECT status FROM sources WHERE sha=?', (sha,)).fetchone()['status']
        return {'sha': sha, 'duplicate': bool(existing), 'parts': ids, 'status': status}

    def import_file(self, path, roles=(), provenance=''):
        path = Path(path)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError('%s is larger than %d MB; it is not a MIDI phrase.' % (path.name, MAX_FILE_BYTES >> 20))
        data = path.read_bytes()
        return self._store(data, midi_file.read(data), 'file', path.name, str(path.resolve()), provenance, roles)

    def import_directory(self, folder, roles=(), provenance='', recursive=True):
        """Bulk import into raw storage. One bad file does not stop the rest; each failure is reported."""
        folder = Path(folder)
        # '._name' files are macOS metadata that Finder writes beside files on non-Mac disks; not MIDI.
        found = sorted(p for p in (folder.rglob('*') if recursive else folder.iterdir())
                       if p.is_file() and p.suffix.lower() in MIDI_SUFFIXES and not p.name.startswith('._'))
        report = {'imported': [], 'duplicates': [], 'failed': []}
        for p in found:
            try:
                r = self.import_file(p, roles, provenance)
                report['duplicates' if r['duplicate'] else 'imported'].append({'path': str(p), **r})
            except (ValueError, OSError) as e:
                report['failed'].append({'path': str(p), 'reason': str(e)})
        return report

    def import_pasted(self, content, name='pasted', roles=(), provenance='pasted'):
        parsed = midi_file.read_pasted(content)
        if isinstance(content, (bytes, bytearray)) and bytes(content[:4]) == b'MThd':
            data = bytes(content)
        elif parsed['format'] is not None:  # base64 text of a real file: keep the file itself
            import base64
            data = base64.b64decode(''.join(str(content).split()))
        else:
            data = json.dumps(parsed['parts'][0]['notes'], sort_keys=True).encode()
        return self._store(data, parsed, 'paste', name, None, provenance, roles)

    # --- vetting gate --------------------------------------------------------------------
    def _source(self, sha):
        r = self.db.execute('SELECT * FROM sources WHERE sha=?', (sha,)).fetchone()
        if not r:
            raise KeyError('No library file %r.' % sha)
        return r

    def _promote(self, sha, by, rule, note):
        src = self._source(sha)
        num, den = (int(x) for x in src['time_signature'].split('/'))
        for r in self.db.execute('SELECT * FROM parts WHERE sha=?', (sha,)).fetchall():
            a = midi_analysis.analyse(json.loads(r['notes']), r['length'], num * 4 / den, bool(r['drums']))
            self.db.execute('UPDATE parts SET analysis=?, analysis_version=? WHERE id=?',
                            (json.dumps(a, separators=(',', ':')), midi_analysis.VERSION, r['id']))
        self.db.execute("UPDATE sources SET status='approved', vetted_at=?, vetted_by=?, vetting_rule=? WHERE sha=?",
                        (_now(), by, rule, sha))
        self._log('approve', sha, by, rule + (': ' + note if note else ''))

    def _demote(self, sha, status, by, rule, note):
        self.db.execute('UPDATE parts SET analysis=?, analysis_version=0 WHERE sha=?', (NO_ANALYSIS, sha))
        self.db.execute('UPDATE sources SET status=?, vetted_at=?, vetted_by=?, vetting_rule=? WHERE sha=?',
                        (status, _now(), by, rule, sha))
        self._log(status if status == 'rejected' else 'unvet', sha, by, rule + (': ' + note if note else ''))

    def approve(self, sha, note='', by='user'):
        """Promote one file into the active library. Explicit and per file."""
        if self._source(sha)['status'] != 'approved':
            self._promote(sha, by, 'manual', note)
            self.db.commit()
        return self.source(sha)

    def reject(self, sha, note='', by='user'):
        """Keep the file out of active use, and out of folder whitelisting, until explicitly approved."""
        self._source(sha); self._demote(sha, 'rejected', by, 'manual', note); self.db.commit()
        return self.source(sha)

    def unvet(self, sha, note='', by='user'):
        """Send a file back to raw storage."""
        self._source(sha); self._demote(sha, 'raw', by, 'manual', note); self.db.commit()
        return self.source(sha)

    def approve_folder(self, path, note='', by='user'):
        """Whitelist a folder: raw files already imported from inside it, and future imports from it,
        are promoted. Rejected files stay rejected."""
        folder = _folder(path)
        self.db.execute('INSERT OR REPLACE INTO approved_folders VALUES (?,?,?,?)', (folder, _now(), by, note or ''))
        self._log('whitelist-folder', folder, by, note)
        promoted = 0
        for r in self.db.execute("SELECT sha, original_path FROM sources WHERE status='raw'").fetchall():
            if _under(r['original_path'], folder):
                self._promote(r['sha'], by, 'folder:' + folder, note); promoted += 1
        self.db.commit()
        return {'folder': folder, 'promoted': promoted}

    def unapprove_folder(self, path, note='', by='user'):
        """Remove a folder from the whitelist and send the files it promoted back to raw."""
        folder = _folder(path)
        self.db.execute('DELETE FROM approved_folders WHERE path=?', (folder,))
        self._log('unwhitelist-folder', folder, by, note)
        rows = self.db.execute("SELECT sha FROM sources WHERE status='approved' AND vetting_rule=?", ('folder:' + folder,)).fetchall()
        for r in rows:
            self._demote(r['sha'], 'raw', by, 'folder removed: ' + folder, note)
        self.db.commit()
        return {'folder': folder, 'demoted': len(rows)}

    def folders(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM approved_folders ORDER BY path')]

    def vetting_log(self, limit=100):
        return [dict(r) for r in self.db.execute('SELECT * FROM vetting_log ORDER BY rowid DESC LIMIT ?', (limit,))]

    def source(self, sha):
        r = self._source(sha)
        return {k: r[k] for k in r.keys()}

    # --- labels (metadata; never promote) ------------------------------------------------
    def _label(self, part_id, role):
        role = _role(role)
        if role not in self.roles():
            raise ValueError('Unknown role %r. Add it first with add_role; known roles: %s.' % (role, ', '.join(self.roles())))
        if not self.db.execute('SELECT 1 FROM parts WHERE id=?', (part_id,)).fetchone():
            raise KeyError('No library part %r.' % part_id)
        self.db.execute('INSERT OR IGNORE INTO labels VALUES (?,?,?)', (part_id, role, _now()))

    def label(self, part_id, *roles):
        for role in roles:
            self._label(part_id, role)
        self.db.commit()
        return self.part(part_id)['roles']

    def unlabel(self, part_id, role):
        self.db.execute('DELETE FROM labels WHERE part_id=? AND role=?', (part_id, _role(role))); self.db.commit()
        return self.part(part_id)['roles']

    # --- reading -------------------------------------------------------------------------
    def _row(self, r, notes=False):
        src = self.db.execute('SELECT * FROM sources WHERE sha=?', (r['sha'],)).fetchone()
        out = {'id': r['id'], 'sha': r['sha'], 'status': src['status'], 'track': r['track'], 'channel': r['channel'],
               'name': r['name'], 'drums': bool(r['drums']), 'noteCount': r['note_count'], 'length': r['length'],
               'roles': [x['role'] for x in self.db.execute('SELECT role FROM labels WHERE part_id=? ORDER BY role', (r['id'],))]}
        out['source'] = {k: src[k] for k in ('kind', 'name', 'original_path', 'provenance', 'imported_at', 'tempo',
                                             'time_signature', 'vetted_at', 'vetted_by', 'vetting_rule')}
        if notes:
            out['notes'] = json.loads(r['notes'])
            out['analysis'] = json.loads(r['analysis']) if src['status'] == 'approved' else None
        return out

    def part(self, part_id, notes=True):
        """One part in any status (for vetting). Analysis is present only once approved."""
        r = self.db.execute('SELECT * FROM parts WHERE id=?', (part_id,)).fetchone()
        if not r:
            raise KeyError('No library part %r.' % part_id)
        return self._row(r, notes)

    def parts(self, role=None, unlabelled=False, notes=False, status='approved'):
        """Parts for use. Defaults to the approved (active) library; raw or rejected only when asked by name."""
        if status not in STATUSES + ('any',):
            raise ValueError('status is one of %s or any.' % ', '.join(STATUSES))
        where, args = [], []
        if status != 'any':
            where.append('p.sha IN (SELECT sha FROM sources WHERE status=?)'); args.append(status)
        if role:
            where.append('p.id IN (SELECT part_id FROM labels WHERE role=?)'); args.append(_role(role))
        elif unlabelled:
            where.append('p.id NOT IN (SELECT part_id FROM labels)')
        sql = 'SELECT p.* FROM parts p' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY p.id'
        return [self._row(r, notes) for r in self.db.execute(sql, args).fetchall()]

    def remove_source(self, sha):
        """Remove a file and its parts from the library (the producer's own original is untouched)."""
        self.db.execute('DELETE FROM sources WHERE sha=?', (sha,)); self._log('remove', sha, 'user'); self.db.commit()
        for p in (self.root / 'files').glob(sha + '.*'):
            p.unlink()

    def reanalyse(self):
        """Re-run analysis on approved parts measured by an older analysis version. Returns how many changed."""
        rows = self.db.execute("SELECT p.*, s.time_signature FROM parts p JOIN sources s ON s.sha=p.sha "
                               "WHERE s.status='approved' AND analysis_version<?", (midi_analysis.VERSION,)).fetchall()
        for r in rows:
            num, den = (int(x) for x in r['time_signature'].split('/'))
            a = midi_analysis.analyse(json.loads(r['notes']), r['length'], num * 4 / den, bool(r['drums']))
            self.db.execute('UPDATE parts SET analysis=?, analysis_version=? WHERE id=?',
                            (json.dumps(a, separators=(',', ':')), midi_analysis.VERSION, r['id']))
        self.db.commit()
        return len(rows)

    def summary(self):
        count = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]
        by_status = {s: {'files': count('SELECT COUNT(*) FROM sources WHERE status=?', s),
                         'parts': count('SELECT COUNT(*) FROM parts WHERE sha IN (SELECT sha FROM sources WHERE status=?)', s)}
                     for s in STATUSES}
        active_roles = {r['role']: r['n'] for r in self.db.execute(
            "SELECT role, COUNT(*) n FROM labels WHERE part_id IN (SELECT id FROM parts WHERE sha IN "
            "(SELECT sha FROM sources WHERE status='approved')) GROUP BY role")}
        return {'byStatus': by_status, 'whitelistedFolders': len(self.folders()),
                'active': {'parts': by_status['approved']['parts'], 'byRole': active_roles,
                           'unlabelled': count("SELECT COUNT(*) FROM parts WHERE id NOT IN (SELECT part_id FROM labels) AND "
                                               "sha IN (SELECT sha FROM sources WHERE status='approved')")}}


def main(argv=None):
    """python3 midi_library.py
         import FOLDER_OR_FILE [--role R ...] [--provenance TEXT]   (always into raw storage)
         approve SHA [--note] | reject SHA [--note] | unvet SHA [--note]
         approve-folder PATH [--note] | unapprove-folder PATH [--note] | folders | log
         label PART ROLE... | unlabel PART ROLE | add-role NAME
         list [--status raw|approved|rejected|any] [--role R | --unlabelled] | show PART | summary"""
    import argparse
    ap = argparse.ArgumentParser(description='Scrunch private MIDI reference library (local only, vetting gate)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('import'); i.add_argument('path'); i.add_argument('--role', action='append', default=[])
    i.add_argument('--provenance', default='')
    for name in ('approve', 'reject', 'unvet'):
        v = sub.add_parser(name); v.add_argument('sha'); v.add_argument('--note', default='')
    for name in ('approve-folder', 'unapprove-folder'):
        v = sub.add_parser(name); v.add_argument('path'); v.add_argument('--note', default='')
    sub.add_parser('folders'); sub.add_parser('log')
    lb = sub.add_parser('label'); lb.add_argument('part'); lb.add_argument('roles', nargs='+')
    ul = sub.add_parser('unlabel'); ul.add_argument('part'); ul.add_argument('role')
    ar = sub.add_parser('add-role'); ar.add_argument('name')
    ls = sub.add_parser('list'); ls.add_argument('--role'); ls.add_argument('--unlabelled', action='store_true')
    ls.add_argument('--status', default='approved')
    sh = sub.add_parser('show'); sh.add_argument('part')
    sub.add_parser('summary')
    a = ap.parse_args(argv)
    lib = Library()
    try:
        if a.cmd == 'import':
            p = Path(a.path)
            out = lib.import_directory(p, a.role, a.provenance) if p.is_dir() else lib.import_file(p, a.role, a.provenance)
        elif a.cmd in ('approve', 'reject', 'unvet'):
            out = getattr(lib, a.cmd)(a.sha, a.note)
        elif a.cmd == 'approve-folder':
            out = lib.approve_folder(a.path, a.note)
        elif a.cmd == 'unapprove-folder':
            out = lib.unapprove_folder(a.path, a.note)
        elif a.cmd == 'folders':
            out = lib.folders()
        elif a.cmd == 'log':
            out = lib.vetting_log()
        elif a.cmd == 'label':
            out = lib.label(a.part, *a.roles)
        elif a.cmd == 'unlabel':
            out = lib.unlabel(a.part, a.role)
        elif a.cmd == 'add-role':
            out = lib.add_role(a.name)
        elif a.cmd == 'list':
            out = [{k: p[k] for k in ('id', 'status', 'name', 'roles', 'noteCount', 'length', 'drums')} | {'file': p['source']['name']}
                   for p in lib.parts(role=a.role, unlabelled=a.unlabelled, status=a.status)]
        elif a.cmd == 'show':
            out = lib.part(a.part)
        else:
            out = lib.summary()
        print(json.dumps(out, indent=1, default=str))
    finally:
        lib.close()


if __name__ == '__main__':
    main()
