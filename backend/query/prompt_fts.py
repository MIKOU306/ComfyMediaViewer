"""Versioned FTS5 trigram candidates for generation prompt bindings.

The generation binding table remains authoritative. New rows enter a pending
side table using pure-SQL triggers, so generation writers need no registered
Python SQL function. A guarded initialization folds and indexes pending rows
with the exact ``media_fold`` implementation used by QueryRepository.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from pathlib import Path


VERSION = 1
FOLD_ALGORITHM = "backend.query.repository._fold:NFC+Python-casefold;unidata=" + unicodedata.unidata_version
SCHEMA_TABLE_SQL = ("CREATE TABLE generation_prompt_fts_schema("
                    "version INTEGER PRIMARY KEY CHECK(version=1),checksum TEXT NOT NULL)")
PENDING_TABLE_SQL = ("CREATE TABLE generation_prompt_fts_pending("
                     "binding_id INTEGER PRIMARY KEY REFERENCES generation_prompt_binding(id) ON DELETE CASCADE)")
FTS_TABLE_SQL = ("CREATE VIRTUAL TABLE generation_prompt_binding_fts USING "
                 "fts5(text, tokenize='trigram case_sensitive 1')")
TRIGGER_SQL = {
    "generation_prompt_fts_after_insert": (
        "CREATE TRIGGER generation_prompt_fts_after_insert AFTER INSERT ON generation_prompt_binding "
        "BEGIN INSERT INTO generation_prompt_fts_pending(binding_id) VALUES(NEW.id); END"),
    "generation_prompt_fts_after_update": (
        "CREATE TRIGGER generation_prompt_fts_after_update AFTER UPDATE OF id,text ON generation_prompt_binding "
        "WHEN OLD.id IS NOT NEW.id OR OLD.text IS NOT NEW.text BEGIN "
        "DELETE FROM generation_prompt_binding_fts WHERE rowid=OLD.id; "
        "DELETE FROM generation_prompt_fts_pending WHERE binding_id=OLD.id; "
        "INSERT INTO generation_prompt_fts_pending(binding_id) VALUES(NEW.id); END"),
    "generation_prompt_fts_after_delete": (
        "CREATE TRIGGER generation_prompt_fts_after_delete AFTER DELETE ON generation_prompt_binding "
        "BEGIN DELETE FROM generation_prompt_binding_fts WHERE rowid=OLD.id; "
        "DELETE FROM generation_prompt_fts_pending WHERE binding_id=OLD.id; END"),
}
_SCHEMA_MATERIAL = "\n".join([str(VERSION), FOLD_ALGORITHM, SCHEMA_TABLE_SQL,
                              PENDING_TABLE_SQL, FTS_TABLE_SQL, *TRIGGER_SQL.values()])
CHECKSUM = hashlib.sha256(_SCHEMA_MATERIAL.encode("utf-8")).hexdigest()
_FTS_SHADOWS = {
    "generation_prompt_binding_fts_data", "generation_prompt_binding_fts_idx",
    "generation_prompt_binding_fts_content", "generation_prompt_binding_fts_docsize",
    "generation_prompt_binding_fts_config",
}
_EXTENSION_OBJECTS = {
    "generation_prompt_fts_schema", "generation_prompt_fts_pending",
    "generation_prompt_binding_fts", *TRIGGER_SQL, *_FTS_SHADOWS,
}
_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
_TRIGRAM_AVAILABLE: bool | None = None


class PromptFTSError(RuntimeError):
    """Base error for the optional prompt candidate index."""


class PromptFTSSchemaError(PromptFTSError):
    """The extension is incomplete, damaged, or has a different checksum."""


def _fold(text: str) -> str:
    # Keep this exactly aligned with backend.query.repository._fold.
    from .repository import _fold as repository_fold
    return repository_fold(text)


def _requires_fallback(text: str) -> bool:
    """Keep token forms that may be treated differently by FTS out of band.

    A prompt is routinely multi-line, so newlines and ordinary separators are
    **not** a reason to bypass FTS: sending them to the pending side used to
    exclude most real prompts (measured 926 of 2172 bindings on a real library),
    and the pending UNION is then re-scanned for every candidate asset row.

    Only forms whose tokenization is genuinely version-sensitive stay out of
    FTS: embedded NUL, and combining marks (the trigram tokenizer's behavior on
    them is not guaranteed across SQLite builds).
    """
    if "\x00" in text:
        return True
    return any(unicodedata.category(char) in ("Mn", "Mc", "Me") for char in text)


def _trigram_available() -> bool:
    global _TRIGRAM_AVAILABLE
    if _TRIGRAM_AVAILABLE is not None:
        return _TRIGRAM_AVAILABLE
    db = sqlite3.connect(":memory:")
    try:
        db.execute(FTS_TABLE_SQL)
        _TRIGRAM_AVAILABLE = True
    except sqlite3.Error:
        _TRIGRAM_AVAILABLE = False
    finally:
        db.close()
    return _TRIGRAM_AVAILABLE


def _connect(database: Path, *, write: bool):
    database = Path(database).expanduser().absolute()
    if database.is_symlink() or not database.is_file():
        raise PromptFTSError("Existing regular catalog database required")
    for parent in database.parents:
        if parent.is_symlink() or (getattr(parent.stat(), "st_file_attributes", 0) & 0x400):
            raise PromptFTSError("Catalog ancestors cannot be links or reparse points")
    real = database.resolve(strict=True)
    connection = sqlite3.connect(real.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if not write:
        connection.execute("PRAGMA query_only=ON")
    return connection


def _validate_generation_schema(connection):
    from backend.metadata.generation_store import GenerationStore, GenerationStoreSchemaError
    try:
        GenerationStore._validate_schema(connection)
    except GenerationStoreSchemaError as error:
        # A database still on an older generation schema is searchable: the prompt
        # index only needs the binding table and its rows. Refusing here would make
        # an unmigrated library unsearchable until the next write, so accept the
        # older version and let the store migrate it under the writer guard.
        row = connection.execute(
            "SELECT version FROM generation_schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None or int(row[0]) > GenerationStore.GENERATION_SCHEMA_VERSION:
            raise PromptFTSSchemaError("Generation store must be initialized and valid first") from error
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='generation_prompt_binding'").fetchone():
        raise PromptFTSSchemaError("Generation prompt binding table is missing")


def _extension_objects(connection):
    names = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'generation_prompt_fts_%' "
        "OR name LIKE 'generation_prompt_binding_fts%'")}
    return names


def _validate_extension(connection):
    objects = _extension_objects(connection)
    unknown = objects - _EXTENSION_OBJECTS
    if unknown:
        raise PromptFTSSchemaError("Unknown prompt FTS schema objects: " + ", ".join(sorted(unknown)))
    if objects != _EXTENSION_OBJECTS:
        missing = _EXTENSION_OBJECTS - objects
        raise PromptFTSSchemaError("Incomplete prompt FTS schema; missing: " + ", ".join(sorted(missing)))

    meta_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                                  "AND name='generation_prompt_fts_schema'").fetchone()[0]
    pending_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                                     "AND name='generation_prompt_fts_pending'").fetchone()[0]
    fts_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                                 "AND name='generation_prompt_binding_fts'").fetchone()[0]
    if (meta_sql != SCHEMA_TABLE_SQL or pending_sql != PENDING_TABLE_SQL or fts_sql != FTS_TABLE_SQL):
        raise PromptFTSSchemaError("Prompt FTS table definition differs from the supported schema")
    for name, statement in TRIGGER_SQL.items():
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or row[0] != statement:
            raise PromptFTSSchemaError("Prompt FTS trigger missing or changed: " + name)
    rows = connection.execute("SELECT version,checksum FROM generation_prompt_fts_schema").fetchall()
    if len(rows) != 1 or rows[0][0] != VERSION or rows[0][1] != CHECKSUM:
        raise PromptFTSSchemaError("Prompt FTS version/checksum mismatch")


def _index_binding(connection, binding_id: int, text: str) -> bool:
    folded = _fold(text)
    connection.execute("DELETE FROM generation_prompt_binding_fts WHERE rowid=?", (binding_id,))
    if _requires_fallback(folded):
        # Pending ids are unioned with FTS matches by the query helper.
        return False
    connection.execute("INSERT INTO generation_prompt_binding_fts(rowid,text) VALUES(?,?)",
                       (binding_id, folded))
    connection.execute("DELETE FROM generation_prompt_fts_pending WHERE binding_id=?", (binding_id,))
    return True


def _refresh_pending(connection, batch_size: int) -> tuple[int, int]:
    last = 0
    indexed = deferred = 0
    while True:
        rows = connection.execute(
            "SELECT p.binding_id,b.text FROM generation_prompt_fts_pending p "
            "JOIN generation_prompt_binding b ON b.id=p.binding_id "
            "WHERE p.binding_id>? ORDER BY p.binding_id LIMIT ?", (last, batch_size)).fetchall()
        if not rows:
            break
        for row in rows:
            if _index_binding(connection, int(row["binding_id"]), row["text"]):
                indexed += 1
            else:
                deferred += 1
            last = int(row["binding_id"])
    return indexed, deferred


def initialize(database, *, batch_size: int = 500) -> dict:
    """Create/backfill the optional extension. Caller must hold the data writer guard.

    Unsupported SQLite returns a clean fallback status without persistent writes.
    Existing partial or mismatched schemas raise ``PromptFTSSchemaError``.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be in 1..10000")
    connection = _connect(database, write=True)
    try:
        if _trigram_available():
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection.execute("BEGIN")
        _validate_generation_schema(connection)
        objects = _extension_objects(connection)
        if not _trigram_available():
            if objects:
                _validate_extension(connection)
            connection.rollback()
            return {"available": False, "fallback": "fts5_trigram_unavailable",
                    "indexed": 0, "pendingFallback": 0}
        if not objects:
            connection.execute(SCHEMA_TABLE_SQL)
            connection.execute(PENDING_TABLE_SQL)
            connection.execute(FTS_TABLE_SQL)
            for statement in TRIGGER_SQL.values():
                connection.execute(statement)
            connection.execute("INSERT INTO generation_prompt_fts_schema(version,checksum) VALUES (?,?)",
                               (VERSION, CHECKSUM))
            last = 0
            indexed = pending_fallback = 0
            while True:
                rows = connection.execute(
                    "SELECT id,text FROM generation_prompt_binding WHERE id>? ORDER BY id LIMIT ?",
                    (last, batch_size)).fetchall()
                if not rows:
                    break
                for row in rows:
                    if _requires_fallback(_fold(row["text"])):
                        connection.execute("INSERT INTO generation_prompt_fts_pending(binding_id) VALUES(?)",
                                           (row["id"],))
                        pending_fallback += 1
                    else:
                        connection.execute("INSERT INTO generation_prompt_binding_fts(rowid,text) VALUES(?,?)",
                                           (row["id"], _fold(row["text"])))
                        indexed += 1
                    last = int(row["id"])
        else:
            _validate_extension(connection)
            indexed, pending_fallback = _refresh_pending(connection, batch_size)
        _validate_extension(connection)
        connection.commit()
        pending = connection.execute("SELECT COUNT(*) FROM generation_prompt_fts_pending").fetchone()[0]
        return {"available": True, "version": VERSION, "checksum": CHECKSUM,
                "indexed": indexed, "pendingFallback": int(pending),
                "newlyDeferred": pending_fallback}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def refresh_pending(database, *, max_rows: int = 500, after_id: int = 0,
                    max_text_bytes: int = 4 * 1024 * 1024) -> dict:
    """Refresh one bounded pending-id page; caller must hold DataWriterGuard.

    This API never creates or repairs the extension schema. ``nextAfterId`` is
    the last pending binding examined, including rows that remain deferred, so
    a caller can advance past unsafe text instead of repeatedly scanning it.
    """
    if type(max_rows) is not int or not 1 <= max_rows <= 10000:
        raise ValueError("max_rows must be in 1..10000")
    if type(after_id) is not int or after_id < 0:
        raise ValueError("after_id must be a non-negative integer")
    if type(max_text_bytes) is not int or not 1 <= max_text_bytes <= 64 * 1024 * 1024:
        raise ValueError("max_text_bytes must be in 1..67108864")
    connection = _connect(database, write=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_generation_schema(connection)
        if not _extension_objects(connection):
            raise PromptFTSSchemaError("Prompt FTS extension must be initialized before refreshing")
        _validate_extension(connection)
        if not _trigram_available():
            raise PromptFTSSchemaError("FTS5 trigram is unavailable for the installed prompt index")
        rows = connection.execute(
            "SELECT p.binding_id,length(CAST(b.text AS BLOB)) AS text_bytes "
            "FROM generation_prompt_fts_pending p "
            "JOIN generation_prompt_binding b ON b.id=p.binding_id "
            "WHERE p.binding_id>? ORDER BY p.binding_id LIMIT ?", (after_id, max_rows)
        ).fetchall()
        indexed = deferred = processed = 0
        text_bytes = 0
        next_after_id = after_id
        for row in rows:
            binding_id = int(row["binding_id"])
            row_bytes = int(row["text_bytes"])
            # Always allow a first row to advance even when a caller configures
            # a budget below the source's single-record bound. Later rows stay
            # pending if adding their source bytes would exceed this batch.
            if processed and text_bytes + row_bytes > max_text_bytes:
                break
            prompt = connection.execute(
                "SELECT text FROM generation_prompt_binding WHERE id=?", (binding_id,)
            ).fetchone()
            if prompt is None:
                # FK cascades normally make this impossible, but keep a stale
                # pending key from wedging pagination if a damaged DB has one.
                connection.execute("DELETE FROM generation_prompt_fts_pending WHERE binding_id=?",
                                   (binding_id,))
                next_after_id = binding_id
                processed += 1
                continue
            if _index_binding(connection, binding_id, prompt["text"]):
                indexed += 1
            else:
                deferred += 1
            text_bytes += row_bytes
            next_after_id = binding_id
            processed += 1
        _validate_extension(connection)
        has_more = connection.execute(
            "SELECT 1 FROM generation_prompt_fts_pending WHERE binding_id>? LIMIT 1",
            (next_after_id,)
        ).fetchone() is not None
        connection.commit()
        return {"nextAfterId": next_after_id, "processed": processed,
                "indexed": indexed, "deferred": deferred, "textBytes": text_bytes,
                "hasMore": has_more}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def prompt_candidate_clause(connection, prompt_text: str, *, binding_alias: str = "pb") -> dict | None:
    """Return a candidate predicate plus the unchanged exact ``instr`` gate.

    ``None`` means an explicit safe fallback (old database, short term, trigram
    unavailable, or a text form whose tokenizer behavior is not guaranteed).
    A malformed present schema raises instead of silently enabling stale search.
    """
    if not isinstance(prompt_text, str):
        raise TypeError("prompt_text must be text")
    if not _ALIAS.fullmatch(binding_alias):
        raise ValueError("Invalid SQL binding alias")
    normalized = _fold(prompt_text)
    if len(normalized) < 3 or _requires_fallback(normalized):
        return None
    objects = _extension_objects(connection)
    if not objects:
        return None
    _validate_extension(connection)
    if not _trigram_available():
        return None
    phrase = '"' + normalized.replace('"', '""') + '"'
    # The candidate set is materialized once per query instead of being a
    # correlated subquery. Re-evaluating the FTS scan plus the pending-id UNION
    # for every candidate asset row is quadratic: measured on a 775-asset root
    # with 926 pending bindings, the correlated form spent 2.7s per statement
    # while the materialized form returned in single-digit milliseconds.
    #
    # The returned ``sql`` therefore carries a WITH clause that must be placed
    # ahead of the consuming statement, plus the binding order that clause needs.
    return {
        "sql": (f"{binding_alias}.id IN (SELECT id FROM prompt_candidate_bindings) "
                "AND instr(media_fold(" + binding_alias + ".text),?)>0"),
        "params": (normalized,),
        "withClause": ("WITH prompt_candidate_bindings(id) AS MATERIALIZED ("
                       "SELECT rowid FROM generation_prompt_binding_fts WHERE text MATCH ? "
                       "UNION SELECT binding_id FROM generation_prompt_fts_pending)"),
        "withParams": (phrase,),
        "normalized": normalized,
        "candidateSource": "generation_prompt_binding.id (FTS5 rowid UNION pending binding ids)",
        "exactPredicate": "instr(media_fold(binding.text), normalized_prompt)>0",
    }
