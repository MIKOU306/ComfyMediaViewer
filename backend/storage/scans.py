"""Persistent root scan epochs. Only a completed current epoch publishes absence."""
import hashlib
import uuid

from backend.storage.catalog import CatalogRepository, IdentityConflictError

SQL = (
    "CREATE TABLE catalog_scan_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)",
    "CREATE TABLE catalog_scan(root_id INTEGER PRIMARY KEY REFERENCES library_root(id),"
    "epoch TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('running','complete','failed')),error TEXT)",
    "CREATE TABLE catalog_scan_seen(root_id INTEGER NOT NULL REFERENCES library_root(id),"
    "epoch TEXT NOT NULL,asset_id INTEGER NOT NULL REFERENCES media_asset(id),PRIMARY KEY(root_id,epoch,asset_id))",
)
CHECKSUM = hashlib.sha256(('1\n' + '\n'.join(SQL)).encode()).hexdigest()


class ScanCancelled(RuntimeError):
    """A scan batch noticed cancellation before committing its writes."""


def require_epoch(db, root_id, epoch):
    row = db.execute('SELECT epoch,state FROM catalog_scan WHERE root_id=?', (root_id,)).fetchone()
    if row is None or row['epoch'] != epoch or row['state'] != 'running':
        raise IdentityConflictError('Scan epoch is no longer current and running')


def require_roots(db, library_id, expected):
    if expected is not None:
        actual = [tuple(row) for row in db.execute('SELECT id,path,case_sensitive FROM library_root '
                                                   'WHERE library_id=? ORDER BY id', (library_id,))]
        if actual != expected:
            raise IdentityConflictError('Registered root scope changed during scan')


class CatalogScans:
    def __init__(self, database):
        self.catalog = CatalogRepository(database)

    def initialize(self):
        with self.catalog._transaction(write=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_scan_schema'").fetchone()
            if exists:
                rows = db.execute('SELECT version,checksum FROM catalog_scan_schema').fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (1, CHECKSUM):
                    raise ValueError('Scan schema checksum/version mismatch')
            else:
                for statement in SQL:
                    db.execute(statement)
                db.execute('INSERT INTO catalog_scan_schema VALUES(1,?)', (CHECKSUM,))
            from backend.storage.directories import initialize
            initialize(db)
            from backend.storage.scan_fingerprint import initialize as initialize_fingerprint
            initialize_fingerprint(db)

    def begin(self, library_uuid, root_uuid):
        epoch = str(uuid.uuid4())
        with self.catalog._transaction(write=True) as db:
            _, root = self.catalog._scope(db, library_uuid, root_uuid)
            db.execute("INSERT INTO catalog_scan(root_id,epoch,state) VALUES(?,?,'running') "
                       "ON CONFLICT(root_id) DO UPDATE SET epoch=excluded.epoch,state='running',error=NULL",
                       (root['id'], epoch))
            db.execute('DELETE FROM catalog_scan_seen WHERE root_id=?', (root['id'],))
        return epoch

    def recover_interrupted(self, library_uuid):
        """Called once at startup by the holder of the application writer lock.

        A persisted running epoch is not evidence of a live worker. Recovery
        changes only task checkpoints, never availability or user records.
        """
        with self.catalog._transaction(write=True) as db:
            library = db.execute('SELECT id FROM library WHERE uuid=?', (library_uuid,)).fetchone()
            if library is None:
                raise ValueError('Unknown scan library')
            rows = db.execute("SELECT s.root_id,s.epoch FROM catalog_scan s JOIN library_root r "
                              "ON r.id=s.root_id WHERE r.library_id=? AND s.state='running'",
                              (library['id'],)).fetchall()
            for row in rows:
                db.execute("UPDATE catalog_scan SET state='failed',error='interrupted' WHERE root_id=? AND epoch=?",
                           (row['root_id'], row['epoch']))
            return {'interruptedEpochs': [row['epoch'] for row in rows], 'recovered': len(rows)}

    def observe(self, library_uuid, root_uuid, epoch, path, file_stat, *, expected_roots=None):
        return self.catalog.observe_file(library_uuid, root_uuid, path, file_stat,
                                         scan_epoch=epoch, expected_roots=expected_roots)

    def observe_batch(self, library_uuid, root_uuid, epoch, files, *,
                      expected_roots=None, cancelled=lambda: False):
        return self.catalog.observe_scanned_files(
            library_uuid, root_uuid, epoch, files,
            expected_roots=expected_roots, cancelled=cancelled)

    def finish(self, library_uuid, root_uuid, epoch, *, failure=None, expected_roots=None, fingerprint=None):
        if failure not in (None, 'offline', 'unreadable', 'cancelled', 'changed'):
            raise ValueError('Unknown scan failure')
        with self.catalog._transaction(write=True) as db:
            library, root = self.catalog._scope(db, library_uuid, root_uuid)
            require_epoch(db, root['id'], epoch)
            if failure is None:
                require_roots(db, library['id'], expected_roots)
            if failure:
                db.execute("UPDATE catalog_scan SET state='failed',error=? WHERE root_id=?", (failure, root['id']))
                if failure not in ('cancelled','changed'):
                    db.execute('UPDATE library_root SET availability=? WHERE id=?', (failure, root['id']))
                # Partial enumeration and I/O failures never publish absence.
                return {'state': 'failed', 'coverage': 'partial', 'failure': failure}
            db.execute("UPDATE media_asset SET availability='missing' WHERE root_id=? "
                       "AND availability IN ('present','unknown','offline','unreadable') "
                       "AND id NOT IN (SELECT asset_id FROM catalog_scan_seen WHERE root_id=? AND epoch=?)",
                       (root['id'], root['id'], epoch))
            db.execute("UPDATE library_root SET availability='online' WHERE id=?", (root['id'],))
            from backend.storage.directories import finish
            finish(db, root['id'], epoch)
            db.execute("UPDATE catalog_scan SET state='complete',error=NULL WHERE root_id=?", (root['id'],))
            if fingerprint:
                from backend.storage.scan_fingerprint import initialize
                initialize(db)
                db.execute('INSERT INTO catalog_scan_fingerprint(root_id,epoch,signature) VALUES(?,?,?) '
                           'ON CONFLICT(root_id) DO UPDATE SET epoch=excluded.epoch,signature=excluded.signature',
                           (root['id'],epoch,fingerprint))
            return {'state': 'complete', 'coverage': 'complete'}

    def observe_directory(self, library_uuid, root_uuid, epoch, path, *, expected_roots=None):
        from backend.storage.directories import observe
        with self.catalog._transaction(write=True) as db:
            library, root = self.catalog._scope(db, library_uuid, root_uuid)
            require_epoch(db, root['id'], epoch)
            require_roots(db, library['id'], expected_roots)
            root = db.execute('SELECT id,path,case_sensitive FROM library_root WHERE id=?', (root['id'],)).fetchone()
            observe(db, root, epoch, path)
