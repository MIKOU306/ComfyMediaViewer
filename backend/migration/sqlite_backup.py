"""Consistent SQLite snapshots, including committed WAL data."""
from pathlib import Path
import os
import sqlite3
import tempfile

from .backup import SnapshotError


def _reject_links(path):
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise SnapshotError('SQLite backup paths must not contain links')
        if candidate.exists() and getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400:
            raise SnapshotError('SQLite backup paths must not contain reparse points')


def backup_database(source, destination):
    """Publish a checked, standalone backup to a new destination.

    This is a database snapshot, not an atomic snapshot of accompanying JSON or
    media files. The caller must pause writers to coordinate those domains.
    """
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    _reject_links(source)
    _reject_links(destination)
    if not source.is_file():
        raise SnapshotError('Source database does not exist')
    if destination.exists() or source == destination:
        raise SnapshotError('Backup destination must be new')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.sqlite-backup-', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    reader = writer = None
    try:
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
        writer = sqlite3.connect(str(temporary))
        reader.backup(writer)
        result = writer.execute('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise SnapshotError('Backup integrity check failed: ' + repr(result))
        if writer.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise SnapshotError('Backup contains foreign key violations')
        writer.close()
        writer = None
        reader.close()
        reader = None
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        # Exclusive destination publication: never overwrite a concurrent file.
        os.link(temporary, destination)
        return destination
    except sqlite3.Error as error:
        raise SnapshotError('SQLite snapshot failed: ' + str(error)) from error
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        temporary.unlink(missing_ok=True)
