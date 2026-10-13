"""Durable workflow classification projections attached to metadata snapshots.

This store persists the compact classification result used by consumers. It is
independent from the normalized workflow-family/generation parser.
"""

from __future__ import annotations

from backend.storage.integrity import foreign_key_violation
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from ..storage.database import initialize_schema
from .archive import MetadataArchive


WORKFLOW_PROJECTION_SCHEMA_VERSION = 1
_SCHEMA_SQL = """
CREATE TABLE workflow_classification_projection (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    projection_version TEXT NOT NULL CHECK (length(projection_version) BETWEEN 1 AND 128),
    classification TEXT NOT NULL CHECK (classification IN ('full','prompt','none','unknown')),
    kind TEXT NOT NULL CHECK (kind IN ('t2i','i2i','cn','other','unknown')),
    evidence_json TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (snapshot_id, projection_version),
    UNIQUE (library_id, asset_id, revision_id, id),
    FOREIGN KEY (library_id, asset_id, revision_id)
        REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (snapshot_id) REFERENCES metadata_snapshot(id) ON DELETE RESTRICT
);
CREATE TRIGGER workflow_projection_snapshot_scope_insert
BEFORE INSERT ON workflow_classification_projection
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM metadata_snapshot s
        WHERE s.id=NEW.snapshot_id AND s.library_id=NEW.library_id
          AND s.asset_id=NEW.asset_id AND s.revision_id=NEW.revision_id
    ) THEN RAISE(ABORT, 'workflow projection snapshot scope mismatch') END;
END;
CREATE TRIGGER workflow_projection_immutable_update
BEFORE UPDATE ON workflow_classification_projection
BEGIN
    SELECT RAISE(ABORT, 'workflow projections are immutable');
END;
CREATE TRIGGER workflow_projection_immutable_delete
BEFORE DELETE ON workflow_classification_projection
BEGIN
    SELECT RAISE(ABORT, 'workflow projections are immutable');
END;
CREATE TABLE workflow_projection_generation (
    id INTEGER PRIMARY KEY CHECK (id=1),
    serial INTEGER NOT NULL CHECK (serial >= 0)
);
INSERT INTO workflow_projection_generation(id,serial) VALUES (1,0);
"""


def _checksum() -> str:
    return hashlib.sha256(f"{WORKFLOW_PROJECTION_SCHEMA_VERSION}\n{_SCHEMA_SQL}".encode()).hexdigest()


class WorkflowProjectionError(RuntimeError):
    """Base error for projection persistence."""


class WorkflowProjectionUnavailableError(WorkflowProjectionError):
    """The initialized catalog cannot be opened safely."""


class WorkflowProjectionSchemaError(WorkflowProjectionError):
    """The required catalog, archive, or projection schema is invalid."""


