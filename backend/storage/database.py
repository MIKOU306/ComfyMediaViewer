"""Connection and versioned-schema management for the local catalog."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path

from .schema import SCHEMA_VERSION, schema_sql


class NewerSchemaError(RuntimeError):
    """The database was created by a program with a newer schema."""


class SchemaChecksumError(RuntimeError):
    """An applied migration no longer matches its recorded checksum."""


def new_identity() -> str:
    """Return a random UUID string for a durable library or media identity."""
    return str(uuid.uuid4())


def revision_token(revision_id: str) -> str:
    """Build the opaque public token; content hashes remain a separate field."""
    return f"rev:{revision_id}"


def connect(database: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open one SQLite connection with per-connection FK and busy handling."""
    connection = sqlite3.connect(str(database), timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
    return connection


def _migration_checksum(version: int, sql: str) -> str:
    payload = f"{version}\n{sql}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def initialize_schema(connection: sqlite3.Connection) -> int:
    """Create or validate the catalog schema; never downgrade or rewrite history."""
    if not connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("SQLite foreign key enforcement must be enabled per connection")

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        user_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if user_tables:
            raise RuntimeError("Database has tables but no schema_migrations; refusing to adopt it")
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        connection.commit()

    rows = connection.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    current = 0
    for row in rows:
        version = int(row["version"])
        if version > SCHEMA_VERSION:
            raise NewerSchemaError(
                f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}"
            )
        if version != current + 1:
            raise RuntimeError(f"Non-contiguous schema history at version {version}")
        if version == 1 and row["checksum"] != _migration_checksum(1, schema_sql()):
            raise SchemaChecksumError("Applied schema migration checksum does not match this program")
        current = version

    if current < SCHEMA_VERSION:
        if current != 0:
            raise RuntimeError("No migration path is defined for this schema version")
        sql = schema_sql()
        checksum = _migration_checksum(1, sql)
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + sql +
                "\nINSERT INTO schema_migrations(version, checksum) VALUES "
                f"(1, '{checksum}');\nCOMMIT;"
            )
        except Exception:
            connection.rollback()
            raise
    return SCHEMA_VERSION
