"""Versioned catalog projection of deliberate deletion, separate from absence."""
import hashlib
import sqlite3
from pathlib import Path

SQL = """CREATE TABLE asset_lifecycle (
 library_id INTEGER NOT NULL, asset_id INTEGER PRIMARY KEY,
 revision_uuid TEXT NOT NULL, operation_uuid TEXT NOT NULL,
 FOREIGN KEY(library_id,asset_id) REFERENCES media_asset(library_id,id) ON DELETE RESTRICT
);"""
CHECKSUM = hashlib.sha256(('1\n' + SQL).encode()).hexdigest()


def validate(connection, *, create=False):
    found = connection.execute("SELECT 1 FROM sqlite_master WHERE name='asset_lifecycle_schema'").fetchone()
    if not found:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='asset_lifecycle'").fetchone():
            raise RuntimeError('Partial asset lifecycle schema')
        if not create:
            return False
        connection.execute('CREATE TABLE asset_lifecycle_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)')
        connection.execute(SQL.rstrip(';'))
        connection.execute('INSERT INTO asset_lifecycle_schema VALUES(1,?)', (CHECKSUM,))
    rows = connection.execute('SELECT version,checksum FROM asset_lifecycle_schema ORDER BY version').fetchall()
    if [tuple(row) for row in rows] != [(1, CHECKSUM)]:
        raise RuntimeError('Unsupported asset lifecycle schema/checksum')
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='asset_lifecycle'").fetchone():
        raise RuntimeError('Missing asset lifecycle table')
    return True


def visible_clause(connection, alias='a'):
    # Older catalogs without this explicitly installed extension retain old reads.
    return (f'NOT EXISTS (SELECT 1 FROM asset_lifecycle dl WHERE dl.asset_id={alias}.id '
            f'AND dl.library_id={alias}.library_id)') if validate(connection) else '1=1'


def initialize(database, trash_store=None):
    """Install under the caller's writer guard; backfill only confirmed trash evidence."""
    source = None
    if trash_store is not None and Path(trash_store).exists():
        from backend.operations.trash import CatalogTrash
        source = sqlite3.connect(Path(trash_store).absolute().as_uri() + '?mode=ro', uri=True)
        source.row_factory = sqlite3.Row
        try:
            CatalogTrash._check(source)
        except Exception:
            source.close()
            raise
    try:
        db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=rw', uri=True)
    except Exception:
        if source is not None:
            source.close()
        raise
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        validate(db, create=True)
        if source is not None:
            for row in source.execute("SELECT library_uuid,media_uuid,revision_uuid,trash_operation_uuid,orig_path "
                                      "FROM trash_item WHERE state IN ('trashed','pending_restore','purging','purged')"):
                # Never hide a different revision or a present replacement from old evidence.
                db.execute('INSERT OR IGNORE INTO asset_lifecycle(library_id,asset_id,revision_uuid,operation_uuid) '
                           'SELECT a.library_id,a.id,?,? FROM media_asset a JOIN library l ON l.id=a.library_id '
                           'JOIN media_revision r ON r.id=a.current_revision_id '
                           "WHERE l.uuid=? AND a.uuid=? AND r.uuid=? AND a.path=? AND a.availability='missing'",
                           (row['revision_uuid'], row['trash_operation_uuid'], row['library_uuid'],
                            row['media_uuid'], row['revision_uuid'], row['orig_path']))
        # Existing cursors must expire when historical rows gain lifecycle markers.
        if db.total_changes and db.execute("SELECT 1 FROM sqlite_master WHERE name='query_index_state'").fetchone():
            db.execute('UPDATE query_index_state SET serial=serial+1 WHERE singleton=1')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        if source is not None:
            source.close()
