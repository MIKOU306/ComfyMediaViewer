"""Persistent query generation, advanced in the same transaction as catalog writes."""
from backend.storage.integrity import foreign_key_violation
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.storage.database import initialize_schema


class QueryIndexError(RuntimeError):
    pass


TABLES = ('library', 'library_root', 'local_profile', 'media_asset', 'media_revision',
          'user_media_state', 'collection', 'collection_item', 'tag', 'tag_assignment',
          'generation_asset_current', 'generation_revision_current',
          'generation_prompt_binding', 'generation_model_reference', 'generation_model_usage')


def _trigger_sql(table, action):
    name = f'query_epoch_{table}_{action.lower()}'
    return name, (f'CREATE TRIGGER {name} AFTER {action} ON {table} '
                  'BEGIN UPDATE query_index_state SET serial=serial+1 WHERE singleton=1; END')


TRIGGERS = dict(_trigger_sql(table, action) for table in TABLES
                for action in ('INSERT', 'UPDATE', 'DELETE'))
CHECKSUM = hashlib.sha256(('1\n' + '\n'.join(TRIGGERS.values())).encode()).hexdigest()
STEP_TRIGGERS = dict(_trigger_sql('generation_step', action) for action in ('INSERT', 'UPDATE', 'DELETE'))
STEP_CHECKSUM = hashlib.sha256(('2\n' + '\n'.join(STEP_TRIGGERS.values())).encode()).hexdigest()
SNAPSHOT_TRIGGERS = dict(_trigger_sql('metadata_snapshot', action) for action in ('INSERT', 'UPDATE', 'DELETE'))
SNAPSHOT_CHECKSUM = hashlib.sha256(('3\n' + '\n'.join(SNAPSHOT_TRIGGERS.values())).encode()).hexdigest()
HISTORY = [(1, CHECKSUM), (2, STEP_CHECKSUM), (3, SNAPSHOT_CHECKSUM)]
REVISION_UPDATE_NAME = 'query_epoch_media_revision_update'
REVISION_UPDATE_SQL = ('CREATE TRIGGER ' + REVISION_UPDATE_NAME + ' AFTER UPDATE ON media_revision WHEN '
    + ' OR '.join('OLD.' + field + ' IS NOT NEW.' + field for field in
                 ('id', 'uuid', 'library_id', 'asset_id', 'size_bytes', 'mtime_ns', 'created_at'))
    + ' BEGIN UPDATE query_index_state SET serial=serial+1 WHERE singleton=1; END')
VERIFICATION_SQL = ('CREATE TRIGGER query_verification_update AFTER UPDATE ON media_revision '
    'WHEN OLD.verification_state IS NOT NEW.verification_state OR OLD.content_hash IS NOT NEW.content_hash '
    'BEGIN UPDATE query_verification_state SET serial=serial+1 WHERE singleton=1; END')
VERIFICATION_TABLE = ('CREATE TABLE query_verification_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
                      'serial INTEGER NOT NULL CHECK(serial>=0))')
VERIFICATION_CHECKSUM = hashlib.sha256(('4\n' + VERIFICATION_TABLE + '\n' + REVISION_UPDATE_SQL + '\n' + VERIFICATION_SQL).encode()).hexdigest()
HISTORY.append((4, VERIFICATION_CHECKSUM))


def _install_verification_generation(connection):
    connection.execute('DROP TRIGGER ' + REVISION_UPDATE_NAME)
    connection.execute(REVISION_UPDATE_SQL)
    connection.execute(VERIFICATION_TABLE)
    connection.execute('INSERT INTO query_verification_state VALUES(1,0)')
    connection.execute(VERIFICATION_SQL)
    connection.execute('INSERT INTO query_schema_migrations VALUES(4,?)', (VERIFICATION_CHECKSUM,))
    connection.execute('UPDATE query_index_state SET epoch=?,serial=0 WHERE singleton=1', (str(uuid.uuid4()),))


# 人工内容安全判定写的是独立的 safety_manual 表，它同样改变安全筛选的可见结果：
# 不推进代际的话，写入前签发的游标会继续被接受，分页可能混用写入前后的可见性。
SAFETY_TRIGGERS = dict(_trigger_sql('safety_manual', action) for action in ('INSERT', 'UPDATE', 'DELETE'))
SAFETY_CHECKSUM = hashlib.sha256(('5\n' + '\n'.join(SAFETY_TRIGGERS.values())).encode()).hexdigest()
HISTORY.append((5, SAFETY_CHECKSUM))


def _has_safety_table(connection):
    """safety_manual 由安全存储按需创建；没有它的库不安装、也不要求安全代际触发器。"""
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='safety_manual'"
                              ).fetchone() is not None


def _install_safety_generation(connection):
    for sql in SAFETY_TRIGGERS.values():
        connection.execute(sql)
    connection.execute('INSERT INTO query_schema_migrations VALUES(5,?)', (SAFETY_CHECKSUM,))
    connection.execute('UPDATE query_index_state SET epoch=?,serial=0 WHERE singleton=1', (str(uuid.uuid4()),))


