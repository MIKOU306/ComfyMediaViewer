"""Versioned generation for query-visible safety labels."""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path


class SafetyQueryStateError(RuntimeError):
    pass


def _trigger(table, action):
    name = f'safety_query_epoch_{table}_{action.lower()}'
    sql = (f'CREATE TRIGGER {name} AFTER {action} ON {table} '
           'BEGIN UPDATE safety_query_state SET serial=serial+1 WHERE singleton=1; END')
    return name, sql


TRIGGERS = dict(_trigger(table, action) for table in ('safety_manual', 'safety_model')
                for action in ('INSERT', 'UPDATE', 'DELETE'))
CHECKSUM = hashlib.sha256(('1\n' + '\n'.join(TRIGGERS.values())).encode()).hexdigest()


class SafetyQueryState:
    def __init__(self, database):
        self.database = Path(database).absolute()

    @classmethod
    def initialize(cls, database):
        """Explicitly install generation tracking after safety tables exist."""
        path = Path(database).absolute()
        if path.is_symlink() or not path.is_file():
            raise SafetyQueryStateError('Existing regular catalog required')
        for parent in path.parents:
            if parent.is_symlink() or (getattr(parent.stat(), 'st_file_attributes', 0) & 0x400):
                raise SafetyQueryStateError('Catalog ancestors cannot be links')
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            required = ('safety_manual', 'safety_model')
            for table in required:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    raise SafetyQueryStateError('Initialize the safety schema first')
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='safety_query_schema'").fetchone()
            if exists:
                cls.validate(db)
            else:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'safety_query_%'").fetchone():
                    raise SafetyQueryStateError('Partial unversioned safety query schema')
                db.execute('CREATE TABLE safety_query_schema(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)')
                db.execute('CREATE TABLE safety_query_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch TEXT NOT NULL, serial INTEGER NOT NULL CHECK(serial>=0))')
                db.execute('INSERT INTO safety_query_state VALUES(1,?,0)', (str(uuid.uuid4()),))
                for sql in TRIGGERS.values():
                    db.execute(sql)
                db.execute('INSERT INTO safety_query_schema VALUES(1,?)', (CHECKSUM,))
                cls.validate(db)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return cls(path)

    @staticmethod
    def validate(connection):
        try:
            row = connection.execute('SELECT version,checksum FROM safety_query_schema').fetchall()
            if [tuple(item) for item in row] != [(1, CHECKSUM)]:
                raise SafetyQueryStateError('Safety query schema/checksum mismatch')
            for name, sql in TRIGGERS.items():
                actual = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
                if actual is None or actual['sql'] != sql:
                    raise SafetyQueryStateError('Safety query generation trigger missing or changed')
            state = connection.execute('SELECT epoch,serial FROM safety_query_state WHERE singleton=1').fetchone()
            if state is None or str(uuid.UUID(state['epoch'])) != state['epoch'] or state['serial'] < 0:
                raise SafetyQueryStateError('Safety query generation state invalid')
            return f"{state['epoch']}:{state['serial']}"
        except (sqlite3.Error, ValueError) as error:
            if isinstance(error, SafetyQueryStateError):
                raise
            raise SafetyQueryStateError('Safety query generation is not initialized or invalid') from error

    @classmethod
    def read_in_transaction(cls, connection, model_id, threshold):
        generation = cls.validate(connection)
        signature = hashlib.sha256(json.dumps(
            [model_id, float(threshold)], ensure_ascii=False, separators=(',', ':'), allow_nan=False
        ).encode()).hexdigest()
        return f'{generation}:cfg:{signature}'
