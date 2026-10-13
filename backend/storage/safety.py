"""Revision-bound safety labels and scores for an initialized catalog.

This module intentionally keeps legacy path-only decisions as unbound evidence.
It never guesses which revision an old path label referred to.
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

from .database import initialize_schema


SAFETY_SCHEMA_VERSION = 1
_SAFETY_SCHEMA_SQL = """
CREATE TABLE safety_manual (
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('nsfw','sfw')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (library_id, asset_id, revision_id),
    FOREIGN KEY (library_id, asset_id, revision_id)
        REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT
);
CREATE TABLE safety_model (
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    model_id TEXT NOT NULL CHECK (length(model_id) > 0),
    score REAL NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (library_id, asset_id, revision_id, model_id),
    FOREIGN KEY (library_id, asset_id, revision_id)
        REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT
);
CREATE TABLE safety_legacy_evidence (
    id INTEGER PRIMARY KEY,
    library_uuid TEXT NOT NULL,
    source_key TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'unbound'
        CHECK (disposition IN ('unbound','reviewed')),
    retained_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (library_uuid, source_key, raw_json)
);
"""


def _safety_checksum(version: int, sql: str = _SAFETY_SCHEMA_SQL) -> str:
    return hashlib.sha256(f"{version}\n{sql}".encode("utf-8")).hexdigest()


def _validate_safety_history(connection: sqlite3.Connection) -> int:
    if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name="
            "'safety_schema_migrations'").fetchone():
        raise SafetySchemaError("Safety schema migration history is missing")
    rows = connection.execute(
        "SELECT version,checksum FROM safety_schema_migrations ORDER BY version").fetchall()
    current = 0
    for row in rows:
        version = int(row["version"])
        if version > SAFETY_SCHEMA_VERSION:
            raise SafetySchemaError(f"Safety schema {version} is newer than supported")
        if version != current + 1:
            raise SafetySchemaError(f"Non-contiguous safety schema history at {version}")
        if row["checksum"] != _safety_checksum(version):
            raise SafetySchemaError(f"Safety schema checksum mismatch at version {version}")
        current = version
    if current != SAFETY_SCHEMA_VERSION:
        raise SafetySchemaError("Safety schema has no applied migration")
    return current


class SafetyError(RuntimeError):
    """Base error for safety-state operations."""


class SafetyUnavailableError(SafetyError):
    """The requested database is missing, a symlink, or inaccessible."""


class SafetySchemaError(SafetyError):
    """The existing database is not an initialized compatible catalog."""


class SafetyIdentityError(SafetyError):
    """An identity is invalid, not current, or outside its explicit library."""


def _uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SafetyIdentityError(f"Invalid {label} UUID") from exc


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SafetyRepository:
    """Transactional safety state bound to library, asset, and exact revision."""

    def __init__(self, database: str | Path):
        self.database = Path(database).absolute()

    @classmethod
    def initialize(cls, database: str | Path) -> "SafetyRepository":
        """Explicitly add the safety schema to an existing initialized catalog."""
        path = Path(database).absolute()
        if path.is_symlink():
            raise SafetyUnavailableError("Safety database path must not be a symlink")
        if not path.is_file():
            raise SafetyUnavailableError("An initialized catalog database must already exist")
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
                raise SafetySchemaError("Core catalog schema is not initialized")
            if not connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise SafetySchemaError("Core catalog schema has no applied migration")
            initialize_schema(connection)
            has_history = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='safety_schema_migrations'").fetchone()
            safety_tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('safety_manual','safety_model','safety_legacy_evidence')").fetchall()
            if not has_history and safety_tables:
                raise SafetySchemaError("Unversioned safety tables exist; refusing to adopt them")
            if not has_history:
                connection.execute(
                    "CREATE TABLE safety_schema_migrations (version INTEGER PRIMARY KEY, "
                    "checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT "
                    "(strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
                )
                connection.commit()
            rows = connection.execute(
                "SELECT version,checksum FROM safety_schema_migrations ORDER BY version").fetchall()
            current = 0
            for row in rows:
                version = int(row["version"])
                if version > SAFETY_SCHEMA_VERSION:
                    raise SafetySchemaError(f"Safety schema {version} is newer than supported")
                if version != current + 1:
                    raise SafetySchemaError(f"Non-contiguous safety schema history at {version}")
                if row["checksum"] != _safety_checksum(version):
                    raise SafetySchemaError(f"Safety schema checksum mismatch at version {version}")
                current = version
            if current == 0:
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n" + _SAFETY_SCHEMA_SQL +
                        "\nINSERT INTO safety_schema_migrations(version,checksum) VALUES "
                        f"({SAFETY_SCHEMA_VERSION},'{_safety_checksum(SAFETY_SCHEMA_VERSION)}');\nCOMMIT;"
                    )
                except Exception:
                    connection.rollback()
                    raise
            else:
                _validate_safety_history(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return cls(path)

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if self.database.is_symlink():
            raise SafetyUnavailableError("Safety database path must not be a symlink")
        try:
            real = self.database.resolve(strict=True)
            if not real.is_file():
                raise SafetyUnavailableError("Safety database does not exist")
            uri = real.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, SafetyError):
                raise
            raise SafetyUnavailableError(f"Cannot open existing safety database: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if foreign_key_violation(connection) is not None:
                raise SafetySchemaError("Catalog has foreign key violations")
            for table in ("schema_migrations", "safety_schema_migrations", "safety_manual", "safety_model",
                          "safety_legacy_evidence"):
                if not connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    raise SafetySchemaError(f"Safety schema is not initialized ({table} missing)")
            if not connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise SafetySchemaError("Catalog schema is not initialized")
            initialize_schema(connection)
            _validate_safety_history(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _current_revision(connection: sqlite3.Connection, library_uuid: str,
                          media_uuid: str, revision_uuid: str):
        library_uuid = _uuid(library_uuid, "library")
        media_uuid = _uuid(media_uuid, "media")
        revision_uuid = _uuid(revision_uuid, "revision")
        row = connection.execute(
            "SELECT l.id AS library_id,a.id AS asset_id,r.id AS revision_id,"
            "r.uuid AS revision_uuid,r.content_hash,r.verification_state,"
            "a.current_revision_id "
            "FROM library l JOIN media_asset a ON a.library_id=l.id "
            "JOIN media_revision r ON r.library_id=a.library_id AND r.asset_id=a.id "
            "WHERE l.uuid=? AND a.uuid=? AND r.uuid=?",
            (library_uuid, media_uuid, revision_uuid),
        ).fetchone()
        if row is None or row["current_revision_id"] != row["revision_id"]:
            raise SafetyIdentityError("Revision is not the current revision of this library asset")
        return row

    def set_manual(self, library_uuid: str, media_uuid: str, revision_uuid: str,
                   label: str, *, verified_revision: Mapping[str, Any]) -> dict[str, Any]:
        """Set a manual label only for a caller-provided verified current revision.

        ``verified_revision`` must be the strict verification record returned by the
        catalog layer; its IDs, verified status, and content hash are rechecked here.
        """
        if label not in ("nsfw", "sfw"):
            raise ValueError("label must be 'nsfw' or 'sfw'")
        if not isinstance(verified_revision, Mapping):
            raise SafetyIdentityError("A strict verification record is required")
        if (verified_revision.get("libraryId") != library_uuid or
                verified_revision.get("mediaId") != media_uuid or
                verified_revision.get("revisionId") != revision_uuid or
                verified_revision.get("verificationState") != "verified" or
                not verified_revision.get("contentHash")):
            raise SafetyIdentityError("Verification record does not match the requested revision")
        with self._transaction(write=True) as connection:
            row = self._current_revision(connection, library_uuid, media_uuid, revision_uuid)
            if row["verification_state"] != "verified" or \
                    row["content_hash"] != verified_revision.get("contentHash"):
                raise SafetyIdentityError("Database revision is not verified by the supplied record")
            connection.execute(
                "INSERT INTO safety_manual(library_id,asset_id,revision_id,label) VALUES (?,?,?,?) "
                "ON CONFLICT(library_id,asset_id,revision_id) DO UPDATE SET "
                "label=excluded.label,created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (row["library_id"], row["asset_id"], row["revision_id"], label),
            )
        return {"mediaId": media_uuid, "revisionId": revision_uuid, "label": label,
                "origin": "manual"}

    def clear_manual(self, library_uuid: str, media_uuid: str,
                     revision_uuid: str | None = None) -> int:
        library_uuid = _uuid(library_uuid, "library")
        media_uuid = _uuid(media_uuid, "media")
        with self._transaction(write=True) as connection:
            asset = connection.execute(
                "SELECT l.id AS library_id,a.id AS asset_id FROM library l "
                "JOIN media_asset a ON a.library_id=l.id WHERE l.uuid=? AND a.uuid=?",
                (library_uuid, media_uuid),
            ).fetchone()
            if asset is None:
                raise SafetyIdentityError("Unknown asset in selected library")
            if revision_uuid is None:
                cursor = connection.execute(
                    "DELETE FROM safety_manual WHERE library_id=? AND asset_id=?",
                    (asset["library_id"], asset["asset_id"]))
            else:
                revision_uuid = _uuid(revision_uuid, "revision")
                cursor = connection.execute(
                    "DELETE FROM safety_manual WHERE library_id=? AND asset_id=? AND revision_id=("
                    "SELECT id FROM media_revision WHERE uuid=? AND library_id=? AND asset_id=?)",
                    (asset["library_id"], asset["asset_id"], revision_uuid,
                     asset["library_id"], asset["asset_id"]))
        return cursor.rowcount

    def clear_all_manual(self, library_uuid: str) -> int:
        """Clear manual overrides for one library; model scores and evidence remain."""
        library_uuid = _uuid(library_uuid, "library")
        with self._transaction(write=True) as connection:
            library = connection.execute(
                "SELECT id FROM library WHERE uuid=?", (library_uuid,)
            ).fetchone()
            if library is None:
                raise SafetyIdentityError("Unknown library UUID")
            cursor = connection.execute(
                "DELETE FROM safety_manual WHERE library_id=?", (library["id"],)
            )
        return cursor.rowcount

    def set_model_score(self, library_uuid: str, media_uuid: str, revision_uuid: str,
                        score: float, model_id: str, *, metadata: Any = None) -> dict[str, Any]:
        score = float(score)
        model_id = str(model_id).strip()
        if not (0.0 <= score <= 1.0):
            raise ValueError("score must be between 0 and 1")
        if not model_id:
            raise ValueError("model_id must not be empty")
        with self._transaction(write=True) as connection:
            row = self._current_revision(connection, library_uuid, media_uuid, revision_uuid)
            connection.execute(
                "INSERT INTO safety_model(library_id,asset_id,revision_id,model_id,score,metadata_json) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(library_id,asset_id,revision_id,model_id) "
                "DO UPDATE SET score=excluded.score,metadata_json=excluded.metadata_json,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (row["library_id"], row["asset_id"], row["revision_id"], model_id,
                 score, _json_text(metadata or {})),
            )
        return {"mediaId": media_uuid, "revisionId": revision_uuid,
                "modelId": model_id, "score": score, "origin": "model"}

    def read_effective(self, library_uuid: str, media_uuid: str, revision_uuid: str,
                       *, model_id: str | None = None, threshold: float = 0.5) -> dict[str, Any]:
        threshold = float(threshold)
        if not (0.0 <= threshold <= 1.0):
            raise ValueError("threshold must be between 0 and 1")
        with self._transaction(write=False) as connection:
            row = self._current_revision(connection, library_uuid, media_uuid, revision_uuid)
            manual = connection.execute(
                "SELECT label FROM safety_manual WHERE library_id=? AND asset_id=? AND revision_id=?",
                (row["library_id"], row["asset_id"], row["revision_id"]),
            ).fetchone()
            machine = None
            if model_id is not None:
                machine = connection.execute(
                    "SELECT score,model_id,metadata_json FROM safety_model "
                    "WHERE library_id=? AND asset_id=? AND revision_id=? AND model_id=?",
                    (row["library_id"], row["asset_id"], row["revision_id"], str(model_id)),
                ).fetchone()
            if manual is not None:
                return {"label": manual["label"], "origin": "manual", "score": None,
                        "modelId": None, "revisionId": revision_uuid}
            if machine is not None:
                score = float(machine["score"])
                return {"label": "nsfw" if score >= threshold else "sfw",
                        "origin": "model", "score": score, "modelId": machine["model_id"],
                        "metadata": json.loads(machine["metadata_json"]),
                        "revisionId": revision_uuid}
            return {"label": None, "origin": None, "score": None, "modelId": None,
                    "revisionId": revision_uuid}

    def current_snapshot(self, library_uuid: str, *, model_id: str, threshold: float,
                         media_ids: list[str] | None = None) -> dict:
        """One query for current revisions; historical decisions stay archived."""
        library_uuid = _uuid(library_uuid, "library")
        threshold = float(threshold)
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        with self._transaction(write=False) as connection:
            if not connection.execute('SELECT 1 FROM library WHERE uuid=?', (library_uuid,)).fetchone():
                raise SafetyIdentityError('Unknown library UUID')
            selected = None
            if media_ids is not None:
                if not isinstance(media_ids, list) or len(media_ids) > 200:
                    raise ValueError('Safety page requires at most 200 media IDs')
                selected = sorted(set(_uuid(value, 'media') for value in media_ids))
                if not selected:
                    return {}
            selection = '' if selected is None else ' AND a.uuid IN (' + ','.join('?' for _ in selected) + ')'
            rows = connection.execute(
                'SELECT a.path,a.uuid AS media_uuid,r.uuid AS revision_uuid,r.size_bytes,r.mtime_ns,'
                'm.label,s.score FROM media_asset a JOIN library l ON l.id=a.library_id '
                'JOIN media_revision r ON r.id=a.current_revision_id '
                'LEFT JOIN safety_manual m ON m.library_id=a.library_id AND m.asset_id=a.id AND m.revision_id=r.id '
                'LEFT JOIN safety_model s ON s.library_id=a.library_id AND s.asset_id=a.id AND s.revision_id=r.id AND s.model_id=? '
                "WHERE l.uuid=? AND a.availability='present'" + selection,
                (model_id, library_uuid, *(selected or []))).fetchall()
            result = {}
            for row in rows:
                if row['path'] in result:
                    raise SafetyIdentityError('Current catalog paths are ambiguous')
                score = None if row['label'] is not None else row['score']
                label = row['label']
                if label is None and score is not None:
                    label = 'nsfw' if score >= threshold else 'sfw'
                result[row['path']] = {'mediaId': row['media_uuid'], 'revisionId': row['revision_uuid'],
                                      'label': label or 'unknown', 'score': score,
                                      'origin': 'manual' if row['label'] is not None else ('model' if score is not None else None),
                                      'size': row['size_bytes'], 'mtimeNs': row['mtime_ns']}
            return result

    def retain_legacy_evidence(self, library_uuid: str, source_key: str,
                               raw_json: str | bytes, report: Mapping[str, Any]) -> dict[str, Any]:
        """Retain source evidence and its migration report without binding it to an asset."""
        library_uuid = _uuid(library_uuid, "library")
        if not isinstance(source_key, str) or not source_key:
            raise ValueError("source_key must be non-empty text")
        if isinstance(raw_json, bytes):
            raw_text = raw_json.decode("utf-8", errors="strict")
        elif isinstance(raw_json, str):
            raw_text = raw_json
        else:
            raise TypeError("raw_json must be original JSON text or bytes")
        if not isinstance(report, Mapping):
            raise TypeError("report must be a mapping")
        with self._transaction(write=True) as connection:
            if not connection.execute("SELECT 1 FROM library WHERE uuid=?", (library_uuid,)).fetchone():
                raise SafetyIdentityError("Unknown library UUID")
            connection.execute(
                "INSERT OR IGNORE INTO safety_legacy_evidence"
                "(library_uuid,source_key,raw_json,report_json) VALUES (?,?,?,?)",
                (library_uuid, source_key, raw_text, _json_text(report)),
            )
            row = connection.execute(
                "SELECT id,disposition FROM safety_legacy_evidence "
                "WHERE library_uuid=? AND source_key=? AND raw_json=?",
                (library_uuid, source_key, raw_text),
            ).fetchone()
        return {"evidenceId": int(row["id"]), "disposition": row["disposition"],
                "bound": False}

    def list_legacy_evidence(self, library_uuid: str) -> list[dict[str, Any]]:
        library_uuid = _uuid(library_uuid, "library")
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT id,source_key,raw_json,report_json,disposition,retained_at "
                "FROM safety_legacy_evidence WHERE library_uuid=? ORDER BY id",
                (library_uuid,),
            ).fetchall()
        return [{"evidenceId": int(row["id"]), "sourceKey": row["source_key"],
                 "rawJson": row["raw_json"], "report": json.loads(row["report_json"]),
                 "disposition": row["disposition"], "retainedAt": row["retained_at"],
                 "bound": False} for row in rows]


__all__ = ["SafetyError", "SafetyUnavailableError", "SafetySchemaError",
           "SafetyIdentityError", "SafetyRepository"]
