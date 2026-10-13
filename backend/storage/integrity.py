"""Reuse audits only while SQLite reports the same on-disk database generation.

A dedicated read-only connection detects commits from every other connection,
including writers that bypass the cooperative process guard or preserve stat.
Replacement, WAL changes and DDL also invalidate. FK enforcement and repository
scope/version checks still execute on every operation. No database is adopted.
"""
from collections import OrderedDict
import hashlib
from pathlib import Path
import sqlite3
import threading

_lock = threading.RLock()
_audits = OrderedDict()
_expected = None


def _version(monitor):
    # A DELETE-journal writer can enter PENDING while our caller holds SHARED.
    # Waiting on a second connection then prevents that very caller from ending
    # its snapshot. A busy monitor therefore means audit, never wait or trust.
    try:
        return monitor.execute('PRAGMA data_version').fetchone()[0]
    except sqlite3.OperationalError as error:
        if 'locked' in str(error).lower() or 'busy' in str(error).lower():
            return None
        raise


def _objects(db):
    return {(r[0], r[1]): r[2] for r in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")}


def _core_objects():
    global _expected
    if _expected is None:
        from .schema import schema_sql
        db = sqlite3.connect(':memory:')
        try:
            db.executescript(schema_sql())
            _expected = _objects(db)
        finally:
            db.close()
    return _expected


def foreign_key_violation(db):
    """Return a violation row as PRAGMA did, or None for a validated generation."""
    if not db.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise RuntimeError('Foreign key enforcement is required')
    filename = db.execute('PRAGMA database_list').fetchone()[2]
    if not filename:
        return db.execute('PRAGMA foreign_key_check').fetchone()
    path = Path(filename).absolute()
    with _lock:
        info = path.stat()
        identity = (info.st_dev, info.st_ino, info.st_ctime_ns)
        record = _audits.get(str(path))
        if record and record['identity'] != identity:
            record['monitor'].close()
            del _audits[str(path)]
            record = None
        if record is None:
            monitor = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, check_same_thread=False, timeout=0)
            record = {'identity':identity,'monitor':monitor,'key':None}
            _audits[str(path)] = record
        _audits.move_to_end(str(path))
        while len(_audits) > 8:
            _, old = _audits.popitem(last=False)
            old['monitor'].close()
        monitor = record['monitor']
        version = _version(monitor)
        objects = _objects(db)
        schema = hashlib.sha256(repr(sorted(objects.items())).encode()).hexdigest()
        wal = Path(str(path) + '-wal')
        wal_info = wal.stat() if wal.exists() else None
        stamp = (info.st_size, info.st_mtime_ns,
                 (wal_info.st_size, wal_info.st_mtime_ns) if wal_info else None)
        key = (version, stamp, schema)
        stable = version is not None and _version(monitor) == version
        if stable and record['key'] == key:
            return None
        record['key'] = None
        for name, sql in _core_objects().items():
            if objects.get(name) != sql:
                raise RuntimeError('Core catalog schema object missing or changed: ' + name[1])
        violation = db.execute('PRAGMA foreign_key_check').fetchone()
        if stable and violation is None and _version(monitor) == version:
            record['key'] = key
        return violation
