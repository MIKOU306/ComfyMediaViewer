"""Versioned raw and normalized ComfyUI workflow archives."""

from __future__ import annotations

from backend.storage.integrity import foreign_key_violation
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterator, Mapping

CANONICAL_VERSION = "cmv-workflow-c14n/1"
COLLECTOR_VERSION = "workflow-capture/1"
UI_LAYOUT_FIELDS = frozenset({"pos", "size", "color", "bgcolor", "selected", "dragging"})
SOURCE_FORMATS = frozenset({"comfyui-ui", "comfyui-api", "generic-json"})


class WorkflowArchiveError(RuntimeError):
    """Base error for workflow archive operations."""


class WorkflowArchiveUnavailableError(WorkflowArchiveError):
    """The existing catalog database cannot be opened."""


class WorkflowArchiveSchemaError(WorkflowArchiveError):
    """The independent workflow archive schema is missing or invalid."""


class WorkflowIdentityError(WorkflowArchiveError):
    """A workflow or revision identity is unknown, ambiguous, or out of scope."""


class WorkflowFormatError(WorkflowArchiveError):
    """A workflow payload is malformed or its declared format is inconsistent."""


def _schema_sql() -> tuple[str, ...]:
    return (
        """CREATE TABLE workflow_family (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            library_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (library_id,id),
            FOREIGN KEY (library_id) REFERENCES library(id) ON DELETE RESTRICT
        )""",
        """CREATE TABLE workflow_version (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            library_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL,
            version_no INTEGER NOT NULL,
            source_format TEXT NOT NULL CHECK (source_format IN ('comfyui-ui','comfyui-api','generic-json')),
            canonical_version TEXT NOT NULL,
            normalized_hash TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (workflow_id,normalized_hash),
            UNIQUE (workflow_id,version_no),
            UNIQUE (library_id,id,workflow_id),
            FOREIGN KEY (library_id) REFERENCES library(id) ON DELETE RESTRICT,
            FOREIGN KEY (library_id,workflow_id) REFERENCES workflow_family(library_id,id) ON DELETE RESTRICT
        )""",
        """CREATE TABLE workflow_node (
            id INTEGER PRIMARY KEY,
            library_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL,
            version_id INTEGER NOT NULL,
            node_key TEXT NOT NULL,
            node_json TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            UNIQUE (version_id,node_key),
            FOREIGN KEY (library_id,version_id,workflow_id)
                REFERENCES workflow_version(library_id,id,workflow_id) ON DELETE RESTRICT
        )""",
        """CREATE TABLE workflow_capture (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            library_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL,
            version_id INTEGER NOT NULL,
            source_format TEXT NOT NULL CHECK (source_format IN ('comfyui-ui','comfyui-api','generic-json')),
            collector_version TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            raw_payload BLOB NOT NULL,
            captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            FOREIGN KEY (library_id) REFERENCES library(id) ON DELETE RESTRICT,
            FOREIGN KEY (library_id,version_id,workflow_id)
                REFERENCES workflow_version(library_id,id,workflow_id) ON DELETE RESTRICT
        )""",
        """CREATE TABLE workflow_revision_usage (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            library_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            revision_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL,
            version_id INTEGER NOT NULL,
            usage_kind TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (revision_id,version_id,usage_kind),
            FOREIGN KEY (library_id,asset_id,revision_id)
                REFERENCES media_revision(library_id,asset_id,id) ON DELETE RESTRICT,
            FOREIGN KEY (library_id,version_id,workflow_id)
                REFERENCES workflow_version(library_id,id,workflow_id) ON DELETE RESTRICT
        )""",
        "CREATE INDEX workflow_version_hash_idx ON workflow_version(library_id,normalized_hash)",
        "CREATE INDEX workflow_capture_version_idx ON workflow_capture(version_id,captured_at)",
        "CREATE INDEX workflow_usage_revision_idx ON workflow_revision_usage(revision_id)",
    )


