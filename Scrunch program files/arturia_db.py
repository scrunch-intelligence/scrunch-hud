"""Put a saved preset into Arturia's browser, the same way one of the producer's own saves sits there.

Arturia's preset browser lists what is in `db.db3`, not what is in the folder, so a preset file
written by Scrunch is invisible until it has a row. This writes that row - and nothing else. It
never touches ratings, hearts, playlists or any preset it did not create, and every preset it
registers can be taken out again by the same name.

The database holds the producer's own likes and ratings, so a verified copy of it (with its
-wal and -shm) is taken before any write, and the write is refused outright if the copy cannot
be made or does not match.
"""
import hashlib,shutil,sqlite3,time
from pathlib import Path

DB=Path('/Library/Arturia/Presets/db.db3')
PARTS=('','-wal','-shm')
USER_PACK='User'

def _digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()

def backup(folder,db=DB):
    """A verified copy of the database and its journals. Returns where it went and what it hashed to."""
    db=Path(db)
    if not db.is_file():raise ValueError('Arturia\'s preset database is not where Scrunch expects it (%s).'%db)
    out=Path(folder)/('db-%s'%time.strftime('%Y%m%d-%H%M%S'));out.mkdir(parents=True,exist_ok=True)
    copied={}
    for part in PARTS:
        src=Path(str(db)+part)
        if not src.is_file():continue
        dst=out/src.name;shutil.copy2(src,dst)
        before,after=_digest(src),_digest(dst)
        if before!=after:raise ValueError('The backup of %s did not match the original; nothing was written.'%src.name)
        copied[src.name]=after
    if not copied:raise ValueError('Nothing was copied; refusing to write to the preset database.')
    return {'folder':str(out),'files':copied}

def _connect(db=DB):
    con=sqlite3.connect(str(db),timeout=5.0);con.row_factory=sqlite3.Row
    return con

def registered(file_path,db=DB):
    """The browser row for a preset file, or None."""
    con=_connect(db)
    try:
        row=con.execute('select * from Preset_Id where file_path=?',(str(file_path),)).fetchone()
        return dict(row) if row else None
    finally:con.close()

def designer_key(con,name):
    row=con.execute('select key_id from Sound_Designers where name=?',(name,)).fetchone()
    if row:return row['key_id']
    key=(con.execute('select max(key_id) from Sound_Designers').fetchone()[0] or 0)+1
    con.execute('insert into Sound_Designers (key_id,name) values (?,?)',(key,name))
    return key

def pack_key(con,name=USER_PACK):
    row=con.execute('select key_id from Packs where name=?',(name,)).fetchone()
    if not row:raise ValueError('Arturia has no %r pack to file this under.'%name)
    return row['key_id']

def register(preset_path,base_path,name,designer='Scrunch',comment=None,db=DB):
    """Give a Scrunch-written preset a browser row, modelled on the preset it was designed from.

    Every field the browser needs is copied from the row of the preset this sound started out as,
    so it is filed as the same kind of sound, for the same instrument, at the same plug-in
    version. Only what genuinely differs is changed: its name, where it lives, that it is in the
    User pack, that Scrunch designed it, and that it is not a favourite until the producer says so.
    """
    preset_path,base_path=str(preset_path),str(base_path)
    if not Path(preset_path).is_file():raise ValueError('There is no preset file at %s.'%preset_path)
    con=_connect(db)
    try:
        if con.execute('select 1 from Preset_Id where file_path=?',(preset_path,)).fetchone():
            raise ValueError('That preset is already in the browser.')
        base=con.execute('select * from Preset_Id where file_path=?',(base_path,)).fetchone()
        if not base:raise ValueError('Scrunch cannot find the preset this sound came from in the browser (%s).'%base_path)
        row=dict(base)
        key=(con.execute('select max(key_id) from Preset_Id').fetchone()[0] or 0)+1
        row.update(key_id=key,name=name,file_path=preset_path,pack=pack_key(con),
                   sound_designer=designer_key(con,designer),favorite=0,rating=0.0,
                   comment=comment or ('Designed in Scrunch from %s.'%base['name']),
                   time_last_save=str(int(time.time())),time_last_open=str(int(time.time())))
        fields=list(row)
        con.execute('insert into Preset_Id (%s) values (%s)'%(','.join('"%s"'%f for f in fields),
                                                              ','.join('?' for _ in fields)),
                    [row[f] for f in fields])
        meta=con.execute('select name,value from Preset_Metadata where preset_key=?',(base['key_id'],)).fetchall()
        next_meta=(con.execute('select max(key_id) from Preset_Metadata').fetchone()[0] or 0)+1
        for i,m in enumerate(meta):
            con.execute('insert into Preset_Metadata (key_id,preset_key,name,value) values (?,?,?,?)',
                        (next_meta+i,key,m['name'],m['value']))
        con.commit()
    finally:con.close()
    written=registered(preset_path,db)
    if not written or written['name']!=name:
        raise ValueError('The browser row was not written as asked; the preset file is untouched.')
    return {'presetKey':written['key_id'],'name':written['name'],'pack':USER_PACK,
            'type':written['type_v2'],'subtype':written['subtype_v2'],'metadataRows':len(meta),
            'modelledOn':base['name']}

def unregister(preset_path,db=DB):
    """Take a Scrunch-registered preset back out of the browser. The file itself is left alone."""
    con=_connect(db)
    try:
        row=con.execute('select key_id from Preset_Id where file_path=?',(str(preset_path),)).fetchone()
        if not row:return {'removed':0}
        con.execute('delete from Preset_Metadata where preset_key=?',(row['key_id'],))
        con.execute('delete from Preset_Id where key_id=?',(row['key_id'],))
        con.commit()
        return {'removed':1,'presetKey':row['key_id']}
    finally:con.close()