class WorkflowProjectionConflictError(WorkflowProjectionError):
    """A snapshot/version already has a different immutable projection."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _validated(projection: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    if not isinstance(projection, Mapping):
        raise ValueError("projection must be a mapping")
    version = projection.get("projectionVersion")
    classification = projection.get("classification")
    kind = projection.get("kind")
    evidence = projection.get("evidence")
    if not isinstance(version, str) or not version.strip() or len(version) > 128:
        raise ValueError("projectionVersion must contain 1 to 128 characters")
    if classification not in ("full", "prompt", "none", "unknown"):
        raise ValueError("classification must be full, prompt, none, or unknown")
    if kind not in ("t2i", "i2i", "cn", "other", "unknown"):
        raise ValueError("kind must be t2i, i2i, cn, other, or unknown")
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    evidence_json = _json(evidence)
    normalized = _json({"projectionVersion": version, "classification": classification,
                        "kind": kind, "evidence": evidence})
    return version, classification, kind, evidence_json, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class WorkflowProjectionStore:
    def __init__(self, database: str | Path):
        self.database = Path(database).absolute()

    @contextmanager
    def _transaction(self, *, write: bool, require_schema: bool = True) -> Iterator[sqlite3.Connection]:
        if self.database.is_symlink():
            raise WorkflowProjectionUnavailableError("Catalog path must not be a symlink")
        try:
            real = self.database.resolve(strict=True)
            if not real.is_file():
                raise WorkflowProjectionUnavailableError("Catalog database does not exist")
            conn = sqlite3.connect(real.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                   uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, WorkflowProjectionError):
                raise
            raise WorkflowProjectionUnavailableError(f"Cannot open projection store: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if foreign_key_violation(conn) is not None:
                raise WorkflowProjectionSchemaError("Database has foreign key violations")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() \
                    or not conn.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise WorkflowProjectionSchemaError("Core catalog is not initialized")
            initialize_schema(conn)
            MetadataArchive._validate_archive_schema(conn)
            if require_schema:
                self._validate_schema(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_projection_schema_migrations'").fetchone():
            raise WorkflowProjectionSchemaError("Workflow projection store is not initialized")
        rows = conn.execute("SELECT version,checksum FROM workflow_projection_schema_migrations ORDER BY version").fetchall()
        if len(rows) != 1 or rows[0]["version"] != WORKFLOW_PROJECTION_SCHEMA_VERSION or rows[0]["checksum"] != _checksum():
            raise WorkflowProjectionSchemaError("Workflow projection migration checksum/version mismatch")

    def initialize(self) -> int:
        with self._transaction(write=True, require_schema=False) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_projection_schema_migrations'").fetchone()
            if exists:
                self._validate_schema(conn)
                return WORKFLOW_PROJECTION_SCHEMA_VERSION
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'workflow_projection_%' OR name='workflow_classification_projection'").fetchone():
                raise WorkflowProjectionSchemaError("Partial unversioned workflow projection schema exists")
            try:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    "CREATE TABLE workflow_projection_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));\n" + _SCHEMA_SQL +
                    f"\nINSERT INTO workflow_projection_schema_migrations(version,checksum) VALUES ({WORKFLOW_PROJECTION_SCHEMA_VERSION},'{_checksum()}');\nCOMMIT;"
                )
            except Exception:
                conn.rollback()
                raise
        return WORKFLOW_PROJECTION_SCHEMA_VERSION

    @staticmethod
    def read_generation_in_transaction(conn: sqlite3.Connection) -> int:
        """Read the initialized generation cursor inside the caller's transaction."""
        row = conn.execute("SELECT serial FROM workflow_projection_generation WHERE id=1").fetchone()
        if row is None:
            raise WorkflowProjectionSchemaError("Workflow projection generation cursor is missing")
        return int(row[0])

    def publish(self, snapshot_uuid: str, projection: Mapping[str, Any]) -> dict[str, Any]:
        version, classification, kind, evidence_json, digest = _validated(projection)
        with self._transaction(write=True) as conn:
            snapshot = conn.execute(
                "SELECT id,library_id,asset_id,revision_id FROM metadata_snapshot WHERE uuid=?",
                (snapshot_uuid,)).fetchone()
            if snapshot is None:
                raise WorkflowProjectionError("Metadata snapshot does not exist")
            old = conn.execute("SELECT uuid,result_sha256 FROM workflow_classification_projection WHERE snapshot_id=? AND projection_version=?",
                               (snapshot["id"], version)).fetchone()
            if old:
                if old["result_sha256"] != digest:
                    raise WorkflowProjectionConflictError("Snapshot already has different content for this projection version")
                return {"status": "idempotent", "projectionUUID": old["uuid"],
                        "snapshotUUID": snapshot_uuid, "projectionVersion": version}
            import uuid
            projection_uuid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO workflow_classification_projection(uuid,library_id,asset_id,revision_id,snapshot_id,"
                "projection_version,classification,kind,evidence_json,result_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (projection_uuid, snapshot["library_id"], snapshot["asset_id"], snapshot["revision_id"],
                 snapshot["id"], version, classification, kind, evidence_json, digest))
            conn.execute("UPDATE workflow_projection_generation SET serial=serial+1 WHERE id=1")
            return {"status": "published", "projectionUUID": projection_uuid,
                    "snapshotUUID": snapshot_uuid, "projectionVersion": version,
                    "generation": self.read_generation_in_transaction(conn)}

    def read_current(self, library_uuid: str, media_uuid: str,
                     projection_version: str) -> dict[str, Any] | None:
        """Read only the requested version on the latest snapshot of the current revision.

        A latest snapshot without this projection version returns None; older snapshots
        are never consulted as fallback.
        """
        with self._transaction(write=False) as conn:
            row = conn.execute(
                "WITH latest AS (SELECT s.id,s.uuid,s.library_id,s.asset_id,s.revision_id "
                "FROM metadata_snapshot s JOIN media_asset a ON a.library_id=s.library_id AND a.id=s.asset_id "
                "JOIN library l ON l.id=s.library_id WHERE l.uuid=? AND a.uuid=? "
                "AND s.revision_id=a.current_revision_id ORDER BY s.captured_at DESC,s.id DESC LIMIT 1) "
                "SELECT latest.uuid AS snapshot_uuid,p.uuid AS projection_uuid,p.projection_version,"
                "p.classification,p.kind,p.evidence_json,p.result_sha256 "
                "FROM latest LEFT JOIN workflow_classification_projection p ON p.snapshot_id=latest.id "
                "AND p.projection_version=?", (library_uuid, media_uuid, projection_version)).fetchone()
            if row is None or row["projection_uuid"] is None:
                return None
            return {"snapshotUUID": row["snapshot_uuid"], "projectionUUID": row["projection_uuid"],
                    "projectionVersion": row["projection_version"], "classification": row["classification"],
                    "kind": row["kind"], "evidence": json.loads(row["evidence_json"]),
                    "sha256": row["result_sha256"]}