class QueryIndexState:
    def __init__(self, database):
        self.database = Path(database).absolute()

    @contextmanager
    def _connection(self, write=False):
        if self.database.is_symlink() or not self.database.is_file():
            raise QueryIndexError('Existing regular catalog required')
        for parent in self.database.parents:
            if parent.is_symlink() or (getattr(parent.stat(), 'st_file_attributes', 0) & 0x400):
                raise QueryIndexError('Catalog ancestors cannot be links')
        connection = sqlite3.connect(self.database.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if foreign_key_violation(connection):
                raise QueryIndexError('Catalog foreign key violations')
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
                raise QueryIndexError('Initialized core catalog required')
            initialize_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def validate(connection, *, allow_legacy=False):
        try:
            rows = connection.execute('SELECT version,checksum FROM query_schema_migrations ORDER BY version').fetchall()
            history = [tuple(row) for row in rows]
            expected = HISTORY if _has_safety_table(connection) else HISTORY[:4]
            if history != expected and not (allow_legacy and
                    history in tuple(HISTORY[:n] for n in range(1, len(HISTORY) + 1))):
                raise QueryIndexError('Query schema version/checksum mismatch')
            required = dict(TRIGGERS)
            if len(history) >= 2:
                required.update(STEP_TRIGGERS)
            if len(history) >= 3:
                required.update(SNAPSHOT_TRIGGERS)
            if len(history) >= 4:
                required[REVISION_UPDATE_NAME] = REVISION_UPDATE_SQL
                required['query_verification_update'] = VERIFICATION_SQL
                verification = connection.execute('SELECT serial FROM query_verification_state WHERE singleton=1').fetchone()
                if verification is None or verification['serial'] < 0:
                    raise QueryIndexError('Query verification generation state invalid')
            if len(history) >= 5:
                required.update(SAFETY_TRIGGERS)
            for name, sql in required.items():
                row = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                                         (name,)).fetchone()
                if row is None or row['sql'] != sql:
                    raise QueryIndexError('Query generation trigger missing or changed')
            row = connection.execute('SELECT epoch,serial FROM query_index_state WHERE singleton=1').fetchone()
            if row is None or str(uuid.UUID(row['epoch'])) != row['epoch'] or row['serial'] < 0:
                raise QueryIndexError('Query generation state invalid')
            return len(history)
        except (sqlite3.Error, ValueError) as error:
            raise QueryIndexError('Query generation is not initialized or is invalid') from error

    def initialize(self):
        with self._connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='query_schema_migrations'").fetchone():
                version = self.validate(connection, allow_legacy=True)
                if version == 1:
                    for sql in STEP_TRIGGERS.values():
                        connection.execute(sql)
                    connection.execute('INSERT INTO query_schema_migrations VALUES(2,?)', (STEP_CHECKSUM,))
                    connection.execute('UPDATE query_index_state SET epoch=?,serial=0 WHERE singleton=1', (str(uuid.uuid4()),))
                if version < 3:
                    for sql in SNAPSHOT_TRIGGERS.values():
                        connection.execute(sql)
                    connection.execute('INSERT INTO query_schema_migrations VALUES(3,?)', (SNAPSHOT_CHECKSUM,))
                    connection.execute('UPDATE query_index_state SET epoch=?,serial=0 WHERE singleton=1', (str(uuid.uuid4()),))
                if version < 4:
                    _install_verification_generation(connection)
                if version < 5 and _has_safety_table(connection):
                    _install_safety_generation(connection)
                self.validate(connection)
                return
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'query_%'").fetchone():
                raise QueryIndexError('Partial unversioned query schema')
            for table in (*TABLES, 'generation_step', 'metadata_snapshot'):
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                          (table,)).fetchone():
                    raise QueryIndexError('Generation and core schemas must be initialized first')
            connection.execute('CREATE TABLE query_schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)')
            connection.execute('CREATE TABLE query_index_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
                               'epoch TEXT NOT NULL,serial INTEGER NOT NULL CHECK(serial>=0))')
            connection.execute('INSERT INTO query_index_state VALUES(1,?,0)', (str(uuid.uuid4()),))
            for sql in TRIGGERS.values():
                connection.execute(sql)
            connection.execute('INSERT INTO query_schema_migrations VALUES(1,?)', (CHECKSUM,))
            for sql in STEP_TRIGGERS.values():
                connection.execute(sql)
            connection.execute('INSERT INTO query_schema_migrations VALUES(2,?)', (STEP_CHECKSUM,))
            for sql in SNAPSHOT_TRIGGERS.values():
                connection.execute(sql)
            connection.execute('INSERT INTO query_schema_migrations VALUES(3,?)', (SNAPSHOT_CHECKSUM,))
            _install_verification_generation(connection)
            if _has_safety_table(connection):
                _install_safety_generation(connection)
            self.validate(connection)

    @staticmethod
    def read_in_transaction(connection):
        """Use the same read transaction as filtering and page selection."""
        QueryIndexState.validate(connection)
        row = connection.execute('SELECT epoch,serial FROM query_index_state WHERE singleton=1').fetchone()
        return {'indexGeneration': f"{row['epoch']}:{row['serial']}",
                'coverage': 'partial', 'countState': 'indexed_only'}

    def read(self):
        with self._connection() as connection:
            return self.read_in_transaction(connection)