def _schema_checksum() -> str:
    source = f"1\n{';'.join(_schema_sql())}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


MAX_WORKFLOW_BYTES = 8 * 1024 * 1024
MAX_WORKFLOW_DEPTH = 64
MAX_WORKFLOW_VALUES = 200000


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowFormatError("Duplicate JSON keys make workflow identity ambiguous")
        result[key] = value
    return result


def _reject_constant(value):
    raise WorkflowFormatError("Non-finite JSON values are not workflow identity evidence")


def _check_graph_budget(value):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > MAX_WORKFLOW_DEPTH or count > MAX_WORKFLOW_VALUES:
            raise WorkflowFormatError("Workflow exceeds structure budget")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _raw_bytes(raw_workflow: Any) -> tuple[bytes, Any]:
    if isinstance(raw_workflow, (bytes, str)) and len(raw_workflow) > MAX_WORKFLOW_BYTES:
        raise WorkflowFormatError("Workflow exceeds byte budget")
    if isinstance(raw_workflow, bytes):
        raw = raw_workflow
        try:
            parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise WorkflowFormatError(f"Invalid UTF-8 JSON workflow: {exc}") from exc
    elif isinstance(raw_workflow, str):
        raw = raw_workflow.encode("utf-8")
        if len(raw) > MAX_WORKFLOW_BYTES:
            raise WorkflowFormatError("Workflow exceeds byte budget")
        try:
            parsed = json.loads(raw_workflow, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise WorkflowFormatError(f"Invalid JSON workflow: {exc}") from exc
    elif isinstance(raw_workflow, Mapping):
        parsed = dict(raw_workflow)
        _check_graph_budget(parsed)
        try:
            raw = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise WorkflowFormatError(f"Workflow object is not JSON serializable: {exc}") from exc
    else:
        raise WorkflowFormatError("Workflow input must be a mapping, JSON text, or UTF-8 JSON bytes")
    if not isinstance(parsed, dict):
        raise WorkflowFormatError("Workflow JSON root must be an object")
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise WorkflowFormatError("Workflow exceeds byte budget")
    _check_graph_budget(parsed)
    return raw, parsed


def _detect_format(graph: Mapping[str, Any]) -> str:
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        return "comfyui-ui"
    if isinstance(nodes, dict):
        return "comfyui-api"
    if graph and all(isinstance(node, dict) and "class_type" in node and "inputs" in node
                    for node in graph.values()):
        return "comfyui-api"
    return "generic-json"


def _api_nodes(graph: Mapping[str, Any]) -> Mapping[str, Any]:
    nodes = graph.get("nodes")
    if isinstance(nodes, dict):
        return nodes
    return graph


def normalize_workflow(raw_workflow: Any, *, source_format: str | None = None) -> dict[str, Any]:
    """Return a versioned canonical graph, preserving unknown and execution fields.

    UI graph node array order and IDs are retained. Only the explicit pure-layout
    node keys in ``UI_LAYOUT_FIELDS`` are removed. API prompt node objects are
    canonicalized as-is, so UI and API representations never share a hash.
    """
    _, graph = _raw_bytes(raw_workflow)
    detected = _detect_format(graph)
    actual_format = source_format or detected
    if actual_format not in SOURCE_FORMATS:
        raise WorkflowFormatError(f"Unsupported source format: {actual_format}")
    if source_format is not None and detected != "generic-json" and source_format != detected:
        raise WorkflowFormatError(
            f"Declared {source_format} does not match detected {detected} graph shape"
        )
    if actual_format == "comfyui-ui":
        nodes = graph.get("nodes")
        if not isinstance(nodes, list):
            raise WorkflowFormatError("ComfyUI UI workflow nodes must be an array")
        normalized_nodes = []
        for node in nodes:
            if not isinstance(node, dict):
                raise WorkflowFormatError("ComfyUI UI workflow nodes must be objects")
            normalized_nodes.append({key: value for key, value in node.items()
                                     if key not in UI_LAYOUT_FIELDS})
        normalized = dict(graph)
        normalized["nodes"] = normalized_nodes
    elif actual_format == "comfyui-api":
        nodes = _api_nodes(graph)
        if not isinstance(nodes, Mapping) or (nodes is graph and not graph):
            raise WorkflowFormatError("ComfyUI API workflow nodes must be an object")
        if any(not isinstance(node, dict) for node in nodes.values()):
            raise WorkflowFormatError("ComfyUI API node values must be objects")
        if any("class_type" not in node or "inputs" not in node for node in nodes.values()):
            raise WorkflowFormatError("ComfyUI API nodes must retain class_type and inputs")
        normalized = graph
    else:
        normalized = graph

    try:
        graph_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowFormatError(f"Workflow contains unsupported JSON values: {exc}") from exc
    identity_payload = json.dumps(
        {"canonical_version": CANONICAL_VERSION, "source_format": actual_format,
         "graph": json.loads(graph_json)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return {
        "source_format": actual_format,
        "canonical_version": CANONICAL_VERSION,
        "normalized_json": graph_json,
        "normalized_hash": hashlib.sha256(identity_payload.encode("utf-8")).hexdigest(),
        "graph": json.loads(graph_json),
    }


def _node_rows(source_format: str, graph: Mapping[str, Any]) -> list[tuple[str, str, int]]:
    rows = []
    if source_format == "comfyui-ui":
        for index, node in enumerate(graph["nodes"]):
            raw_id = node.get("id")
            key = str(raw_id) if raw_id is not None else f"index:{index}"
            rows.append((key, json.dumps(node, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")), index))
    elif source_format == "comfyui-api":
        for index, (node_id, node) in enumerate(sorted(_api_nodes(graph).items(), key=lambda pair: str(pair[0]))):
            rows.append((str(node_id), json.dumps(node, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":")), index))
    if len({row[0] for row in rows}) != len(rows):
        raise WorkflowFormatError("Workflow node identifiers must be unique")
    return rows


class WorkflowArchive:
    """Store workflow families, canonical versions, source captures, and explicit revision links."""

    def __init__(self, database: str | os.PathLike[str]):
        self.database = Path(database).absolute()

    @contextmanager
    def _transaction(self, *, write: bool, require_schema: bool = True) -> Iterator[sqlite3.Connection]:
        if self.database.is_symlink():
            raise WorkflowArchiveUnavailableError("Catalog path must not be a symlink")
        try:
            real = self.database.resolve(strict=True)
            if not real.is_file():
                raise WorkflowArchiveUnavailableError("Catalog database does not exist")
            uri = real.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, WorkflowArchiveError):
                raise
            raise WorkflowArchiveUnavailableError(f"Cannot open existing catalog: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if foreign_key_violation(connection) is not None:
                raise WorkflowArchiveSchemaError("Catalog has foreign key violations")
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
                raise WorkflowArchiveSchemaError("Catalog schema is not initialized")
            if not connection.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise WorkflowArchiveSchemaError("Catalog schema migration is missing")
            from ..storage.database import initialize_schema
            initialize_schema(connection)
            if require_schema:
                self._validate_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_schema_migrations'").fetchone():
            raise WorkflowArchiveSchemaError("Workflow archive is not initialized")
        rows = connection.execute(
            "SELECT version,checksum FROM workflow_schema_migrations ORDER BY version"
        ).fetchall()
        if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != _schema_checksum():
            raise WorkflowArchiveSchemaError("Workflow archive migration checksum/version mismatch")
        for table in ("workflow_family", "workflow_version", "workflow_node",
                      "workflow_capture", "workflow_revision_usage"):
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                raise WorkflowArchiveSchemaError(f"Workflow archive table is missing: {table}")

    def initialize(self) -> int:
        """Explicitly create the independent workflow schema in an existing catalog."""
        with self._transaction(write=True, require_schema=False) as connection:
            if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_schema_migrations'").fetchone():
                self._validate_schema(connection)
                return 1
            if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_family'").fetchone():
                raise WorkflowArchiveSchemaError("Partial workflow archive schema exists")
            connection.execute(
                "CREATE TABLE workflow_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
                "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
            )
            for statement in _schema_sql():
                connection.execute(statement)
            connection.execute(
                "INSERT INTO workflow_schema_migrations(version,checksum) VALUES (1,?)",
                (_schema_checksum(),),
            )
        return 1

    def create_workflow(self, library_uuid: str, name: str,
                        workflow_uuid: str | None = None) -> dict[str, str]:
        if not isinstance(name, str) or not name.strip():
            raise WorkflowIdentityError("Workflow family name must be non-empty")
        workflow_uuid = str(uuid.UUID(workflow_uuid)) if workflow_uuid else str(uuid.uuid4())
        with self._transaction(write=True) as connection:
            library = self._library(connection, library_uuid)
            existing = connection.execute(
                "SELECT id,uuid,name,library_id FROM workflow_family WHERE uuid=?", (workflow_uuid,)
            ).fetchone()
            if existing:
                if existing["library_id"] != library["id"]:
                    raise WorkflowIdentityError("Workflow UUID belongs to another library")
                return {"workflow_uuid": existing["uuid"], "name": existing["name"]}
            connection.execute(
                "INSERT INTO workflow_family(uuid,library_id,name) VALUES (?,?,?)",
                (workflow_uuid, library["id"], name.strip()),
            )
        return {"workflow_uuid": workflow_uuid, "name": name.strip()}

    def capture_workflow(self, library_uuid: str, raw_workflow: Any, *,
                         workflow_uuid: str | None = None, name: str | None = None,
                         source_format: str | None = None,
                         collector_version: str = COLLECTOR_VERSION,
                         reuse_exact_capture: bool = False,
                         _connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        raw, parsed = _raw_bytes(raw_workflow)
        normalized = normalize_workflow(parsed, source_format=source_format)
        node_rows = _node_rows(normalized["source_format"], normalized["graph"])
        if not isinstance(collector_version, str) or not collector_version:
            raise ValueError("collector_version must be non-empty")
        requested_uuid = str(uuid.UUID(workflow_uuid)) if workflow_uuid else None
        default_name = name.strip() if isinstance(name, str) and name.strip() else "Workflow"
        raw_hash = hashlib.sha256(raw).hexdigest()

        transaction = self._transaction(write=True) if _connection is None else nullcontext(_connection)
        with transaction as connection:
            library = self._library(connection, library_uuid)
            family = None
            if requested_uuid:
                family = connection.execute(
                    "SELECT id,uuid,name,library_id FROM workflow_family WHERE uuid=?", (requested_uuid,)
                ).fetchone()
                if family and family["library_id"] != library["id"]:
                    raise WorkflowIdentityError("Workflow UUID belongs to another library")
                if family is None:
                    connection.execute(
                        "INSERT INTO workflow_family(uuid,library_id,name) VALUES (?,?,?)",
                        (requested_uuid, library["id"], default_name),
                    )
                    family = connection.execute(
                        "SELECT id,uuid,name,library_id FROM workflow_family WHERE uuid=?", (requested_uuid,)
                    ).fetchone()
            else:
                match = connection.execute(
                    "SELECT f.id,f.uuid,f.name,f.library_id FROM workflow_version v "
                    "JOIN workflow_family f ON f.id=v.workflow_id AND f.library_id=v.library_id "
                    "WHERE v.library_id=? AND v.normalized_hash=? ORDER BY v.id LIMIT 1",
                    (library["id"], normalized["normalized_hash"]),
                ).fetchone()
                if match:
                    family = match
                else:
                    family_uuid = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO workflow_family(uuid,library_id,name) VALUES (?,?,?)",
                        (family_uuid, library["id"], default_name),
                    )
                    family = connection.execute(
                        "SELECT id,uuid,name,library_id FROM workflow_family WHERE uuid=?", (family_uuid,)
                    ).fetchone()

            version = connection.execute(
                "SELECT id,uuid,version_no,source_format,canonical_version,normalized_hash "
                "FROM workflow_version WHERE workflow_id=? AND normalized_hash=?",
                (family["id"], normalized["normalized_hash"]),
            ).fetchone()
            created_version = version is None
            if version is None:
                version_no = connection.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 FROM workflow_version WHERE workflow_id=?",
                    (family["id"],),
                ).fetchone()[0]
                version_uuid = str(uuid.uuid4())
                version_id = connection.execute(
                    "INSERT INTO workflow_version(uuid,library_id,workflow_id,version_no,source_format,"
                    "canonical_version,normalized_hash,normalized_json) VALUES (?,?,?,?,?,?,?,?)",
                    (version_uuid, library["id"], family["id"], version_no,
                     normalized["source_format"], normalized["canonical_version"],
                     normalized["normalized_hash"], normalized["normalized_json"]),
                ).lastrowid
                for node_key, node_json, ordinal in node_rows:
                    connection.execute(
                        "INSERT INTO workflow_node(library_id,workflow_id,version_id,node_key,node_json,ordinal) "
                        "VALUES (?,?,?,?,?,?)",
                        (library["id"], family["id"], version_id, node_key, node_json, ordinal),
                    )
                version = {"id": version_id, "uuid": version_uuid, "version_no": version_no,
                           "source_format": normalized["source_format"],
                           "canonical_version": normalized["canonical_version"],
                           "normalized_hash": normalized["normalized_hash"]}
            existing_capture = None
            if reuse_exact_capture:
                existing_capture = connection.execute(
                    "SELECT uuid,raw_payload FROM workflow_capture WHERE library_id=? AND workflow_id=? "
                    "AND version_id=? AND collector_version=? AND raw_sha256=? ORDER BY id LIMIT 1",
                    (library["id"], family["id"], version["id"], collector_version, raw_hash)).fetchone()
                if existing_capture and bytes(existing_capture["raw_payload"]) != raw:
                    raise WorkflowArchiveError("Stored matching workflow capture is inconsistent")
            capture_uuid = existing_capture["uuid"] if existing_capture else str(uuid.uuid4())
            if existing_capture is None:
                connection.execute(
                    "INSERT INTO workflow_capture(uuid,library_id,workflow_id,version_id,source_format,"
                    "collector_version,raw_sha256,raw_payload) VALUES (?,?,?,?,?,?,?,?)",
                    (capture_uuid, library["id"], family["id"], version["id"],
                     normalized["source_format"], collector_version, raw_hash, raw),
                )
            return {
                "workflow_uuid": family["uuid"],
                "workflow_name": family["name"],
                "version_uuid": version["uuid"],
                "version_no": version["version_no"],
                "source_format": normalized["source_format"],
                "canonical_version": normalized["canonical_version"],
                "normalized_hash": normalized["normalized_hash"],
                "created_version": created_version,
                "capture_uuid": capture_uuid,
                "raw_sha256": raw_hash,
                "reused_capture": existing_capture is not None,
            }

    def attach_revision_usage(self, library_uuid: str, revision_uuid: str,
                              version_uuid: str, usage_kind: str) -> dict[str, Any]:
        """Attach only an explicitly supplied revision/version relation; infer no generation roles."""
        if not isinstance(usage_kind, str) or not usage_kind.strip():
            raise WorkflowIdentityError("usage_kind must be a non-empty explicit label")
        with self._transaction(write=True) as connection:
            library = self._library(connection, library_uuid)
            revision = connection.execute(
                "SELECT r.id,r.asset_id FROM media_revision r WHERE r.uuid=? AND r.library_id=?",
                (revision_uuid, library["id"]),
            ).fetchone()
            if revision is None:
                raise WorkflowIdentityError("Unknown revision in selected library")
            version = connection.execute(
                "SELECT id,workflow_id FROM workflow_version WHERE uuid=? AND library_id=?",
                (version_uuid, library["id"]),
            ).fetchone()
            if version is None:
                raise WorkflowIdentityError("Unknown workflow version in selected library")
            connection.execute(
                "INSERT INTO workflow_revision_usage(uuid,library_id,asset_id,revision_id,workflow_id,"
                "version_id,usage_kind) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(revision_id,version_id,usage_kind) DO NOTHING",
                (str(uuid.uuid4()), library["id"], revision["asset_id"], revision["id"],
                 version["workflow_id"], version["id"], usage_kind.strip()),
            )
            row = connection.execute(
                "SELECT uuid,usage_kind FROM workflow_revision_usage "
                "WHERE revision_id=? AND version_id=? AND usage_kind=?",
                (revision["id"], version["id"], usage_kind.strip()),
            ).fetchone()
            return {"usage_uuid": row["uuid"], "usage_kind": row["usage_kind"]}

    def read_version(self, version_uuid: str, *, library_uuid: str | None = None,
                     include_captures: bool = True) -> dict[str, Any]:
        """Read canonical graph and the separately retained raw captures."""
        with self._transaction(write=False) as connection:
            if library_uuid is not None:
                library = self._library(connection, library_uuid)
                if connection.execute("SELECT 1 FROM workflow_version WHERE uuid=? AND library_id=?",
                                      (version_uuid, library["id"])).fetchone() is None:
                    raise WorkflowIdentityError("Unknown workflow version in selected library")
            version = connection.execute(
                "SELECT v.uuid,v.version_no,v.source_format,v.canonical_version,v.normalized_hash,"
                "v.normalized_json,f.uuid AS workflow_uuid,f.name "
                "FROM workflow_version v JOIN workflow_family f ON f.id=v.workflow_id "
                "AND f.library_id=v.library_id WHERE v.uuid=?", (version_uuid,),
            ).fetchone()
            if version is None:
                raise WorkflowIdentityError("Unknown workflow version UUID")
            captures = connection.execute(
                "SELECT uuid,collector_version,raw_sha256,raw_payload,captured_at "
                "FROM workflow_capture WHERE version_id=(SELECT id FROM workflow_version WHERE uuid=?) "
                "ORDER BY id", (version_uuid,),
            ).fetchall() if include_captures else []
            nodes = connection.execute(
                "SELECT node_key,node_json,ordinal FROM workflow_node "
                "WHERE version_id=(SELECT id FROM workflow_version WHERE uuid=?) ORDER BY ordinal",
                (version_uuid,),
            ).fetchall()
            return {
                "workflow_uuid": version["workflow_uuid"],
                "workflow_name": version["name"],
                "version_uuid": version["uuid"],
                "version_no": version["version_no"],
                "source_format": version["source_format"],
                "canonical_version": version["canonical_version"],
                "normalized_hash": version["normalized_hash"],
                "graph": json.loads(version["normalized_json"]),
                "nodes": [{"node_key": row["node_key"], "node": json.loads(row["node_json"]),
                           "ordinal": row["ordinal"]} for row in nodes],
                "captures": [{"capture_uuid": row["uuid"],
                              "collector_version": row["collector_version"],
                              "raw_sha256": row["raw_sha256"],
                              "raw_payload": bytes(row["raw_payload"]),
                              "captured_at": row["captured_at"]} for row in captures],
            }

    @staticmethod
    def _library(connection: sqlite3.Connection, library_uuid: str):
        row = connection.execute("SELECT id,uuid FROM library WHERE uuid=?", (library_uuid,)).fetchone()
        if row is None:
            raise WorkflowIdentityError("Unknown library UUID")
        return row

