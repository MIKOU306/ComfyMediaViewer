"""Forward-only, non-unique lookup indexes; never edit core migration history."""
import hashlib
from pathlib import Path
import sqlite3

SQL = (
    'CREATE INDEX cmv_asset_library_path_root_idx ON media_asset(library_id,path,root_id)',
    'CREATE INDEX cmv_asset_library_root_path_idx ON media_asset(library_id,root_id,path)',
)
CHECKSUM = hashlib.sha256(('1\n' + '\n'.join(SQL)).encode()).hexdigest()


def initialize(database):
    db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=rw', uri=True)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        found = db.execute("SELECT 1 FROM sqlite_master WHERE name='cmv_performance_schema'").fetchone()
        if not found:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'cmv_asset_library_%'").fetchone():
                raise RuntimeError('Unversioned performance indexes')
            db.execute('CREATE TABLE cmv_performance_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)')
            for sql in SQL:
                db.execute(sql)
            db.execute('INSERT INTO cmv_performance_schema VALUES(1,?)', (CHECKSUM,))
        if db.execute('SELECT version,checksum FROM cmv_performance_schema ORDER BY version').fetchall() != [(1, CHECKSUM)]:
            raise RuntimeError('Performance schema checksum/version mismatch')
        for sql in SQL:
            name = sql.split()[2]
            actual = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
            if actual is None or actual[0] != sql:
                raise RuntimeError('Performance index missing or changed')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
