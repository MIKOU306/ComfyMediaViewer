"""Immutable, library-scoped workflow identity projections."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import nullcontext
from typing import Any

from .workflow import WorkflowArchive, WorkflowArchiveSchemaError, WorkflowIdentityError
from .workflow_identity import analyze_workflow_identity

SCHEMA_VERSION = 1
LATEST_SCHEMA_VERSION = 2
_CANDIDATE_INDEX = "CREATE INDEX workflow_definition_candidate_idx ON workflow_definition_version(library_id,algorithm,adapter_version,graph_format,structure_hash,id)"
_CANDIDATE_CHECKSUM = hashlib.sha256(("2\n" + _CANDIDATE_INDEX).encode()).hexdigest()


def _schema_sql() -> tuple[str, ...]:
    return (
        "CREATE UNIQUE INDEX workflow_capture_identity_scope_idx ON workflow_capture(library_id,id,workflow_id)",
        """CREATE TABLE workflow_identity_schema_migrations(
            version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE workflow_definition_version(
            id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, library_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL, algorithm TEXT NOT NULL, adapter_version TEXT NOT NULL,
            graph_format TEXT NOT NULL, definition_hash TEXT NOT NULL, definition_json TEXT NOT NULL,
            structure_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(library_id,id,workflow_id),
            UNIQUE(library_id,workflow_id,algorithm,adapter_version,graph_format,definition_hash),
            FOREIGN KEY(library_id,workflow_id) REFERENCES workflow_family(library_id,id) ON DELETE RESTRICT)""",
        """CREATE TABLE workflow_capture_identity(
            id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, library_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL, capture_id INTEGER NOT NULL, version_id INTEGER NOT NULL,
            algorithm TEXT NOT NULL, adapter_version TEXT NOT NULL, graph_format TEXT NOT NULL,
            status TEXT NOT NULL, coverage TEXT NOT NULL, execution_hash TEXT, bindings_json TEXT NOT NULL,
            result_json TEXT NOT NULL, definition_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(library_id,capture_id,algorithm,adapter_version),
            FOREIGN KEY(library_id,capture_id,workflow_id)
                REFERENCES workflow_capture(library_id,id,workflow_id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,version_id,workflow_id)
                REFERENCES workflow_version(library_id,id,workflow_id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,definition_id,workflow_id)
                REFERENCES workflow_definition_version(library_id,id,workflow_id) ON DELETE RESTRICT)""",
        "CREATE INDEX workflow_identity_family_page_idx ON workflow_definition_version(library_id,workflow_id,id)",
        """CREATE TRIGGER workflow_definition_version_no_update BEFORE UPDATE ON workflow_definition_version
            BEGIN SELECT RAISE(ABORT,'workflow definitions are immutable'); END""",
        """CREATE TRIGGER workflow_definition_version_no_delete BEFORE DELETE ON workflow_definition_version
            BEGIN SELECT RAISE(ABORT,'workflow definitions are immutable'); END""",
        """CREATE TRIGGER workflow_capture_identity_no_update BEFORE UPDATE ON workflow_capture_identity
            BEGIN SELECT RAISE(ABORT,'workflow capture identities are immutable'); END""",
        """CREATE TRIGGER workflow_capture_identity_no_delete BEFORE DELETE ON workflow_capture_identity
            BEGIN SELECT RAISE(ABORT,'workflow capture identities are immutable'); END""",
    )


def _schema_checksum() -> str:
    return hashlib.sha256(f"{SCHEMA_VERSION}\n{';'.join(_schema_sql())}".encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class WorkflowIdentityStoreError(RuntimeError):
    pass


class WorkflowIdentityStoreSchemaError(WorkflowIdentityStoreError):
    pass


class WorkflowIdentityStoreConflict(WorkflowIdentityStoreError):
    pass


class WorkflowIdentityStore:
    """Store immutable capture projections and family-scoped definitions."""

    def __init__(self, database: str):
        self.archive = WorkflowArchive(database)

    @staticmethod
    def _validate(connection: sqlite3.Connection, *, allow_v1: bool = False) -> None:
        rows = connection.execute("SELECT version,checksum FROM workflow_identity_schema_migrations ORDER BY version").fetchall()
        expected = [(1, _schema_checksum()), (2, _CANDIDATE_CHECKSUM)]
        actual = [(row["version"],row["checksum"]) for row in rows]
        if actual != expected and not (allow_v1 and actual == expected[:1]):
            raise WorkflowIdentityStoreSchemaError("Workflow identity schema version/checksum mismatch")
        for name in ("workflow_definition_version", "workflow_capture_identity"):
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
                raise WorkflowIdentityStoreSchemaError(f"Workflow identity table is missing: {name}")
        if len(rows) == 2 and not connection.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='workflow_definition_candidate_idx'").fetchone():
            raise WorkflowIdentityStoreSchemaError("Workflow candidate index is missing")

    def initialize(self) -> int:
        with self.archive._transaction(write=True) as connection:
            present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_identity_schema_migrations'").fetchone()
            if present:
                self._validate(connection, allow_v1=True)
            else:
                for statement in _schema_sql():
                    connection.execute(statement)
                connection.execute("INSERT INTO workflow_identity_schema_migrations(version,checksum) VALUES (?,?)",
                                   (SCHEMA_VERSION, _schema_checksum()))
            if not connection.execute("SELECT 1 FROM workflow_identity_schema_migrations WHERE version=2").fetchone():
                connection.execute(_CANDIDATE_INDEX)
                connection.execute("INSERT INTO workflow_identity_schema_migrations(version,checksum) VALUES (2,?)",
                                   (_CANDIDATE_CHECKSUM,))
            self._validate(connection)
        return LATEST_SCHEMA_VERSION

    def publish_capture(self, library_uuid: str, capture_uuid: str, *,
                        _connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        transaction = self.archive._transaction(write=True) if _connection is None else nullcontext(_connection)
        with transaction as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            row = connection.execute(
                "SELECT c.id,c.uuid,c.library_id,c.workflow_id,c.version_id,c.source_format,c.raw_sha256,c.raw_payload,"
                "f.uuid workflow_uuid,v.uuid version_uuid FROM workflow_capture c "
                "JOIN workflow_family f ON f.id=c.workflow_id AND f.library_id=c.library_id "
                "JOIN workflow_version v ON v.id=c.version_id AND v.library_id=c.library_id "
                "WHERE c.uuid=? AND c.library_id=?", (capture_uuid, library["id"])).fetchone()
            if row is None:
                raise WorkflowIdentityError("Unknown capture UUID in library")
            raw = bytes(row["raw_payload"])
            if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
                raise WorkflowIdentityStoreConflict("Stored workflow capture hash does not match raw payload")
            analysis = analyze_workflow_identity(raw, source_format=row["source_format"])
            algorithm, adapter = analysis["algorithm"], analysis["adapter_version"]
            existing = connection.execute(
                "SELECT ci.*,d.uuid definition_uuid,d.definition_hash,d.structure_hash FROM workflow_capture_identity ci "
                "LEFT JOIN workflow_definition_version d ON d.id=ci.definition_id AND d.library_id=ci.library_id "
                "WHERE ci.library_id=? AND ci.capture_id=? AND ci.algorithm=? AND ci.adapter_version=?",
                (library["id"], row["id"], algorithm, adapter)).fetchone()
            if existing:
                if existing["result_json"] != _json(analysis):
                    raise WorkflowIdentityStoreConflict("Identity projection already exists with different immutable result")
                return self._record(existing, row)

            definition_id = None
            if analysis.get("status") == "comparable" and analysis.get("definition_hash"):
                # Keep the identity contract separate from runtime bindings. The complete result remains on capture identity.
                definition = {"definition_graph": analysis.get("definition_graph"),
                              "definition_bindings": analysis.get("definition_bindings", [])}
                definition_json = _json(definition)
                key = (library["id"], row["workflow_id"], algorithm, adapter,
                       analysis["graph_format"], analysis["definition_hash"])
                connection.execute(
                    "INSERT OR IGNORE INTO workflow_definition_version(uuid,library_id,workflow_id,algorithm,adapter_version,"
                    "graph_format,definition_hash,definition_json,structure_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), *key, definition_json, analysis["structure_hash"]))
                definition_row = connection.execute(
                    "SELECT id,definition_json,structure_hash FROM workflow_definition_version WHERE library_id=? AND workflow_id=? "
                    "AND algorithm=? AND adapter_version=? AND graph_format=? AND definition_hash=?", key).fetchone()
                if (definition_row["definition_json"] != definition_json or
                        definition_row["structure_hash"] != analysis["structure_hash"]):
                    raise WorkflowIdentityStoreConflict("Definition hash collision has different canonical definition")
                definition_id = definition_row["id"]
            identity_uuid = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO workflow_capture_identity(uuid,library_id,workflow_id,capture_id,version_id,algorithm,adapter_version,"
                "graph_format,status,coverage,execution_hash,bindings_json,result_json,definition_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity_uuid, library["id"], row["workflow_id"], row["id"], row["version_id"], algorithm, adapter,
                 analysis["graph_format"], analysis["status"], analysis["coverage"], analysis.get("execution_hash"),
                 _json(analysis.get("bindings", [])), _json(analysis), definition_id))
            saved = connection.execute(
                "SELECT ci.*,d.uuid definition_uuid,d.definition_hash,d.structure_hash FROM workflow_capture_identity ci "
                "LEFT JOIN workflow_definition_version d ON d.id=ci.definition_id AND d.library_id=ci.library_id "
                "WHERE ci.uuid=?", (identity_uuid,)).fetchone()
            return {"status": saved["status"], "captureIdentityUUID": saved["uuid"],
                    "workflowUUID": row["workflow_uuid"], "versionUUID": row["version_uuid"],
                    "definitionUUID": saved["definition_uuid"], "algorithm": algorithm,
                    "adapterVersion": adapter, "executionHash": saved["execution_hash"],
                    "definitionHash": saved["definition_hash"], "structureHash": saved["structure_hash"]}

    @staticmethod
    def _record(identity: sqlite3.Row, capture: sqlite3.Row) -> dict[str, Any]:
        definition_uuid = identity["definition_uuid"] if "definition_uuid" in identity.keys() else None
        return {"status": identity["status"], "captureIdentityUUID": identity["uuid"],
                "workflowUUID": capture["workflow_uuid"], "versionUUID": capture["version_uuid"],
                "definitionUUID": definition_uuid, "algorithm": identity["algorithm"],
                "adapterVersion": identity["adapter_version"], "executionHash": identity["execution_hash"],
                "definitionHash": identity["definition_hash"] if "definition_hash" in identity.keys() else None,
                "structureHash": identity["structure_hash"] if "structure_hash" in identity.keys() else None}

    def read_capture_identity(self, library_uuid: str, capture_uuid: str, *,
                              algorithm: str | None = None, adapter_version: str | None = None) -> dict[str, Any]:
        with self.archive._transaction(write=False) as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            params: list[Any] = [library["id"], capture_uuid]
            sql = ("SELECT ci.*,c.uuid capture_uuid,f.uuid workflow_uuid,v.uuid version_uuid,d.uuid definition_uuid,"
                   "d.definition_hash,d.structure_hash FROM workflow_capture_identity ci "
                   "JOIN workflow_capture c ON c.id=ci.capture_id AND c.library_id=ci.library_id "
                   "JOIN workflow_family f ON f.id=ci.workflow_id AND f.library_id=ci.library_id "
                   "JOIN workflow_version v ON v.id=ci.version_id AND v.library_id=ci.library_id "
                   "LEFT JOIN workflow_definition_version d ON d.id=ci.definition_id AND d.library_id=ci.library_id "
                   "WHERE ci.library_id=? AND c.uuid=?")
            if algorithm is not None:
                sql += " AND ci.algorithm=?"; params.append(algorithm)
            if adapter_version is not None:
                sql += " AND ci.adapter_version=?"; params.append(adapter_version)
            row = connection.execute(sql + " ORDER BY ci.id DESC LIMIT 1", params).fetchone()
            if row is None:
                raise WorkflowIdentityError("Workflow capture identity not found in library")
            result = dict(row)
            result["result"] = json.loads(result.pop("result_json"))
            result["bindings"] = json.loads(result.pop("bindings_json"))
            return result

    def list_definitions(self, library_uuid: str, workflow_uuid: str, *, after_id: int = 0,
                         limit: int = 50) -> dict[str, Any]:
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("after_id must be nonnegative and limit must be between 1 and 100")
        with self.archive._transaction(write=False) as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            family = connection.execute("SELECT id FROM workflow_family WHERE uuid=? AND library_id=?",
                                        (workflow_uuid, library["id"])).fetchone()
            if family is None:
                raise WorkflowIdentityError("Unknown workflow UUID in library")
            rows = connection.execute(
                "SELECT id,uuid,algorithm,adapter_version,graph_format,definition_hash,structure_hash,created_at "
                "FROM workflow_definition_version WHERE library_id=? AND workflow_id=? AND id>? ORDER BY id LIMIT ?",
                (library["id"], family["id"], after_id, limit + 1)).fetchall()
            has_more = len(rows) > limit
            items = []
            for row in rows[:limit]:
                items.append(dict(row))
            return {"items": items, "nextAfterId": items[-1]["id"] if has_more and items else None}

    def read_definition(self, library_uuid: str, definition_uuid: str) -> dict[str, Any]:
        with self.archive._transaction(write=False) as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            row = connection.execute(
                "SELECT d.*,f.uuid workflow_uuid FROM workflow_definition_version d "
                "JOIN workflow_family f ON f.id=d.workflow_id AND f.library_id=d.library_id "
                "WHERE d.library_id=? AND d.uuid=?", (library["id"], definition_uuid)).fetchone()
            if row is None:
                raise WorkflowIdentityError("Workflow definition not found in library")
            result = dict(row)
            result["definition"] = json.loads(result.pop("definition_json"))
            return result
