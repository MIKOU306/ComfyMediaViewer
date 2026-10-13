"""Explicit, immutable links from embedded metadata evidence to workflow captures."""
from __future__ import annotations

from contextlib import nullcontext
import codecs
import hashlib
import json
import sqlite3
import uuid
from typing import Any

from .workflow import WorkflowArchive
from .workflow_source_store import WorkflowSourceStore

SCHEMA_VERSION = 1
PAGE_SIZE_MAX = 100
EVIDENCE_KIND = "embedded_metadata_claim"
CONVERSION_PRESERVED = "source_text_bytes_preserved"
CONVERSION_TRANSCODED = "decoded_text_transcoded_to_utf8"


def _schema_sql() -> tuple[str, ...]:
    return (
        """CREATE TABLE workflow_source_binding_schema_migrations(
            version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE workflow_source_binding(
            id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
            source_id INTEGER NOT NULL, capture_id INTEGER NOT NULL,
            library_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, revision_id INTEGER NOT NULL,
            snapshot_id INTEGER NOT NULL, version_id INTEGER NOT NULL, workflow_id INTEGER NOT NULL,
            source_record_sha256 TEXT NOT NULL, imported_utf8_sha256 TEXT NOT NULL,
            conversion TEXT NOT NULL CHECK(conversion IN ('source_text_bytes_preserved','decoded_text_transcoded_to_utf8')),
            evidence_kind TEXT NOT NULL CHECK(evidence_kind='embedded_metadata_claim'),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(source_id,capture_id),
            FOREIGN KEY(source_id) REFERENCES workflow_source_evidence(id) ON DELETE RESTRICT,
            FOREIGN KEY(capture_id) REFERENCES workflow_capture(id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id) REFERENCES library(id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,asset_id,revision_id)
                REFERENCES media_revision(library_id,asset_id,id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,snapshot_id,asset_id,revision_id)
                REFERENCES metadata_snapshot(library_id,id,asset_id,revision_id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,version_id,workflow_id)
                REFERENCES workflow_version(library_id,id,workflow_id) ON DELETE RESTRICT)""",
        "CREATE INDEX workflow_source_binding_capture_idx ON workflow_source_binding(library_id,capture_id,id)",
        """CREATE TRIGGER workflow_source_binding_scope BEFORE INSERT ON workflow_source_binding
            BEGIN
              SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM workflow_source_evidence s WHERE s.id=NEW.source_id
                  AND s.library_id=NEW.library_id AND s.asset_id=NEW.asset_id
                  AND s.revision_id=NEW.revision_id AND s.snapshot_id=NEW.snapshot_id
                  AND s.record_sha256=NEW.source_record_sha256
              ) THEN RAISE(ABORT,'source binding evidence scope mismatch') END;
              SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM workflow_capture c WHERE c.id=NEW.capture_id
                  AND c.library_id=NEW.library_id AND c.version_id=NEW.version_id
                  AND c.workflow_id=NEW.workflow_id
              ) THEN RAISE(ABORT,'source binding capture scope mismatch') END;
            END""",
        """CREATE TRIGGER workflow_source_binding_no_update BEFORE UPDATE ON workflow_source_binding
            BEGIN SELECT RAISE(ABORT,'workflow source bindings are immutable'); END""",
        """CREATE TRIGGER workflow_source_binding_no_delete BEFORE DELETE ON workflow_source_binding
            BEGIN SELECT RAISE(ABORT,'workflow source bindings are immutable'); END""",
    )


def _checksum() -> str:
    return hashlib.sha256(f"{SCHEMA_VERSION}\n{';'.join(_schema_sql())}".encode("utf-8")).hexdigest()


class WorkflowSourceBindingError(RuntimeError):
    pass


class WorkflowSourceBindingSchemaError(WorkflowSourceBindingError):
    pass


class WorkflowSourceBindingConflict(WorkflowSourceBindingError):
    pass


