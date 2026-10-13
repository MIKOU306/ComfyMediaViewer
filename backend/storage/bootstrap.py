"""Initialize genuinely empty installations, never adopt existing user material."""
from pathlib import Path
import json
import os
from .database import connect, initialize_schema, new_identity
from .runtime_state import publish_state, _safe_path
from backend.migration.guard import DataWriterGuard


def initialize_fresh(data_dir, guard):
    root = Path(data_dir).absolute()
    if not isinstance(guard, DataWriterGuard) or guard.stream is None or guard.stream.closed \
            or guard.path != root.resolve() / '.cmv-writer.lock':
        raise RuntimeError('Fresh initialization requires the owned writer guard')
    def internal(item):
        if item.name == '.cmv-writer.lock':
            return True
        # Read-only installations can create this empty fallback during import.
        return item.name == 'hotfix' and _safe_path(root, 'hotfix').is_dir() and not any(item.iterdir())
    if any(not internal(item) for item in root.iterdir()):
        raise RuntimeError('Data appeared during first startup; refusing to initialize over it')
    database = _safe_path(root, 'catalog.sqlite')
    # Even a partially created catalog requires recovery, never silent replacement.
    if database.exists() or _safe_path(root, 'storage-state.json').exists():
        raise RuntimeError('Fresh catalog already exists; initialization refused')
    library, profile = new_identity(), new_identity()
    intent = _safe_path(root,'fresh-initialization.json')
    with intent.open('x',encoding='utf-8') as stream:
        json.dump({'version':1,'intent':'initialize-empty','database':'catalog.sqlite',
                   'libraryUuid':library,'profileUuid':profile},stream)
        stream.flush();os.fsync(stream.fileno())
    database.open('xb').close()
    db = connect(database)
    try:
        initialize_schema(db)
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO library(uuid,name) VALUES(?,?)', (library, 'Local'))
        owner = db.execute('SELECT id FROM library WHERE uuid=?', (library,)).fetchone()[0]
        db.execute('INSERT INTO local_profile(uuid,library_id,name) VALUES(?,?,?)', (profile, owner, 'Local'))
        db.commit()
    finally:
        db.close()
    state = publish_state(root, {'version':1,'mode':'sqlite','database':'catalog.sqlite',
                                 'libraryUuid':library,'profileUuid':profile})
    intent.unlink()
    return state
