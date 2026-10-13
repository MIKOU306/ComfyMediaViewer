"""Transactional persistence for parsed ComfyUI generation metadata.

The store extends an initialized catalog and initialized raw-metadata archive. It
does not edit either core schema, resolve model names to files, or promote legacy
metadata into manual confirmations.
"""

from __future__ import annotations

from backend.storage.integrity import foreign_key_violation
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterator, Mapping

from ..storage.database import initialize_schema
from .archive import MetadataArchive


GENERATION_SCHEMA_VERSION = 2
_GENERATION_SCHEMA_SQL = """
CREATE TABLE generation_parse_run (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    parser_version INTEGER NOT NULL CHECK (parser_version > 0),
    status TEXT NOT NULL CHECK (status IN ('successful','failed')),
    result_sha256 TEXT,
    result_json TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (library_id, asset_id, revision_id, id),
    UNIQUE (snapshot_id, parser_version, result_sha256),
    CHECK ((status='successful' AND result_sha256 IS NOT NULL AND result_json IS NOT NULL
            AND error_text IS NULL) OR
           (status='failed' AND result_sha256 IS NULL AND result_json IS NULL
            AND error_text IS NOT NULL)),
    FOREIGN KEY (library_id, asset_id, revision_id)
        REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (snapshot_id) REFERENCES metadata_snapshot(id) ON DELETE RESTRICT
);
CREATE TRIGGER generation_run_snapshot_scope_insert
BEFORE INSERT ON generation_parse_run
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM metadata_snapshot s
        WHERE s.id=NEW.snapshot_id AND s.library_id=NEW.library_id
          AND s.asset_id=NEW.asset_id AND s.revision_id=NEW.revision_id
    ) THEN RAISE(ABORT, 'generation run snapshot scope mismatch') END;
END;
CREATE TABLE generation_step (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL CHECK (step_index >= 0),
    node_id TEXT NOT NULL,
    class_type TEXT NOT NULL,
    seed_text TEXT,
    params_json TEXT NOT NULL,
    UNIQUE (run_id, id),
    UNIQUE (run_id, step_index),
    UNIQUE (run_id, node_id),
    FOREIGN KEY (library_id, asset_id, revision_id, run_id)
        REFERENCES generation_parse_run(library_id, asset_id, revision_id, id) ON DELETE CASCADE
);
CREATE TABLE generation_output (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL,
    output_index INTEGER NOT NULL CHECK (output_index >= 0),
    node_id TEXT NOT NULL,
    class_type TEXT NOT NULL,
    filename_prefix TEXT,
    UNIQUE (run_id, id),
    UNIQUE (run_id, output_index),
    UNIQUE (run_id, node_id),
    FOREIGN KEY (run_id) REFERENCES generation_parse_run(id) ON DELETE CASCADE
);
CREATE TABLE generation_output_step (
    run_id INTEGER NOT NULL,
    output_id INTEGER NOT NULL,
    step_id INTEGER NOT NULL,
    PRIMARY KEY (output_id, step_id),
    FOREIGN KEY (run_id, output_id) REFERENCES generation_output(run_id, id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, step_id) REFERENCES generation_step(run_id, id) ON DELETE CASCADE
);
CREATE TABLE generation_prompt_binding (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    step_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('positive','negative')),
    stage INTEGER NOT NULL CHECK (stage >= 0),
    text TEXT NOT NULL,
    source_nodes_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (step_id, side, stage),
    FOREIGN KEY (run_id, step_id) REFERENCES generation_step(run_id, id) ON DELETE CASCADE
);
-- Prompt search reaches bindings as "pb.run_id = g.run_id AND pb.id IN (candidates)".
-- Without this index SQLite rescans the whole binding table per generation run,
-- which measured 2.8s on a 775-asset root versus 68ms with it.
CREATE INDEX generation_prompt_binding_run_id ON generation_prompt_binding(run_id, id);
CREATE TABLE generation_model_reference (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    raw_name TEXT,
    resolution TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (resolution IN ('unresolved','resolved','confirmed')),
    content_hash TEXT,
    UNIQUE (run_id, id),
    UNIQUE (run_id, node_id),
    CHECK (content_hash IS NULL OR resolution='confirmed'),
    FOREIGN KEY (run_id) REFERENCES generation_parse_run(id) ON DELETE CASCADE
);
CREATE TABLE generation_model_usage (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    step_id INTEGER,
    output_id INTEGER,
    model_reference_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('model','clip','vae')),
    load_order INTEGER NOT NULL CHECK (load_order >= 0),
    weight REAL,
    source_node_id TEXT NOT NULL,
    CHECK ((step_id IS NOT NULL AND output_id IS NULL) OR
           (step_id IS NULL AND output_id IS NOT NULL)),
    FOREIGN KEY (run_id, step_id) REFERENCES generation_step(run_id, id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, output_id) REFERENCES generation_output(run_id, id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, model_reference_id)
        REFERENCES generation_model_reference(run_id, id) ON DELETE CASCADE
);
CREATE TABLE generation_revision_current (
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    PRIMARY KEY (library_id, asset_id, revision_id),
    FOREIGN KEY (library_id, asset_id, revision_id)
        REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (library_id, asset_id, revision_id, run_id)
        REFERENCES generation_parse_run(library_id, asset_id, revision_id, id) ON DELETE RESTRICT
);
CREATE TABLE generation_asset_current (
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    PRIMARY KEY (library_id, asset_id),
    FOREIGN KEY (library_id, asset_id, revision_id)
        REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (library_id, asset_id, revision_id, run_id)
        REFERENCES generation_parse_run(library_id, asset_id, revision_id, id) ON DELETE RESTRICT
);
CREATE TRIGGER generation_revision_current_success_insert
BEFORE INSERT ON generation_revision_current
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM generation_parse_run r WHERE r.id=NEW.run_id AND r.status='successful'
    ) THEN RAISE(ABORT, 'current generation run must be successful') END;
END;
CREATE TRIGGER generation_revision_current_success_update
BEFORE UPDATE ON generation_revision_current
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM generation_parse_run r WHERE r.id=NEW.run_id AND r.status='successful'
    ) THEN RAISE(ABORT, 'current generation run must be successful') END;
END;
CREATE TRIGGER generation_asset_current_success_insert
BEFORE INSERT ON generation_asset_current
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM generation_parse_run r JOIN media_asset a
          ON a.library_id=r.library_id AND a.id=r.asset_id
        WHERE r.id=NEW.run_id AND r.status='successful'
          AND a.current_revision_id=NEW.revision_id
    ) THEN RAISE(ABORT, 'asset generation projection must target current successful revision') END;
END;
CREATE TRIGGER generation_asset_current_success_update
BEFORE UPDATE ON generation_asset_current
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM generation_parse_run r JOIN media_asset a
          ON a.library_id=r.library_id AND a.id=r.asset_id
        WHERE r.id=NEW.run_id AND r.status='successful'
          AND a.current_revision_id=NEW.revision_id
    ) THEN RAISE(ABORT, 'asset generation projection must target current successful revision') END;
END;
"""


