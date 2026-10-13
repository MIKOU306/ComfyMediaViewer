"""Crash-recoverable journal metadata for trash, restore, and move operations.

This module records intent and completion signals only. It never reads, moves,
renames, or deletes the media files themselves.
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
import time
import uuid
from typing import Any, Iterator


JOURNAL_VERSION = 1
STATES = {"planned", "disk_done", "metadata_done", "complete", "conflict", "error"}
ACTIONS = {"trash", "restore", "move"}
_NORMAL_ORDER = ("planned", "disk_done", "metadata_done", "complete")
_DB_SCHEMA = """
CREATE TABLE journal_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO journal_meta(key,value) VALUES ('schema_version','1');
CREATE TABLE operations (
    operation_uuid TEXT PRIMARY KEY,
    library_uuid TEXT NOT NULL,
    media_uuid TEXT NOT NULL,
    revision_uuid TEXT,
    action TEXT NOT NULL CHECK (action IN ('trash','restore','move')),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('planned','disk_done','metadata_done','complete','conflict','error')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    disk_done_at TEXT,
    metadata_done_at TEXT,
    completed_at TEXT,
    conflict_at TEXT,
    conflict_text TEXT,
    error_at TEXT,
    error_text TEXT
);
CREATE INDEX operations_recovery_idx ON operations(state, created_at);
CREATE INDEX operations_library_idx ON operations(library_uuid, created_at);
CREATE TRIGGER operations_immutable_identity
BEFORE UPDATE OF operation_uuid,library_uuid,media_uuid,revision_uuid,action,source_path,destination_path,created_at
ON operations
WHEN NEW.operation_uuid IS NOT OLD.operation_uuid
  OR NEW.library_uuid IS NOT OLD.library_uuid
  OR NEW.media_uuid IS NOT OLD.media_uuid
  OR NEW.revision_uuid IS NOT OLD.revision_uuid
  OR NEW.action IS NOT OLD.action
  OR NEW.source_path IS NOT OLD.source_path
  OR NEW.destination_path IS NOT OLD.destination_path
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT,'operation identity and paths are immutable');
END;
CREATE TRIGGER operations_state_transition_guard
BEFORE UPDATE OF state ON operations
WHEN NOT (
    (OLD.state='planned' AND NEW.state IN ('disk_done','conflict','error')) OR
    (OLD.state='disk_done' AND NEW.state IN ('metadata_done','conflict','error')) OR
    (OLD.state='metadata_done' AND NEW.state IN ('complete','conflict','error'))
)
BEGIN
    SELECT RAISE(ABORT,'illegal operation journal state transition');
END;
CREATE TRIGGER operations_no_delete
BEFORE DELETE ON operations
BEGIN
    SELECT RAISE(ABORT,'operation journal rows are append-only');