class WorkflowSourceBindings:
    """Bind verified source text to the exact raw payload of a workflow capture."""

    def __init__(self, database: str):
        self.archive = WorkflowArchive(database)

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_source_binding_schema_migrations'").fetchone():
            raise WorkflowSourceBindingSchemaError("Workflow source bindings are not initialized")
        rows = connection.execute("SELECT version,checksum FROM workflow_source_binding_schema_migrations ORDER BY version").fetchall()
        if [(row["version"], row["checksum"]) for row in rows] != [(SCHEMA_VERSION, _checksum())]:
            raise WorkflowSourceBindingSchemaError("Workflow source binding migration version/checksum mismatch")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_source_binding'").fetchone():
            raise WorkflowSourceBindingSchemaError("Workflow source binding table is missing")

    def initialize(self) -> int:
        with self.archive._transaction(write=True, require_schema=False) as connection:
            # Initialization requires both independent source and workflow archives.
            WorkflowSourceStore._validate(connection)
            WorkflowArchive._validate_schema(connection)
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_source_binding_schema_migrations'").fetchone()
            if exists:
                self._validate(connection)
                return SCHEMA_VERSION
            partial = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_source_binding'"
            ).fetchone()
            if partial:
                raise WorkflowSourceBindingSchemaError("Partial workflow source binding schema exists")
            for statement in _schema_sql():
                connection.execute(statement)
            connection.execute("INSERT INTO workflow_source_binding_schema_migrations(version,checksum) VALUES (?,?)",
                               (SCHEMA_VERSION, _checksum()))
            self._validate(connection)
        return SCHEMA_VERSION

    def bind(self, library_uuid: str, source_uuid: str, capture_uuid: str, *,
             _connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        tx = self.archive._transaction(write=True) if _connection is None else nullcontext(_connection)
        with tx as connection:
            self._validate(connection)
            WorkflowSourceStore._validate(connection)
            library = self.archive._library(connection, library_uuid)
            source = connection.execute(
                "SELECT * FROM workflow_source_evidence WHERE uuid=? AND library_id=?",
                (source_uuid, library["id"])).fetchone()
            if source is None:
                raise WorkflowSourceBindingError("Unknown workflow source in selected library")
            record_json = source["record_json"]
            if hashlib.sha256(record_json.encode("utf-8")).hexdigest() != source["record_sha256"]:
                raise WorkflowSourceBindingConflict("Stored source record hash mismatch")
            try:
                record = json.loads(record_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise WorkflowSourceBindingConflict("Stored source record is invalid JSON") from exc
            locator_json = source["source_locator_json"]
            if hashlib.sha256(locator_json.encode("utf-8")).hexdigest() != source["locator_hash"]:
                raise WorkflowSourceBindingConflict("Stored source locator hash mismatch")
            if json.loads(locator_json) != record.get("source_locator"):
                raise WorkflowSourceBindingConflict("Stored source locator does not match source record")
            for field in ("kind", "status", "reason", "text_encoding"):
                if source[field] != record.get(field):
                    raise WorkflowSourceBindingConflict("Stored source summary does not match source record")
            if bool(source["ambiguous"]) != bool(record.get("ambiguous")):
                raise WorkflowSourceBindingConflict("Stored source ambiguity does not match source record")
            if source["source_text_bytes_sha256"] != record.get("text_bytes_sha256"):
                raise WorkflowSourceBindingConflict("Stored source byte hash does not match source record")
            decoded = source["decoded_text_utf8"]
            if source["status"] != "available" or not isinstance(record.get("text"), str) or decoded is None:
                raise WorkflowSourceBindingError("Only available sources with retained decoded text can be bound")
            decoded = bytes(decoded)
            decoded_hash = hashlib.sha256(decoded).hexdigest()
            if decoded_hash != source["decoded_text_utf8_sha256"]:
                raise WorkflowSourceBindingConflict("Stored decoded text hash mismatch")
            try:
                text = decoded.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WorkflowSourceBindingConflict("Stored decoded text is not UTF-8") from exc
            if record["text"] != text:
                raise WorkflowSourceBindingConflict("Decoded text does not match stored source record")
            capture = connection.execute(
                "SELECT c.id,c.uuid,c.library_id,c.workflow_id,c.version_id,c.raw_sha256,c.raw_payload,"
                "v.uuid version_uuid,f.uuid workflow_uuid FROM workflow_capture c "
                "JOIN workflow_version v ON v.id=c.version_id AND v.library_id=c.library_id AND v.workflow_id=c.workflow_id "
                "JOIN workflow_family f ON f.id=c.workflow_id AND f.library_id=c.library_id "
                "WHERE c.uuid=? AND c.library_id=?", (capture_uuid, library["id"])).fetchone()
            if capture is None:
                raise WorkflowSourceBindingError("Unknown workflow capture in selected library")
            raw = bytes(capture["raw_payload"])
            if hashlib.sha256(raw).hexdigest() != capture["raw_sha256"]:
                raise WorkflowSourceBindingConflict("Stored workflow capture hash mismatch")
            if raw != decoded:
                raise WorkflowSourceBindingError("Capture bytes do not exactly match the source decoded text")
            try:
                encoding_is_utf8 = codecs.lookup(source["text_encoding"] or "utf-8").name == "utf-8"
            except LookupError:
                encoding_is_utf8 = False
            conversion = (CONVERSION_PRESERVED
                          if encoding_is_utf8 and source["source_text_bytes_sha256"] == decoded_hash
                          and record.get("text_bytes_provenance") in ("collector_decoded_text_payload", "container_text_value")
                          else CONVERSION_TRANSCODED)
            existing = connection.execute(
                "SELECT b.*,c.uuid capture_uuid,v.uuid version_uuid,f.uuid workflow_uuid "
                "FROM workflow_source_binding b JOIN workflow_capture c ON c.id=b.capture_id "
                "JOIN workflow_version v ON v.id=b.version_id AND v.library_id=b.library_id "
                "JOIN workflow_family f ON f.id=b.workflow_id AND f.library_id=b.library_id "
                "WHERE b.source_id=? AND b.capture_id=?", (source["id"], capture["id"])).fetchone()
            values = (source["record_sha256"], decoded_hash, conversion, EVIDENCE_KIND,
                      library["id"], source["asset_id"], source["revision_id"], source["snapshot_id"],
                      capture["version_id"], capture["workflow_id"])
            if existing:
                actual = tuple(existing[key] for key in (
                    "source_record_sha256", "imported_utf8_sha256", "conversion", "evidence_kind",
                    "library_id", "asset_id", "revision_id", "snapshot_id", "version_id", "workflow_id"))
                if actual != values:
                    raise WorkflowSourceBindingConflict("Existing source-to-capture binding differs")
                return {**self._binding_record(existing), "source_uuid": source_uuid}
            binding_uuid = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO workflow_source_binding(uuid,source_id,capture_id,library_id,asset_id,revision_id,snapshot_id,"
                "version_id,workflow_id,source_record_sha256,imported_utf8_sha256,conversion,evidence_kind) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (binding_uuid, source["id"], capture["id"], library["id"], source["asset_id"], source["revision_id"],
                 source["snapshot_id"], capture["version_id"], capture["workflow_id"], source["record_sha256"],
                 decoded_hash, conversion, EVIDENCE_KIND))
            row = connection.execute(
                "SELECT b.*,c.uuid capture_uuid,v.uuid version_uuid,f.uuid workflow_uuid "
                "FROM workflow_source_binding b JOIN workflow_capture c ON c.id=b.capture_id "
                "JOIN workflow_version v ON v.id=b.version_id AND v.library_id=b.library_id "
                "JOIN workflow_family f ON f.id=b.workflow_id AND f.library_id=b.library_id WHERE b.uuid=?",
                (binding_uuid,)).fetchone()
            return {**self._binding_record(row), "source_uuid": source_uuid}

    @staticmethod
    def _binding_record(row: sqlite3.Row) -> dict[str, Any]:
        return {"uuid": row["uuid"], "capture_uuid": row["capture_uuid"], "version_uuid": row["version_uuid"],
                "workflow_uuid": row["workflow_uuid"], "source_record_sha256": row["source_record_sha256"],
                "imported_utf8_sha256": row["imported_utf8_sha256"], "conversion": row["conversion"],
                "evidence_kind": row["evidence_kind"]}

    def list_for_capture(self, library_uuid: str, capture_uuid: str, *, limit: int = 100,
                         after_id: int = 0) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= PAGE_SIZE_MAX or type(after_id) is not int or after_id < 0:
            raise ValueError("limit must be 1..100 and after_id a non-negative integer")
        with self.archive._transaction(write=False) as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            capture = connection.execute("SELECT id FROM workflow_capture WHERE uuid=? AND library_id=?",
                                         (capture_uuid, library["id"])).fetchone()
            if capture is None:
                raise WorkflowSourceBindingError("Unknown workflow capture in selected library")
            rows = connection.execute(
                "SELECT b.id,b.uuid,b.source_record_sha256,b.imported_utf8_sha256,b.conversion,b.evidence_kind,s.uuid source_uuid,"
                "s.kind source_kind,s.source_locator_json,s.locator_hash,s.record_sha256 current_source_record_sha256,"
                "a.path media_path,a.uuid media_uuid,r.uuid revision_uuid,m.uuid snapshot_uuid "
                "FROM workflow_source_binding b JOIN workflow_source_evidence s ON s.id=b.source_id AND s.library_id=b.library_id "
                "JOIN media_asset a ON a.id=s.asset_id AND a.library_id=s.library_id "
                "JOIN media_revision r ON r.id=s.revision_id AND r.asset_id=s.asset_id AND r.library_id=s.library_id "
                "JOIN metadata_snapshot m ON m.id=s.snapshot_id AND m.library_id=s.library_id "
                "WHERE b.library_id=? AND b.capture_id=? AND b.id>? ORDER BY b.id LIMIT ?",
                (library["id"], capture["id"], after_id, limit + 1)).fetchall()
            more = len(rows) > limit
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                if item.pop("current_source_record_sha256") != item["source_record_sha256"]:
                    raise WorkflowSourceBindingConflict("Binding source record hash differs")
                if hashlib.sha256(item["source_locator_json"].encode("utf-8")).hexdigest() != item.pop("locator_hash"):
                    raise WorkflowSourceBindingConflict("Binding source locator hash mismatch")
                item["source_locator"] = json.loads(item.pop("source_locator_json"))
            return {"items": items, "next_after_id": items[-1]["id"] if more and items else None,
                    "has_more": more, "text_included": False}
