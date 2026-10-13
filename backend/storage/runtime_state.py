"""Validated persistent authority pointer; database presence is never activation."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

from .database import initialize_schema


class StorageStateError(RuntimeError):
    pass


def _uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise StorageStateError('Invalid storage identity') from error


def _safe_path(data_dir, relative):
    if not isinstance(relative, str):
        raise StorageStateError('Invalid database path')
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or ':' in relative:
        raise StorageStateError('Database must be within the application data directory')
    root = Path(data_dir).absolute()
    candidate = root / path
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise StorageStateError('Storage paths must not contain links or reparse points')
    return candidate


def validate_state(data_dir, state):
    if not isinstance(state, dict) or state.get('version') != 1:
        raise StorageStateError('Unsupported storage pointer version')
    if state.get('mode') != 'sqlite':
        raise StorageStateError('Unsupported storage authority')
    library = _uuid(state.get('libraryUuid'))
    profile = _uuid(state.get('profileUuid'))
    database = _safe_path(data_dir, state.get('database'))
    if not database.is_file():
        raise StorageStateError('Active database is missing; refusing silent JSON fallback')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        initialize_schema(connection)
        owner = connection.execute(
            'SELECT p.id FROM local_profile p JOIN library l ON l.id=p.library_id '
            'WHERE p.uuid=? AND l.uuid=?', (profile, library)).fetchone()
        if owner is None:
            raise StorageStateError('Profile does not belong to the selected library')
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise StorageStateError('Active database has foreign key violations')
    finally:
        connection.close()
    return {**state, 'libraryUuid': library, 'profileUuid': profile}


def recover_catalog_journal(data_dir, writer_guard):
    """Allow SQLite crash recovery only while the caller owns the data writer guard.

    Read-only validation cannot roll back a hot DELETE journal after a forced
    process exit. This opens an existing authority in rw mode, without creating
    a database or initializing/upgrading its schema, before ordinary validation.
    """
    from backend.migration.guard import DataWriterGuard
    root=Path(data_dir).absolute()
    if not isinstance(writer_guard,DataWriterGuard) or writer_guard.stream is None \
            or writer_guard.stream.closed or writer_guard.path!=root.resolve()/'.cmv-writer.lock':
        raise StorageStateError('Catalog journal recovery requires the owned data writer guard')
    pointer=_safe_path(root,'storage-state.json')
    if not pointer.is_file():
        return
    with pointer.open('rb') as stream:
        raw=stream.read(65537)
    if len(raw)>65536:
        raise StorageStateError('Storage pointer exceeds supported size')
    state=json.loads(raw.decode('utf-8'))
    if not isinstance(state,dict) or state.get('version')!=1 or state.get('mode')!='sqlite':
        raise StorageStateError('Unsupported storage authority')
    _uuid(state.get('libraryUuid'));_uuid(state.get('profileUuid'))
    database=_safe_path(root,state.get('database'))
    if not database.is_file():
        raise StorageStateError('Active database is missing; refusing to create it')
    for suffix in ('-journal','-wal','-shm'):
        _safe_path(root,state['database']+suffix)
    connection=sqlite3.connect(database.as_uri()+'?mode=rw',uri=True)
    try:
        connection.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
    finally:
        connection.close()


def check_relocation_startup(data_dir):
    if _safe_path(data_dir,'relocation-retired.json').exists():
        raise StorageStateError('Data directory has been retired by relocation; use the target directory or recovery tool')
    cutover = _safe_path(data_dir,'relocation-cutover.json')
    if cutover.exists():
        try:
            intent=json.loads(cutover.read_text('utf-8'))
        except (OSError,ValueError) as error:
            raise StorageStateError('Cannot read relocation cutover state') from error
        if not isinstance(intent,dict) or intent.get('version')!=1 or intent.get('phase')!='completed':
            raise StorageStateError('Relocation cutover is incomplete; recovery required before startup')
        try:
            target=Path(data_dir).absolute()
            if intent.get('authorityChanged') is not True or Path(intent['candidate'])!=target:
                raise ValueError('Completed relocation belongs to another directory')
            selector=Path(intent['selector'])
            if not selector.is_absolute() or selector.name!='portable.flag':
                raise ValueError('Completed relocation selector is invalid')
            selected=json.loads(_safe_path(selector.parent,selector.name).read_text('utf-8'))
            if not isinstance(selected,dict) or Path(selected.get('dir','')).absolute()!=target:
                raise ValueError('Completed relocation selector points elsewhere')
            state=json.loads(_safe_path(target,'storage-state.json').read_text('utf-8'))
            if state!=intent['storage']:
                raise ValueError('Completed relocation authority differs')
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise StorageStateError('Completed relocation evidence is inconsistent') from error


def load_state(data_dir):
    check_relocation_startup(data_dir)
    pointer = _safe_path(data_dir, 'storage-state.json')
    if not pointer.exists():
        if _safe_path(data_dir,'fresh-initialization.json').exists():
            raise StorageStateError('Fresh initialization was interrupted; preserving its database and refusing JSON fallback')
        if _safe_path(data_dir,'relocation-databases.json').exists():
            raise StorageStateError('Relocation candidate is not activated; refusing legacy JSON fallback')
        return {'version': 1, 'mode': 'legacy-json'}
    try:
        state = json.loads(pointer.read_text('utf-8'))
    except (OSError, ValueError) as error:
        raise StorageStateError('Cannot read storage authority; refusing fallback') from error
    return validate_state(data_dir, state)


def publish_state(data_dir, state):
    """Caller must hold the writer guard and complete reconciliation first.

    This only publishes a validated pointer. It does not perform migration,
    source reconciliation, database copying or rollback export.
    """
    validated = validate_state(data_dir, state)
    pointer = _safe_path(data_dir, 'storage-state.json')
    descriptor, name = tempfile.mkstemp(prefix='.storage-state-', dir=pointer.parent)
    staging = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(validated, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, pointer)
    finally:
        staging.unlink(missing_ok=True)
    return validated
