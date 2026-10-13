"""Immutable, library-scoped evidence extracted from metadata snapshots.

EXTRACTOR_VERSION is part of source identity. Any change to extraction rules or
the interpretation of stored text must use a new version, never rewrite rows.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import sqlite3
import uuid
from typing import Any

from .archive import MetadataArchive, rehydrate_snapshot_result
from .workflow import WorkflowArchive
from .workflow_sources import extract_workflow_sources

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "workflow-sources/2"
PAGE_SIZE_MAX = 100


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _schema_sql() -> tuple[str, ...]:
    return (
        """CREATE TABLE workflow_source_schema_migrations(
            version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE workflow_source_evidence(
            id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
            library_id INTEGER NOT NULL, snapshot_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL, revision_id INTEGER NOT NULL,
            extractor_version TEXT NOT NULL, locator_hash TEXT NOT NULL,
            source_locator_json TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, ambiguous INTEGER NOT NULL CHECK(ambiguous IN (0,1)),
            reason TEXT, text_encoding TEXT, decoded_text_utf8 BLOB, decoded_text_utf8_sha256 TEXT,
            source_text_bytes_sha256 TEXT,
            text_bytes_provenance TEXT NOT NULL, record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(snapshot_id,extractor_version,locator_hash),
            FOREIGN KEY(library_id) REFERENCES library(id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,snapshot_id,asset_id,revision_id)
                REFERENCES metadata_snapshot(library_id,id,asset_id,revision_id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,asset_id,revision_id)
                REFERENCES media_revision(library_id,asset_id,id) ON DELETE RESTRICT)""",
        "CREATE UNIQUE INDEX workflow_source_snapshot_scope_idx ON metadata_snapshot(library_id,id,asset_id,revision_id)",
        "CREATE UNIQUE INDEX workflow_source_snapshot_library_idx ON metadata_snapshot(library_id,id)",
        "CREATE INDEX workflow_source_page_idx ON workflow_source_evidence(library_id,snapshot_id,id)",
        """CREATE TABLE workflow_source_summary(
            library_id INTEGER NOT NULL,snapshot_id INTEGER NOT NULL,extractor_version TEXT NOT NULL,
            summary_json TEXT NOT NULL,summary_sha256 TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,extractor_version),
            FOREIGN KEY(library_id,snapshot_id) REFERENCES metadata_snapshot(library_id,id) ON DELETE RESTRICT)""",
        """CREATE TRIGGER workflow_source_summary_no_update BEFORE UPDATE ON workflow_source_summary
            BEGIN SELECT RAISE(ABORT,'workflow source summaries are immutable'); END""",
        """CREATE TRIGGER workflow_source_summary_no_delete BEFORE DELETE ON workflow_source_summary
            BEGIN SELECT RAISE(ABORT,'workflow source summaries are immutable'); END""",
        """CREATE TRIGGER workflow_source_evidence_no_update BEFORE UPDATE ON workflow_source_evidence
            BEGIN SELECT RAISE(ABORT,'workflow source evidence is immutable'); END""",
        """CREATE TRIGGER workflow_source_evidence_no_delete BEFORE DELETE ON workflow_source_evidence
            BEGIN SELECT RAISE(ABORT,'workflow source evidence is immutable'); END""",
    )


def _checksum() -> str:
    return hashlib.sha256(f"{SCHEMA_VERSION}\n{';'.join(_schema_sql())}".encode("utf-8")).hexdigest()


class WorkflowSourceStoreError(RuntimeError):
    pass


class WorkflowSourceStoreSchemaError(WorkflowSourceStoreError):
    pass


class WorkflowSourceStoreConflict(WorkflowSourceStoreError):
    pass


class WorkflowSourceStore:
    """Store candidate evidence; this class never assigns workflow identity."""

    def __init__(self, database: str):
        self.archive = WorkflowArchive(database)
        self.metadata = MetadataArchive(database)

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_source_schema_migrations'").fetchone()
        if not exists:
            raise WorkflowSourceStoreSchemaError("Workflow source store is not initialized")
        rows = connection.execute("SELECT version,checksum FROM workflow_source_schema_migrations ORDER BY version").fetchall()
        if [(r["version"], r["checksum"]) for r in rows] != [(SCHEMA_VERSION, _checksum())]:
            raise WorkflowSourceStoreSchemaError("Workflow source migration version/checksum mismatch")
        for name in ("workflow_source_evidence", "workflow_source_summary"):
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
                raise WorkflowSourceStoreSchemaError(f"Workflow source table is missing: {name}")

    def initialize(self) -> int:
        with self.archive._transaction(write=True, require_schema=False) as connection:
            # Refuse to create sources until their immutable input archive exists.
            self.metadata._validate_archive_schema(connection)
            present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_source_schema_migrations'").fetchone()
            if present:
                self._validate(connection)
                return SCHEMA_VERSION
            partial = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('workflow_source_evidence','workflow_source_summary') LIMIT 1"
            ).fetchone()
            if partial:
                raise WorkflowSourceStoreSchemaError("Partial workflow source schema exists")
            for statement in _schema_sql():
                connection.execute(statement)
            connection.execute("INSERT INTO workflow_source_schema_migrations(version,checksum) VALUES (?,?)", (SCHEMA_VERSION, _checksum()))
            self._validate(connection)
        return SCHEMA_VERSION

    def publish_snapshot(self, library_uuid: str, snapshot_uuid: str, *,
                         _connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        tx = self.archive._transaction(write=True, require_schema=False) if _connection is None else nullcontext(_connection)
        with tx as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            snap = connection.execute(
                "SELECT s.id snapshot_id,s.library_id,s.asset_id,s.revision_id,s.uuid,s.source_format,"
                "s.collector_version,s.state,s.coverage,s.source_sha256,s.result_json,s.captured_at,"
                "t.payload,a.uuid asset_uuid,r.uuid revision_uuid FROM metadata_snapshot s "
                "JOIN media_asset a ON a.id=s.asset_id AND a.library_id=s.library_id "
                "JOIN media_revision r ON r.id=s.revision_id AND r.asset_id=s.asset_id AND r.library_id=s.library_id "
                "LEFT JOIN metadata_snapshot_text t ON t.snapshot_id=s.id "
                "WHERE s.uuid=? AND s.library_id=?", (snapshot_uuid, library["id"])).fetchone()
            if snap is None:
                raise WorkflowSourceStoreError("Unknown metadata snapshot in selected library")
            # v2 moves the bulky raw-byte fields into metadata_snapshot_text;
            # rehydrate so the extractor sees the original collector shape.
            snapshot = {"uuid": snap["uuid"], "source_format": snap["source_format"],
                        "collector_version": snap["collector_version"], "state": snap["state"],
                        "coverage": snap["coverage"], "source_sha256": snap["source_sha256"],
                        "result": rehydrate_snapshot_result(json.loads(snap["result_json"]),
                                                            snap["payload"]),
                        "captured_at": snap["captured_at"]}
            extracted = extract_workflow_sources(snapshot)
            summary = {key: value for key, value in extracted.items() if key != "sources"}
            summary_json = _json(summary)
            summary_hash = hashlib.sha256(summary_json.encode("utf-8")).hexdigest()
            old_summary = connection.execute(
                "SELECT summary_sha256 FROM workflow_source_summary WHERE snapshot_id=? AND extractor_version=?",
                (snap["snapshot_id"], EXTRACTOR_VERSION)).fetchone()
            if old_summary and old_summary["summary_sha256"] != summary_hash:
                raise WorkflowSourceStoreConflict("Snapshot summary changed under the same extractor version")
            if not old_summary:
                connection.execute("INSERT INTO workflow_source_summary VALUES (?,?,?,?,?)",
                                   (library["id"], snap["snapshot_id"], EXTRACTOR_VERSION, summary_json, summary_hash))
            inserted = 0
            for source in extracted["sources"]:
                locator_json = _json(source.get("source_locator"))
                locator_hash = hashlib.sha256(locator_json.encode("utf-8")).hexdigest()
                record = {"extractor_version": EXTRACTOR_VERSION, **source}
                record_json = _json(record)
                record_hash = hashlib.sha256(record_json.encode("utf-8")).hexdigest()
                text_value = source.get("text")
                decoded_utf8 = text_value.encode("utf-8") if isinstance(text_value, str) else None
                decoded_utf8_hash = hashlib.sha256(decoded_utf8).hexdigest() if decoded_utf8 is not None else None
                existing = connection.execute(
                    "SELECT record_sha256 FROM workflow_source_evidence WHERE snapshot_id=? AND extractor_version=? AND locator_hash=?",
                    (snap["snapshot_id"], EXTRACTOR_VERSION, locator_hash)).fetchone()
                if existing:
                    if existing["record_sha256"] != record_hash:
                        raise WorkflowSourceStoreConflict("Source locator already has different immutable evidence")
                    continue
                connection.execute(
                    "INSERT INTO workflow_source_evidence(uuid,library_id,snapshot_id,asset_id,revision_id,extractor_version,"
                    "locator_hash,source_locator_json,kind,status,ambiguous,reason,text_encoding,decoded_text_utf8,decoded_text_utf8_sha256,source_text_bytes_sha256,"
                    "text_bytes_provenance,record_json,record_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), library["id"], snap["snapshot_id"], snap["asset_id"], snap["revision_id"],
                     EXTRACTOR_VERSION, locator_hash, locator_json, source["kind"], source["status"],
                     int(bool(source["ambiguous"])), source.get("reason"), source.get("text_encoding"), decoded_utf8,
                     decoded_utf8_hash, source.get("text_bytes_sha256"),
                     (source.get("text_bytes_provenance") or "") + ("; decoded text stored as UTF-8; source hash remains the collector byte hash" if decoded_utf8 is not None else ""),
                     record_json, record_hash))
                inserted += 1
            count = connection.execute("SELECT count(*) FROM workflow_source_evidence WHERE snapshot_id=? AND extractor_version=?",
                                       (snap["snapshot_id"], EXTRACTOR_VERSION)).fetchone()[0]
            return {"snapshot_uuid": snapshot_uuid, "library_uuid": library_uuid,
                    "extractor_version": EXTRACTOR_VERSION, "candidate_count": extracted["candidate_count"],
                    "stored_count": count, "inserted_count": inserted, "coverage": extracted["coverage"],
                    "summary": summary}

    def list_sources(self, library_uuid: str, snapshot_uuid: str, *, limit: int = 100,
                     after_id: int = 0) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= PAGE_SIZE_MAX or type(after_id) is not int or after_id < 0:
            raise ValueError("limit must be 1..100 and after_id a non-negative integer")
        with self.archive._transaction(write=False, require_schema=False) as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            snapshot_row = connection.execute("SELECT id FROM metadata_snapshot WHERE uuid=? AND library_id=?",
                                              (snapshot_uuid, library["id"])).fetchone()
            if snapshot_row is None:
                raise WorkflowSourceStoreError("Unknown metadata snapshot in selected library")
            summary_row = connection.execute("SELECT summary_json,summary_sha256 FROM workflow_source_summary "
                                              "WHERE library_id=? AND snapshot_id=? AND extractor_version=?",
                                              (library["id"], snapshot_row["id"], EXTRACTOR_VERSION)).fetchone()
            summary = None
            if summary_row:
                if hashlib.sha256(summary_row["summary_json"].encode("utf-8")).hexdigest() != summary_row["summary_sha256"]:
                    raise WorkflowSourceStoreConflict("Stored extraction summary hash mismatch")
                summary = json.loads(summary_row["summary_json"])
            rows = connection.execute(
                "SELECT e.id,e.uuid,e.extractor_version,e.locator_hash,e.kind,e.status,e.ambiguous,e.reason,"
                "e.text_encoding,e.source_text_bytes_sha256,e.text_bytes_provenance,e.record_sha256 "
                "FROM workflow_source_evidence e JOIN metadata_snapshot s ON s.id=e.snapshot_id AND s.library_id=e.library_id "
                "WHERE e.library_id=? AND s.uuid=? AND e.extractor_version=? AND e.id>? ORDER BY e.id LIMIT ?",
                (library["id"], snapshot_uuid, EXTRACTOR_VERSION, after_id, limit + 1)).fetchall()
            more = len(rows) > limit
            items = [dict(row) for row in rows[:limit]]
            return {"items": items, "next_after_id": items[-1]["id"] if more and items else None,
                    "has_more": more, "text_included": False, "summary": summary}

    def read_source(self, library_uuid: str, source_uuid: str) -> dict[str, Any]:
        with self.archive._transaction(write=False, require_schema=False) as connection:
            self._validate(connection)
            library = self.archive._library(connection, library_uuid)
            row = connection.execute(
                "SELECT e.* ,s.uuid snapshot_uuid,a.uuid asset_uuid,a.path media_path,r.uuid revision_uuid "
                "FROM workflow_source_evidence e JOIN metadata_snapshot s ON s.id=e.snapshot_id AND s.library_id=e.library_id "
                "JOIN media_asset a ON a.id=e.asset_id AND a.library_id=e.library_id "
                "JOIN media_revision r ON r.id=e.revision_id AND r.asset_id=e.asset_id AND r.library_id=e.library_id "
                "WHERE e.library_id=? AND e.uuid=?", (library["id"], source_uuid)).fetchone()
            if row is None:
                raise WorkflowSourceStoreError("Workflow source not found in selected library")
            result = dict(row)
            record_json = result.pop("record_json")
            if hashlib.sha256(record_json.encode("utf-8")).hexdigest() != result["record_sha256"]:
                raise WorkflowSourceStoreConflict("Stored source record hash mismatch")
            record = json.loads(record_json)
            locator_json = result["source_locator_json"]
            if hashlib.sha256(locator_json.encode("utf-8")).hexdigest() != result["locator_hash"]:
                raise WorkflowSourceStoreConflict("Stored source locator hash mismatch")
            if json.loads(locator_json) != record.get("source_locator"):
                raise WorkflowSourceStoreConflict("Stored source locator does not match source record")
            for field in ("kind", "status", "reason", "text_encoding"):
                if result[field] != record.get(field):
                    raise WorkflowSourceStoreConflict("Stored source summary does not match source record")
            if bool(result["ambiguous"]) != bool(record.get("ambiguous")):
                raise WorkflowSourceStoreConflict("Stored source ambiguity does not match source record")
            if result["source_text_bytes_sha256"] != record.get("text_bytes_sha256"):
                raise WorkflowSourceStoreConflict("Stored source byte hash does not match source record")
            decoded = result["decoded_text_utf8"]
            if decoded is not None:
                decoded = bytes(decoded)
                if hashlib.sha256(decoded).hexdigest() != result["decoded_text_utf8_sha256"]:
                    raise WorkflowSourceStoreConflict("Stored decoded text hash mismatch")
                if record.get("text") != decoded.decode("utf-8", errors="strict"):
                    raise WorkflowSourceStoreConflict("Stored decoded text does not match source record")
            elif result["decoded_text_utf8_sha256"] is not None:
                raise WorkflowSourceStoreConflict("Decoded text hash exists without text bytes")
            result["decoded_text_utf8"] = decoded
            result["source_locator"] = json.loads(result.pop("source_locator_json"))
            result["record"] = record
            return result