def _schema_checksum(version: int = GENERATION_SCHEMA_VERSION) -> str:
    return hashlib.sha256(f"{version}\n{_GENERATION_SCHEMA_SQL}".encode("utf-8")).hexdigest()


class GenerationStoreError(RuntimeError):
    """Base error for generation metadata persistence."""


class GenerationStoreUnavailableError(GenerationStoreError):
    """Database is absent, inaccessible, or a symlink."""


class GenerationStoreSchemaError(GenerationStoreError):
    """Required core, archive, or generation schema is invalid."""


class GenerationSnapshotError(GenerationStoreError):
    """Snapshot identity, ownership, or revision is invalid."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _validate_result(result: Mapping[str, Any]) -> tuple[int, str, str]:
    if not isinstance(result, Mapping):
        raise ValueError("parsed_result must be a mapping")
    version = result.get("parseVersion")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("parsed_result.parseVersion must be a positive integer")
    coverage = result.get("coverage")
    if coverage not in ("complete", "partial", "empty"):
        raise ValueError("parsed_result.coverage must be complete, partial, or empty")
    for key in ("steps", "prompts", "models", "outputs", "warnings"):
        if not isinstance(result.get(key), list):
            raise ValueError(f"parsed_result.{key} must be a list")
    text = _json(result)
    return version, text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parser_family(result: Mapping[str, Any]) -> str:
    """Identify the existing parser families without comparing their version spaces."""
    raw = result.get("raw")
    if not isinstance(raw, Mapping):
        return "unknown"
    if raw.get("format") == "a1111_parameters":
        return "a1111_parameters"
    if "format" in raw:
        return "unknown"
    return "comfy_graph" if all(isinstance(node, Mapping) and
                                isinstance(node.get("class_type"), str) and
                                isinstance(node.get("inputs"), Mapping)
                                for node in raw.values()) else "unknown"


class GenerationStore:
    """Store immutable parse runs and current projections for raw snapshots."""

    def __init__(self, database: str | Path):
        self.database = Path(database).absolute()

    @contextmanager
    def _transaction(self, *, write: bool, require_store: bool = True) -> Iterator[sqlite3.Connection]:
        if self.database.is_symlink():
            raise GenerationStoreUnavailableError("Catalog path must not be a symlink")
        try:
            real = self.database.resolve(strict=True)
            if not real.is_file():
                raise GenerationStoreUnavailableError("Catalog database does not exist")
            connection = sqlite3.connect(real.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                         uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, GenerationStoreError):
                raise
            raise GenerationStoreUnavailableError(f"Cannot open generation store: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if foreign_key_violation(connection) is not None:
                raise GenerationStoreSchemaError("Database has foreign key violations")
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() \
                    or not connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise GenerationStoreSchemaError("Core catalog is not initialized")
            initialize_schema(connection)
            MetadataArchive._validate_archive_schema(connection)
            if require_store:
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
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name="
                "'generation_schema_migrations'").fetchone():
            raise GenerationStoreSchemaError("Generation store is not initialized")
        rows = connection.execute(
            "SELECT version,checksum FROM generation_schema_migrations ORDER BY version").fetchall()
        if len(rows) != 1 or rows[0]["version"] != GENERATION_SCHEMA_VERSION \
                or rows[0]["checksum"] != _schema_checksum():
            raise GenerationStoreSchemaError("Generation schema migration checksum/version mismatch")

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        """Bring an older generation schema forward without touching user data.

        Migrations are additive and idempotent. They run under the caller's
        write transaction and writer guard, and each step verifies the object it
        created so a half-applied upgrade is reported instead of silently used.
        """
        rows = connection.execute(
            "SELECT version,checksum FROM generation_schema_migrations ORDER BY version").fetchall()
        if not rows:
            raise GenerationStoreSchemaError("Generation store is not initialized")
        current = int(rows[-1]["version"])
        if current > GENERATION_SCHEMA_VERSION:
            raise GenerationStoreSchemaError(
                "Generation schema is newer than this build supports")
        if current == GENERATION_SCHEMA_VERSION:
            return
        for version in range(current + 1, GENERATION_SCHEMA_VERSION + 1):
            if version == 2:
                # v2 adds only generation_prompt_binding_run_id. CREATE INDEX IF
                # NOT EXISTS keeps a partially upgraded database self-healing.
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS generation_prompt_binding_run_id "
                    "ON generation_prompt_binding(run_id, id)")
            else:
                raise GenerationStoreSchemaError(f"No migration for generation schema {version}")
        if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='generation_prompt_binding_run_id'").fetchone():
            raise GenerationStoreSchemaError("generation_prompt_binding_run_id was not created")
        connection.execute("DELETE FROM generation_schema_migrations")
        connection.execute(
            "INSERT INTO generation_schema_migrations(version,checksum) VALUES (?,?)",
            (GENERATION_SCHEMA_VERSION, _schema_checksum()))

    def initialize(self) -> int:
        """Explicitly create the independent generation schema on an archived catalog."""
        with self._transaction(write=True, require_store=False) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='generation_schema_migrations'").fetchone()
            if exists:
                self._migrate_schema(connection)
                self._validate_schema(connection)
                return GENERATION_SCHEMA_VERSION
            partial = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'generation_%'").fetchone()
            if partial:
                raise GenerationStoreSchemaError("Partial unversioned generation schema exists")
            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    "CREATE TABLE generation_schema_migrations (version INTEGER PRIMARY KEY, "
                    "checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT "
                    "(strftime('%Y-%m-%dT%H:%M:%fZ','now')));\n" +
                    _GENERATION_SCHEMA_SQL +
                    "\nINSERT INTO generation_schema_migrations(version,checksum) VALUES "
                    f"({GENERATION_SCHEMA_VERSION},'{_schema_checksum()}');\nCOMMIT;"
                )
            except Exception:
                connection.rollback()
                raise
        return GENERATION_SCHEMA_VERSION

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, snapshot_uuid: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT s.id AS snapshot_id,s.uuid AS snapshot_uuid,s.library_id,s.asset_id,s.revision_id,"
            "s.source_sha256,l.uuid AS library_uuid,a.uuid AS media_uuid,r.uuid AS revision_uuid,"
            "a.current_revision_id,r.id AS core_revision_id "
            "FROM metadata_snapshot s JOIN library l ON l.id=s.library_id "
            "JOIN media_asset a ON a.library_id=s.library_id AND a.id=s.asset_id "
            "JOIN media_revision r ON r.library_id=s.library_id AND r.asset_id=s.asset_id "
            "AND r.id=s.revision_id WHERE s.uuid=?", (snapshot_uuid,),
        ).fetchone()
        if row is None:
            raise GenerationSnapshotError("Unknown or invalid metadata snapshot UUID")
        return row

    @staticmethod
    def _prompt_source_nodes(parsed_result: Mapping[str, Any], step_node_id: str,
                             side: str, prompt_text: str) -> list[str]:
        """Resolve prompt source IDs only by traversing that sampler's exact graph links."""
        raw = parsed_result.get("raw")
        if not isinstance(raw, Mapping):
            return []
        graph = {str(key): value for key, value in raw.items()}
        sampler = graph.get(step_node_id)
        inputs = sampler.get("inputs", {}) if isinstance(sampler, Mapping) else {}
        value = inputs.get(side) if isinstance(inputs, Mapping) else None
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return []
        stack = [str(value[0])]
        visited: set[str] = set()
        found: list[str] = []
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            node = graph.get(node_id)
            if not isinstance(node, Mapping):
                continue
            node_inputs = node.get("inputs", {})
            if not isinstance(node_inputs, Mapping):
                continue
            if node.get("class_type") in ("CLIPTextEncode", "CLIPTextEncodeSDXL"):
                if node_inputs.get("text") == prompt_text:
                    found.append(node_id)
                continue
            for input_value in reversed(list(node_inputs.values())):
                if isinstance(input_value, (list, tuple)) and len(input_value) >= 2 \
                        and isinstance(input_value[0], (str, int)):
                    stack.append(str(input_value[0]))
        return found

    @staticmethod
    def _insert_model_refs(connection: sqlite3.Connection, run_id: int,
                           records: list[Mapping[str, Any]]) -> dict[str, int]:
        refs: dict[str, int] = {}
        for record in records:
            node_id = str(record.get("nodeId") or "")
            kind = str(record.get("kind") or "unknown")
            if not node_id:
                raise ValueError("model record requires nodeId")
            raw_name = record.get("name")
            if raw_name is not None and not isinstance(raw_name, str):
                raise ValueError("model raw name must be text or null")
            existing = refs.get(node_id)
            if existing is not None:
                current = connection.execute(
                    "SELECT kind,raw_name FROM generation_model_reference WHERE id=?", (existing,)
                ).fetchone()
                if current["kind"] != kind or current["raw_name"] != raw_name:
                    raise ValueError(f"conflicting model records for source node {node_id}")
                continue
            cursor = connection.execute(
                "INSERT INTO generation_model_reference(run_id,node_id,kind,raw_name,resolution) "
                "VALUES (?,?,?,?,'unresolved')", (run_id, node_id, kind, raw_name),
            )
            refs[node_id] = int(cursor.lastrowid)
        return refs

    @staticmethod
    def _add_usage(connection: sqlite3.Connection, *, run_id: int, step_id: int | None,
                   output_id: int | None, model_id: int, side: str, order: int,
                   weight: Any, node_id: str) -> None:
        if weight is not None:
            weight = float(weight)
        connection.execute(
            "INSERT INTO generation_model_usage(run_id,step_id,output_id,model_reference_id,"
            "side,load_order,weight,source_node_id) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, step_id, output_id, model_id, side, order, weight, node_id),
        )

    def publish(self, snapshot_uuid: str, parsed_result: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one successful parse without regressing its parser-family pointer."""
        parse_version, result_json, digest = _validate_result(parsed_result)
        parser_family = _parser_family(parsed_result)
        declared_snapshot = parsed_result.get("sourceSnapshotUUID", parsed_result.get("snapshotUUID"))
        if declared_snapshot is not None and declared_snapshot != snapshot_uuid:
            raise GenerationSnapshotError("Parsed result declares a different source snapshot")
        with self._transaction(write=True) as connection:
            snapshot = self._snapshot(connection, snapshot_uuid)
            declared_hash = parsed_result.get("sourceSHA256")
            if declared_hash is not None and declared_hash != snapshot["source_sha256"]:
                raise GenerationSnapshotError("Parsed result source hash does not match snapshot")
            existing = connection.execute(
                "SELECT id,uuid FROM generation_parse_run WHERE snapshot_id=? AND parser_version=? "
                "AND result_sha256=? AND status='successful'",
                (snapshot["snapshot_id"], parse_version, digest),
            ).fetchone()
            current = connection.execute(
                "SELECT r.uuid,r.parser_version,r.result_json FROM generation_revision_current c "
                "JOIN generation_parse_run r ON r.id=c.run_id WHERE c.library_id=? AND c.asset_id=? "
                "AND c.revision_id=?",
                (snapshot["library_id"], snapshot["asset_id"], snapshot["revision_id"]),
            ).fetchone()
            conflict = parser_family == "unknown"
            stale = False
            if current:
                current_family = _parser_family(json.loads(current["result_json"]))
                # Graph and A1111 parameter parser versions are independent sequences.
                conflict = current_family == "unknown" or parser_family == "unknown" \
                    or current_family != parser_family
                if not conflict:
                    same_version = connection.execute(
                        "SELECT 1 FROM generation_parse_run WHERE snapshot_id=? AND parser_version=? "
                        "AND status='successful' AND result_sha256<>? LIMIT 1",
                        (snapshot["snapshot_id"], parse_version, digest),
                    ).fetchone()
                    conflict = bool(same_version)
                    stale = parse_version < int(current["parser_version"])
            if existing:
                run_id, run_uuid = int(existing["id"]), existing["uuid"]
                reused = True
            else:
                cursor = connection.execute(
                    "INSERT INTO generation_parse_run(uuid,library_id,asset_id,revision_id,snapshot_id,"
                    "parser_version,status,result_sha256,result_json) VALUES (?,?,?,?,?,?,'successful',?,?)",
                    (str(uuid.uuid4()), snapshot["library_id"], snapshot["asset_id"],
                     snapshot["revision_id"], snapshot["snapshot_id"], parse_version, digest, result_json),
                )
                run_id = int(cursor.lastrowid)
                run_uuid = connection.execute(
                    "SELECT uuid FROM generation_parse_run WHERE id=?", (run_id,)
                ).fetchone()["uuid"]
                reused = False
                step_ids: dict[str, int] = {}
                step_models: dict[str, list[Mapping[str, Any]]] = {}
                for index, step in enumerate(parsed_result["steps"]):
                    if not isinstance(step, Mapping):
                        raise ValueError("steps entries must be objects")
                    node_id = str(step.get("nodeId") or "")
                    class_type = str(step.get("classType") or "")
                    if not node_id or not class_type:
                        raise ValueError("each step requires nodeId and classType")
                    params = {key: value for key, value in step.items()
                              if key not in {"nodeId", "classType", "seed", "models", "positive",
                                             "negative", "outputs", "unresolvedNodes"}}
                    cur = connection.execute(
                        "INSERT INTO generation_step(uuid,run_id,library_id,asset_id,revision_id,"
                        "step_index,node_id,class_type,seed_text,params_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), run_id, snapshot["library_id"], snapshot["asset_id"],
                         snapshot["revision_id"], index, node_id, class_type,
                         None if step.get("seed") is None else str(step.get("seed")), _json(params)),
                    )
                    step_ids[node_id] = int(cur.lastrowid)
                    step_models[node_id] = step.get("models", [])
                    for side in ("positive", "negative"):
                        prompts = step.get(side, [])
                        if not isinstance(prompts, list):
                            raise ValueError(f"step {side} prompts must be a list")
                        for stage, text in enumerate(prompts):
                            if not isinstance(text, str):
                                raise ValueError("prompt text must be text")
                            source_nodes = []
                            for prompt in parsed_result["prompts"]:
                                if isinstance(prompt, Mapping) and prompt.get("stepId") == node_id \
                                        and prompt.get("role") == side and prompt.get("text") == text:
                                    source_nodes = prompt.get("sourceNodes")
                                    break
                            if source_nodes is None or not source_nodes:
                                source_nodes = self._prompt_source_nodes(
                                    parsed_result, node_id, side, text)
                            if not isinstance(source_nodes, list) or any(
                                    not isinstance(item, (str, int)) for item in source_nodes):
                                raise ValueError("prompt sourceNodes must be a list of node IDs")
                            connection.execute(
                                "INSERT INTO generation_prompt_binding(run_id,step_id,side,stage,text,"
                                "source_nodes_json) VALUES (?,?,?,?,?,?)",
                                (run_id, step_ids[node_id], side, stage, text, _json(source_nodes)),
                            )

                output_ids: dict[str, int] = {}
                output_models: dict[str, list[Mapping[str, Any]]] = {}
                for index, output in enumerate(parsed_result["outputs"]):
                    if not isinstance(output, Mapping):
                        raise ValueError("outputs entries must be objects")
                    node_id = str(output.get("nodeId") or "")
                    class_type = str(output.get("classType") or "")
                    if not node_id or not class_type:
                        raise ValueError("each output requires nodeId and classType")
                    cur = connection.execute(
                        "INSERT INTO generation_output(uuid,run_id,output_index,node_id,class_type,"
                        "filename_prefix) VALUES (?,?,?,?,?,?)",
                        (str(uuid.uuid4()), run_id, index, node_id, class_type,
                         output.get("filenamePrefix")),
                    )
                    output_ids[node_id] = int(cur.lastrowid)
                    output_models[node_id] = output.get("models", [])
                    refs_for_output = output.get("steps", [])
                    if not isinstance(refs_for_output, list):
                        raise ValueError("output steps must be a list")
                    for step_node_id in refs_for_output:
                        if step_node_id not in step_ids:
                            raise ValueError(f"output references unknown step {step_node_id}")
                        connection.execute(
                            "INSERT INTO generation_output_step(run_id,output_id,step_id) VALUES (?,?,?)",
                            (run_id, output_ids[node_id], step_ids[step_node_id]),
                        )

                all_models: list[Mapping[str, Any]] = []
                for values in step_models.values():
                    if not isinstance(values, list):
                        raise ValueError("step models must be lists")
                    all_models.extend(values)
                for values in output_models.values():
                    if not isinstance(values, list):
                        raise ValueError("output models must be lists")
                    all_models.extend(values)
                refs = self._insert_model_refs(connection, run_id, all_models)

                for step_node, values in step_models.items():
                    for order, model in enumerate(values):
                        ref_id = refs[str(model["nodeId"])]
                        kind = str(model.get("kind") or "")
                        if kind == "lora":
                            if model.get("strengthModel") is not None:
                                self._add_usage(connection, run_id=run_id, step_id=step_ids[step_node],
                                                output_id=None, model_id=ref_id, side="model",
                                                order=order, weight=model["strengthModel"],
                                                node_id=str(model["nodeId"]))
                            if model.get("strengthClip") is not None:
                                self._add_usage(connection, run_id=run_id, step_id=step_ids[step_node],
                                                output_id=None, model_id=ref_id, side="clip",
                                                order=order, weight=model["strengthClip"],
                                                node_id=str(model["nodeId"]))
                        else:
                            side = "vae" if kind == "vae" else "model"
                            self._add_usage(connection, run_id=run_id, step_id=step_ids[step_node],
                                            output_id=None, model_id=ref_id, side=side,
                                            order=order, weight=None, node_id=str(model["nodeId"]))

                for output_node, values in output_models.items():
                    output_record = next(item for item in parsed_result["outputs"]
                                         if str(item.get("nodeId")) == output_node)
                    attached_steps = set(output_record.get("steps", []))
                    used_by_attached_steps = {
                        str(model.get("nodeId")) for step in attached_steps
                        for model in step_models.get(step, [])
                    }
                    for order, model in enumerate(values):
                        if str(model.get("nodeId")) in used_by_attached_steps:
                            continue
                        kind = str(model.get("kind") or "")
                        side = "vae" if kind == "vae" else "model"
                        self._add_usage(connection, run_id=run_id, step_id=None,
                                        output_id=output_ids[output_node],
                                        model_id=refs[str(model["nodeId"])], side=side,
                                        order=order, weight=None, node_id=str(model["nodeId"]))

                if not conflict and not stale:
                    connection.execute(
                        "INSERT INTO generation_revision_current(library_id,asset_id,revision_id,run_id) "
                        "VALUES (?,?,?,?) ON CONFLICT(library_id,asset_id,revision_id) "
                        "DO UPDATE SET run_id=excluded.run_id",
                        (snapshot["library_id"], snapshot["asset_id"], snapshot["revision_id"], run_id),
                    )
                    if snapshot["current_revision_id"] == snapshot["core_revision_id"]:
                        connection.execute(
                            "INSERT INTO generation_asset_current(library_id,asset_id,revision_id,run_id) "
                            "VALUES (?,?,?,?) ON CONFLICT(library_id,asset_id) DO UPDATE SET "
                            "revision_id=excluded.revision_id,run_id=excluded.run_id",
                            (snapshot["library_id"], snapshot["asset_id"], snapshot["revision_id"], run_id),
                        )
        status = ("conflict" if conflict else "idempotent" if reused else
                  "stale" if stale else "published")
        return {"status": status, "runUUID": run_uuid,
                "snapshotUUID": snapshot_uuid, "libraryUUID": snapshot["library_uuid"],
                "mediaUUID": snapshot["media_uuid"], "revisionUUID": snapshot["revision_uuid"],
                "isAssetCurrent": snapshot["current_revision_id"] == snapshot["core_revision_id"],
                "parseVersion": parse_version, "coverage": parsed_result["coverage"],
                "projectionUpdated": not (conflict or stale),
                **({"error": "parser family or same-version result conflicts with current history"}
                   if conflict else {})}

    def record_failure(self, snapshot_uuid: str, parser_version: int, error: str) -> dict[str, Any]:
        """Append a failed run without changing either successful projection."""
        if isinstance(parser_version, bool) or not isinstance(parser_version, int) or parser_version < 1:
            raise ValueError("parser_version must be a positive integer")
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be non-empty text")
        with self._transaction(write=True) as connection:
            snapshot = self._snapshot(connection, snapshot_uuid)
            run_uuid = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO generation_parse_run(uuid,library_id,asset_id,revision_id,snapshot_id,"
                "parser_version,status,error_text) VALUES (?,?,?,?,?,?,'failed',?)",
                (run_uuid, snapshot["library_id"], snapshot["asset_id"], snapshot["revision_id"],
                 snapshot["snapshot_id"], parser_version, error),
            )
        return {"status": "failed", "runUUID": run_uuid, "snapshotUUID": snapshot_uuid,
                "revisionUUID": snapshot["revision_uuid"], "error": error,
                "projectionUpdated": False}

    def read_current(self, snapshot_uuid: str) -> dict[str, Any] | None:
        """Return last successful parse for this exact snapshot revision, if any."""
        with self._transaction(write=False) as connection:
            snapshot = self._snapshot(connection, snapshot_uuid)
            row = connection.execute(
                "SELECT r.uuid,r.parser_version,r.status,r.result_json,r.result_sha256,r.created_at "
                "FROM generation_revision_current c JOIN generation_parse_run r ON r.id=c.run_id "
                "WHERE c.library_id=? AND c.asset_id=? AND c.revision_id=?",
                (snapshot["library_id"], snapshot["asset_id"], snapshot["revision_id"]),
            ).fetchone()
        if row is None:
            return None
        return {"runUUID": row["uuid"], "parserVersion": int(row["parser_version"]),
                "status": row["status"], "result": json.loads(row["result_json"]),
                "resultSHA256": row["result_sha256"], "createdAt": row["created_at"],
                "revisionUUID": snapshot["revision_uuid"]}

    def read_asset_current(self, library_uuid: str, media_uuid: str) -> dict[str, Any] | None:
        """Return current successful parse only when its revision is still asset-current."""
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT r.uuid AS run_uuid,r.parser_version,r.result_json,r.result_sha256,"
                "r.created_at,l.uuid AS library_uuid,a.uuid AS media_uuid,rev.uuid AS revision_uuid "
                "FROM generation_asset_current c JOIN generation_parse_run r ON r.id=c.run_id "
                "JOIN library l ON l.id=c.library_id JOIN media_asset a "
                "ON a.library_id=c.library_id AND a.id=c.asset_id "
                "JOIN media_revision rev ON rev.library_id=c.library_id AND rev.asset_id=c.asset_id "
                "AND rev.id=c.revision_id AND a.current_revision_id=rev.id "
                "WHERE l.uuid=? AND a.uuid=? AND r.status='successful'",
                (library_uuid, media_uuid),
            ).fetchone()
        if row is None:
            return None
        return {"runUUID": row["run_uuid"], "parserVersion": int(row["parser_version"]),
                "result": json.loads(row["result_json"]), "resultSHA256": row["result_sha256"],
                "createdAt": row["created_at"], "libraryUUID": row["library_uuid"],
                "mediaUUID": row["media_uuid"], "revisionUUID": row["revision_uuid"]}

    def read_run(self, run_uuid: str) -> dict[str, Any]:
        """Read a run with normalized step/output/prompt/model relations."""
        with self._transaction(write=False) as connection:
            run = connection.execute(
                "SELECT r.*,s.uuid AS snapshot_uuid,l.uuid AS library_uuid,a.uuid AS media_uuid,"
                "rev.uuid AS revision_uuid FROM generation_parse_run r "
                "JOIN metadata_snapshot s ON s.id=r.snapshot_id JOIN library l ON l.id=r.library_id "
                "JOIN media_asset a ON a.library_id=r.library_id AND a.id=r.asset_id "
                "JOIN media_revision rev ON rev.library_id=r.library_id AND rev.asset_id=r.asset_id "
                "AND rev.id=r.revision_id WHERE r.uuid=?", (run_uuid,),
            ).fetchone()
            if run is None:
                raise GenerationSnapshotError("Unknown parse run UUID")
            steps = connection.execute(
                "SELECT id,node_id,class_type,seed_text,params_json FROM generation_step "
                "WHERE run_id=? ORDER BY step_index", (run["id"],)
            ).fetchall()
            outputs = connection.execute(
                "SELECT id,node_id,class_type,filename_prefix FROM generation_output "
                "WHERE run_id=? ORDER BY output_index", (run["id"],)
            ).fetchall()
            prompts = connection.execute(
                "SELECT p.side,p.stage,p.text,p.source_nodes_json,s.node_id AS step_node "
                "FROM generation_prompt_binding p JOIN generation_step s ON s.id=p.step_id "
                "WHERE p.run_id=? ORDER BY s.step_index,p.side,p.stage", (run["id"],)
            ).fetchall()
            models = connection.execute(
                "SELECT id,node_id,kind,raw_name,resolution,content_hash "
                "FROM generation_model_reference WHERE run_id=? ORDER BY id", (run["id"],)
            ).fetchall()
            usage = connection.execute(
                "SELECT u.side,u.load_order,u.weight,u.source_node_id,s.node_id AS step_node,"
                "o.node_id AS output_node,m.node_id AS model_node,m.raw_name,m.kind "
                "FROM generation_model_usage u JOIN generation_model_reference m "
                "ON m.id=u.model_reference_id LEFT JOIN generation_step s ON s.id=u.step_id "
                "LEFT JOIN generation_output o ON o.id=u.output_id WHERE u.run_id=? "
                "ORDER BY u.load_order,u.id", (run["id"],)
            ).fetchall()
            output_steps = connection.execute(
                "SELECT o.node_id AS output_node,s.node_id AS step_node "
                "FROM generation_output_step x JOIN generation_output o ON o.id=x.output_id "
                "JOIN generation_step s ON s.id=x.step_id WHERE x.run_id=? ORDER BY o.output_index,s.step_index",
                (run["id"],)
            ).fetchall()
        return {"runUUID": run["uuid"], "snapshotUUID": run["snapshot_uuid"],
                "libraryUUID": run["library_uuid"], "mediaUUID": run["media_uuid"],
                "revisionUUID": run["revision_uuid"], "parserVersion": int(run["parser_version"]),
                "status": run["status"], "result": json.loads(run["result_json"])
                if run["result_json"] is not None else None, "error": run["error_text"],
                "steps": [{"nodeId": row["node_id"], "classType": row["class_type"],
                           "seed": row["seed_text"], "params": json.loads(row["params_json"])}
                          for row in steps],
                "outputs": [{"nodeId": row["node_id"], "classType": row["class_type"],
                             "filenamePrefix": row["filename_prefix"]} for row in outputs],
                "outputSteps": [dict(row) for row in output_steps],
                "prompts": [{"stepNodeId": row["step_node"], "side": row["side"],
                             "stage": int(row["stage"]), "text": row["text"],
                             "sourceNodes": json.loads(row["source_nodes_json"])} for row in prompts],
                "models": [{"nodeId": row["node_id"], "kind": row["kind"],
                            "rawName": row["raw_name"], "resolution": row["resolution"],
                            "contentHash": row["content_hash"]} for row in models],
                "modelUsage": [dict(row) for row in usage]}


__all__ = ["GenerationStore", "GenerationStoreError", "GenerationStoreUnavailableError",
           "GenerationStoreSchemaError", "GenerationSnapshotError"]
