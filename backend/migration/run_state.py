"""Persistent compare-and-swap migration checkpoints, separate from live authority."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


class MigrationRunError(RuntimeError):
    pass


SCHEMA = (
    'CREATE TABLE migration_run_schema(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)',
    'CREATE TABLE migration_run(uuid TEXT PRIMARY KEY,identity_json TEXT NOT NULL,state TEXT NOT NULL,'
    'checkpoint TEXT NOT NULL,event_no INTEGER NOT NULL DEFAULT 0)',
    'CREATE TABLE migration_run_event(run_uuid TEXT NOT NULL,event_no INTEGER NOT NULL,'
    'before_state TEXT NOT NULL,after_state TEXT NOT NULL,evidence_json TEXT NOT NULL,'
    'PRIMARY KEY(run_uuid,event_no),FOREIGN KEY(run_uuid) REFERENCES migration_run(uuid))',
)
CHECKSUM = hashlib.sha256(('1\n' + '\n'.join(SCHEMA)).encode()).hexdigest()
NEXT = {'preflight': {'backed_up', 'failed'}, 'backed_up': {'importing', 'failed'},
        'importing': {'reconciled', 'failed'}, 'reconciled': {'switching', 'failed'},
        'switching': {'active', 'failed'}, 'active': {'rollback_exported'}, 'rollback_exported': set()}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class MigrationRuns:
    def __init__(self, database):
        self.database = Path(database).absolute()

    @contextmanager
    def _connection(self, *, write=False, initialize=False):
        for path in (self.database, *self.database.parents):
            if path.is_symlink() or (path.exists() and getattr(path.stat(), 'st_file_attributes', 0) & 0x400):
                raise MigrationRunError('Migration registry cannot contain links')
        if not initialize and not self.database.is_file():
            raise MigrationRunError('Migration registry is not initialized')
        mode = 'rwc' if initialize else 'rw' if write else 'ro'
        connection = sqlite3.connect(self.database.as_uri() + '?mode=' + mode, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if not initialize:
                self._validate(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate(connection):
        try:
            rows = connection.execute('SELECT version,checksum FROM migration_run_schema').fetchall()
            if len(rows) != 1 or rows[0]['version'] != 1 or rows[0]['checksum'] != CHECKSUM:
                raise MigrationRunError('Migration registry version/checksum mismatch')
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise MigrationRunError('Migration registry foreign key violations')
        except sqlite3.Error as error:
            raise MigrationRunError('Migration registry schema missing') from error

    def initialize(self):
        with self._connection(write=True, initialize=True) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables:
                self._validate(connection)
                return
            for sql in SCHEMA:
                connection.execute(sql)
            connection.execute('INSERT INTO migration_run_schema VALUES(1,?)', (CHECKSUM,))

    def create(self, run_uuid, identity):
        run_uuid = str(uuid.UUID(run_uuid))
        if not isinstance(identity, dict) or not all(identity.get(field) for field in
                ('sourceManifestHash', 'sourceDataDir', 'snapshot', 'shadowDatabase')):
            raise MigrationRunError('Immutable source and target identity required')
        encoded = _json(identity)
        with self._connection(write=True) as db:
            existing = db.execute('SELECT * FROM migration_run WHERE uuid=?', (run_uuid,)).fetchone()
            if existing:
                if existing['identity_json'] != encoded:
                    raise MigrationRunError('Run UUID cannot adopt another source or target')
            else:
                db.execute("INSERT INTO migration_run(uuid,identity_json,state,checkpoint) VALUES(?,?,'preflight','preflight')",
                           (run_uuid, encoded))
        return self.read(run_uuid)

    def read(self, run_uuid):
        with self._connection() as db:
            row = db.execute('SELECT * FROM migration_run WHERE uuid=?', (run_uuid,)).fetchone()
            if row is None:
                raise MigrationRunError('Unknown migration run')
            if row['state'] not in {*NEXT, 'failed'} or row['checkpoint'] not in NEXT or row['event_no'] < 0:
                raise MigrationRunError('Invalid persisted migration state')
            return {**dict(row), 'identity': json.loads(row['identity_json'])}

    def transition(self, run_uuid, expected, target, evidence):
        if not isinstance(evidence, dict) or not evidence:
            raise MigrationRunError('Checkpoint evidence required')
        encoded = _json(evidence)
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM migration_run WHERE uuid=?', (run_uuid,)).fetchone()
            if row is None:
                raise MigrationRunError('Unknown migration run')
            if row['state'] == target:
                previous = db.execute('SELECT * FROM migration_run_event WHERE run_uuid=? AND event_no=?',
                                      (run_uuid, row['event_no'])).fetchone()
                if previous and previous['before_state'] == expected and previous['evidence_json'] == encoded:
                    return self._record(row)
                raise MigrationRunError('Checkpoint retry differs from committed evidence')
            if row['state'] != expected:
                raise MigrationRunError('Migration checkpoint changed; reread before continuing')
            allowed = {row['checkpoint']} if expected == 'failed' else NEXT.get(expected, set())
            if target not in allowed:
                raise MigrationRunError('Invalid migration transition')
            number = row['event_no'] + 1
            checkpoint = row['checkpoint'] if target == 'failed' else target
            db.execute('UPDATE migration_run SET state=?,checkpoint=?,event_no=? WHERE uuid=? AND state=?',
                       (target, checkpoint, number, run_uuid, expected))
            db.execute('INSERT INTO migration_run_event VALUES(?,?,?,?,?)',
                       (run_uuid, number, expected, target, encoded))
            return {'uuid': run_uuid, 'state': target, 'checkpoint': checkpoint, 'event_no': number}

    def latest_evidence(self, run_uuid):
        with self._connection() as db:
            row = db.execute('SELECT e.evidence_json FROM migration_run_event e JOIN migration_run r '
                             'ON r.uuid=e.run_uuid AND r.event_no=e.event_no WHERE r.uuid=?', (run_uuid,)).fetchone()
            return json.loads(row['evidence_json']) if row else None

    @staticmethod
    def _record(row):
        return {key: row[key] for key in ('uuid', 'state', 'checkpoint', 'event_no')}