END;
"""


class JournalError(RuntimeError):
    """Base journal error."""


class JournalUnavailableError(JournalError):
    """The explicitly initialized journal cannot be opened."""


class JournalSchemaError(JournalError):
    """The file is not a supported operation journal."""


class OperationNotFoundError(JournalError):
    """No journal operation has the requested UUID."""


class InvalidTransitionError(JournalError):
    """A compare-and-set transition does not follow the operation state machine."""


class StateConflictError(JournalError):
    """The requested state transition lost its compare-and-set race."""


class JournalBusyError(JournalError):
    """SQLite could not acquire the journal write lock before the busy timeout."""


def _now() -> str:
    return str(int(time.time()))


def _uuid(value: str | None, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise JournalError(f"{label} must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise JournalError(f"{label} must be a UUID string") from exc


def _open_existing(database: Path, *, read_only: bool, timeout: float) -> sqlite3.Connection:
    try:
        resolved = database.resolve(strict=True)
        if not resolved.is_file():
            raise JournalUnavailableError("Operation journal does not exist")
        uri = resolved.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    except JournalError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise JournalUnavailableError(f"Cannot open operation journal: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
    return connection


def initialize_journal(database: str | Path, *, timeout: float = 5.0) -> None:
    """Explicitly create or validate an independent operation-journal database."""
    path = Path(database).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        connection = _open_existing(path, read_only=True, timeout=timeout)
        try:
            row = connection.execute(
                "SELECT value FROM journal_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or row["value"] != str(JOURNAL_VERSION):
                raise JournalSchemaError("Unsupported operation journal schema")
        except sqlite3.Error as exc:
            raise JournalSchemaError("File is not a valid operation journal") from exc
        finally:
            connection.close()
        return

    connection = sqlite3.connect(str(path), timeout=timeout)
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + _DB_SCHEMA + "\nCOMMIT;")
    except Exception:
        connection.rollback()
        connection.close()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    connection.close()


class OperationJournal:
    """Append-only operation identity plus compare-and-set lifecycle state."""

    def __init__(self, database: str | Path, *, timeout: float = 5.0):
        self.database = Path(database).expanduser()
        self.timeout = timeout

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        connection = _open_existing(self.database, read_only=not write, timeout=self.timeout)
        try:
            try:
                version = connection.execute(
                    "SELECT value FROM journal_meta WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.Error as exc:
                raise JournalSchemaError("File is not a valid operation journal") from exc
            if version is None or version["value"] != str(JOURNAL_VERSION):
                raise JournalSchemaError("Unsupported operation journal schema")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise JournalBusyError("Operation journal is busy; no success was recorded") from exc
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(connection: sqlite3.Connection, operation_uuid: str):
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_uuid=?", (operation_uuid,)
        ).fetchone()
        if row is None:
            raise OperationNotFoundError(f"Unknown operation UUID: {operation_uuid}")
        return row

    def plan(self, *, library_uuid: str, media_uuid: str, revision_uuid: str | None,
             action: str, source_path: str, destination_path: str,
             operation_uuid: str | None = None) -> dict[str, Any]:
        """Persist a planned operation; retry with the same UUID is idempotent."""
        library_uuid = _uuid(library_uuid, "library_uuid")
        media_uuid = _uuid(media_uuid, "media_uuid")
        revision_uuid = _uuid(revision_uuid, "revision_uuid", optional=True)
        operation_uuid = _uuid(str(uuid.uuid4()) if operation_uuid is None else operation_uuid,
                               "operation_uuid")
        if not isinstance(action, str) or action not in ACTIONS:
            raise JournalError(f"Unsupported operation action: {action}")
        if not isinstance(source_path, str) or not source_path:
            raise JournalError("source_path must be a non-empty string")
        if not isinstance(destination_path, str) or not destination_path:
            raise JournalError("destination_path must be a non-empty string")
        with self._transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM operations WHERE operation_uuid=?", (operation_uuid,)
            ).fetchone()
            if existing:
                wanted = (library_uuid, media_uuid, revision_uuid, action, source_path, destination_path)
                actual = tuple(existing[key] for key in (
                    "library_uuid", "media_uuid", "revision_uuid", "action", "source_path", "destination_path"
                ))
                if actual != wanted:
                    raise StateConflictError("Operation UUID already belongs to different immutable details")
                return self._as_dict(existing, status="idempotent")
            now = _now()
            connection.execute(
                "INSERT INTO operations(operation_uuid,library_uuid,media_uuid,revision_uuid,action,"
                "source_path,destination_path,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'planned',?,?)",
                (operation_uuid, library_uuid, media_uuid, revision_uuid, action,
                 source_path, destination_path, now, now),
            )
            return self._as_dict(self._row(connection, operation_uuid), status="planned")

    @staticmethod
    def _as_dict(row, *, status: str | None = None) -> dict[str, Any]:
        result = dict(row)
        if status is not None:
            result["result"] = status
        return result

    def get(self, operation_uuid: str) -> dict[str, Any]:
        operation_uuid = _uuid(operation_uuid, "operation_uuid")
        with self._transaction(write=False) as connection:
            return self._as_dict(self._row(connection, operation_uuid))

    def list_unfinished(self, *, library_uuid: str | None = None) -> list[dict[str, Any]]:
        """List all operations needing recovery or review; complete rows are excluded."""
        if library_uuid is not None:
            library_uuid = _uuid(library_uuid, "library_uuid")
        with self._transaction(write=False) as connection:
            if library_uuid:
                rows = connection.execute(
                    "SELECT * FROM operations WHERE state!='complete' AND library_uuid=? "
                    "ORDER BY created_at,operation_uuid", (library_uuid,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM operations WHERE state!='complete' ORDER BY created_at,operation_uuid"
                ).fetchall()
            return [self._as_dict(row) for row in rows]

    def transition(self, operation_uuid: str, *, expected_state: str,
                   target_state: str, detail: str | None = None) -> dict[str, Any]:
        """CAS a legal lifecycle step or terminal conflict/error outcome.

        Repeating a successful stage callback is idempotent, including when the
        operation has already reached a later normal stage. Terminal failures do
        not masquerade as successful callbacks.
        """
        operation_uuid = _uuid(operation_uuid, "operation_uuid")
        if expected_state not in STATES or target_state not in STATES:
            raise InvalidTransitionError("Unknown operation journal state")
        allowed = {
            "planned": {"disk_done", "conflict", "error"},
            "disk_done": {"metadata_done", "conflict", "error"},
            "metadata_done": {"complete", "conflict", "error"},
        }
        if target_state not in allowed.get(expected_state, set()):
            raise InvalidTransitionError(f"Illegal transition {expected_state} -> {target_state}")
        if target_state in {"error", "conflict"} and (not isinstance(detail, str) or not detail.strip()):
            raise JournalError(f"{target_state} transition requires a non-empty detail")
        with self._transaction(write=True) as connection:
            row = self._row(connection, operation_uuid)
            current = row["state"]
            if current == target_state:
                detail_key = "error_text" if target_state == "error" else "conflict_text"
                if target_state in {"error", "conflict"} and row[detail_key] != detail:
                    raise StateConflictError("Repeated terminal transition has different detail")
                return self._as_dict(row, status="idempotent")
            if target_state in _NORMAL_ORDER and current in _NORMAL_ORDER:
                if _NORMAL_ORDER.index(current) > _NORMAL_ORDER.index(target_state):
                    return self._as_dict(row, status="idempotent")
            if current != expected_state:
                raise StateConflictError(
                    f"Operation is {current}; expected {expected_state} for {target_state}"
                )

            now = _now()
            timestamp_column = {
                "disk_done": "disk_done_at", "metadata_done": "metadata_done_at",
                "complete": "completed_at", "conflict": "conflict_at", "error": "error_at",
            }[target_state]
            detail_column = {"conflict": "conflict_text", "error": "error_text"}.get(target_state)
            assignments = ["state=?", "updated_at=?", f"{timestamp_column}=?"]
            values: list[Any] = [target_state, now, now]
            if detail_column:
                assignments.append(f"{detail_column}=?")
                values.append(detail)
            values.extend((operation_uuid, expected_state))
            cursor = connection.execute(
                f"UPDATE operations SET {','.join(assignments)} WHERE operation_uuid=? AND state=?",
                values,
            )
            if cursor.rowcount != 1:
                raise StateConflictError("Operation state changed before the journal update")
            return self._as_dict(self._row(connection, operation_uuid), status="applied")

    def mark_disk_done(self, operation_uuid: str) -> dict[str, Any]:
        return self.transition(operation_uuid, expected_state="planned", target_state="disk_done")

    def mark_metadata_done(self, operation_uuid: str) -> dict[str, Any]:
        return self.transition(operation_uuid, expected_state="disk_done", target_state="metadata_done")

    def mark_complete(self, operation_uuid: str) -> dict[str, Any]:
        return self.transition(operation_uuid, expected_state="metadata_done", target_state="complete")

    def mark_conflict(self, operation_uuid: str, detail: str) -> dict[str, Any]:
        existing = self.get(operation_uuid)
        current = existing["state"]
        if current == "conflict":
            if existing["conflict_text"] != detail:
                raise StateConflictError("Repeated conflict callback has different detail")
            existing["result"] = "idempotent"
            return existing
        return self.transition(operation_uuid, expected_state=current,
                               target_state="conflict", detail=detail)

    def mark_error(self, operation_uuid: str, detail: str) -> dict[str, Any]:
        existing = self.get(operation_uuid)
        current = existing["state"]
        if current == "error":
            if existing["error_text"] != detail:
                raise StateConflictError("Repeated error callback has different detail")
            existing["result"] = "idempotent"
            return existing
        return self.transition(operation_uuid, expected_state=current,
                               target_state="error", detail=detail)
