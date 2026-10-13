"""Persistent, cancellable background model-file verification jobs.

Only this module's worker calls ``inspect_model_file``. Receipts are stored in
the catalog and can be read only through a completed, correctly scoped job.
"""

from __future__ import annotations

from backend.storage.integrity import foreign_key_violation
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

from .model_identity import ModelIdentityError, inspect_model_file

MODEL_VERIFICATION_SCHEMA_VERSION = 1
_JOB_SCHEMA_SQL = """
CREATE TABLE model_verification_job (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL REFERENCES library(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('running','completed','failed','cancelled','interrupted')),
    path TEXT NOT NULL,
    max_bytes INTEGER NOT NULL CHECK (max_bytes > 0),
    max_seconds REAL NOT NULL CHECK (max_seconds > 0 AND max_seconds <= 3600),
    progress_json TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    private_receipt_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT
)
"""
_RUNNING_INDEX_SQL = "CREATE UNIQUE INDEX model_verification_one_running ON model_verification_job (library_id) WHERE status='running'"


def _schema_checksum() -> str:
    return hashlib.sha256(
        f"{MODEL_VERIFICATION_SCHEMA_VERSION}\n{_JOB_SCHEMA_SQL}\n{_RUNNING_INDEX_SQL}".encode()
    ).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ModelVerificationJobError(RuntimeError):
    """Base error for the persistent model verification queue."""


class ModelVerificationJobUnavailableError(ModelVerificationJobError):
    """The initialized catalog cannot be opened safely."""


class ModelVerificationJobSchemaError(ModelVerificationJobError):
    """The job extension is absent or has an unexpected schema version."""


class ModelVerificationJobNotFoundError(ModelVerificationJobError):
    """The job does not exist in the requested library scope."""


class ModelVerificationJobConflictError(ModelVerificationJobError):
    """A verification worker is already occupying the single worker slot."""


class ModelVerificationJobs:
    HISTORY_LIMIT = 100

    def __init__(self, database: str | Path):
        self.database = Path(database).absolute()
        self._lock = threading.RLock()
        self._workers: dict[str, tuple[threading.Thread, threading.Event]] = {}

    @contextmanager
    def _transaction(self, *, write: bool, require_schema: bool = True) -> Iterator[sqlite3.Connection]:
        if self.database.is_symlink():
            raise ModelVerificationJobUnavailableError("Catalog path must not be a symlink")
        try:
            real = self.database.resolve(strict=True)
            if not real.is_file():
                raise ModelVerificationJobUnavailableError("Catalog database does not exist")
            conn = sqlite3.connect(real.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                   uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, ModelVerificationJobError):
                raise
            raise ModelVerificationJobUnavailableError(f"Cannot open verification catalog: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if foreign_key_violation(conn) is not None:
                raise ModelVerificationJobSchemaError("Catalog has foreign key violations")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
                raise ModelVerificationJobSchemaError("Core catalog schema is not initialized")
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise ModelVerificationJobSchemaError("Core catalog migration is missing")
            from ..storage.database import initialize_schema
            initialize_schema(conn)
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
        row = conn.execute(
            "SELECT version,checksum FROM model_verification_schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None or row["version"] != MODEL_VERIFICATION_SCHEMA_VERSION or row["checksum"] != _schema_checksum():
            raise ModelVerificationJobSchemaError("Model verification job schema is absent or has a checksum mismatch")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_verification_job'").fetchone():
            raise ModelVerificationJobSchemaError("Model verification job table is missing")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='model_verification_one_running'").fetchone():
            raise ModelVerificationJobSchemaError("Single-worker uniqueness index is missing")

    def initialize(self) -> int:
        """Explicitly install the job extension on an initialized catalog."""
        with self._transaction(write=True, require_schema=False) as conn:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_verification_schema_migrations'"
            ).fetchone()
            if present:
                self._validate_schema(conn)
                return MODEL_VERIFICATION_SCHEMA_VERSION
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_verification_job'"
            ).fetchone():
                raise ModelVerificationJobSchemaError("Partial model verification schema exists")
            conn.execute("CREATE TABLE model_verification_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))" )
            conn.execute(_JOB_SCHEMA_SQL)
            conn.execute(_RUNNING_INDEX_SQL)
            conn.execute("INSERT INTO model_verification_schema_migrations(version,checksum) VALUES (?,?)",
                         (MODEL_VERIFICATION_SCHEMA_VERSION, _schema_checksum()))
        return MODEL_VERIFICATION_SCHEMA_VERSION

    def start(self, library_uuid: str, path: str | Path,
              budgets: Mapping[str, int | float] | None = None) -> dict[str, str]:
        if budgets is None:
            budgets = {}
        if not isinstance(budgets, Mapping) or set(budgets) - {"max_bytes", "max_seconds"}:
            raise ValueError("budgets may contain only max_bytes and max_seconds")
        max_bytes = budgets.get("max_bytes", 32 * 1024**3)
        max_seconds = budgets.get("max_seconds", 120)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 3600:
            raise ValueError("max_seconds must be greater than zero and at most 3600")
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError("path must be a non-empty filesystem path")
        if not isinstance(library_uuid, str):
            raise ValueError("library_uuid must be a UUID string")
        try:
            library_uuid = str(uuid.UUID(library_uuid))
        except (ValueError, AttributeError) as exc:
            raise ValueError("library_uuid must be a valid UUID") from exc
        target = str(Path(path).absolute())
        job_uuid = str(uuid.uuid4())
        with self._lock:
            try:
                with self._transaction(write=True) as conn:
                    library = conn.execute("SELECT id FROM library WHERE uuid=?", (library_uuid,)).fetchone()
                    if library is None:
                        raise ModelVerificationJobNotFoundError("Library scope does not exist")
                    conn.execute(
                        "INSERT INTO model_verification_job(uuid,library_id,status,path,max_bytes,max_seconds,progress_json) "
                        "VALUES (?,?, 'running', ?,?,?,?)",
                        (job_uuid, library["id"], target, max_bytes, float(max_seconds), _json({"pass": 0, "bytesRead": 0, "sizeBytes": None})))
                    # Keep a finite history while preserving all running work.
                    conn.execute(
                        "DELETE FROM model_verification_job WHERE id IN (SELECT id FROM model_verification_job "
                        "WHERE status!='running' ORDER BY id DESC LIMIT -1 OFFSET ?)", (self.HISTORY_LIMIT - 1,))
            except sqlite3.IntegrityError as exc:
                raise ModelVerificationJobConflictError("A model verification is already running in this library") from exc
            cancel_event = threading.Event()
            worker = threading.Thread(target=self._run, args=(job_uuid, library_uuid, cancel_event),
                                      name=f"model-verify-{job_uuid[:8]}", daemon=True)
            self._workers[job_uuid] = (worker, cancel_event)
            try:
                worker.start()
            except Exception as exc:
                self._finish(job_uuid, "failed", error=f"Could not start verification worker: {exc}")
                self._workers.pop(job_uuid, None)
                raise
        return {"jobUUID": job_uuid, "status": "running"}

    def _run(self, job_uuid: str, library_uuid: str, cancel_event: threading.Event) -> None:
        try:
            with self._transaction(write=False) as conn:
                row = conn.execute(
                    "SELECT j.path,j.max_bytes,j.max_seconds,l.uuid AS library_uuid FROM model_verification_job j "
                    "JOIN library l ON l.id=j.library_id WHERE j.uuid=? AND l.uuid=?",
                    (job_uuid, library_uuid)).fetchone()
        except Exception as exc:
            try:
                self._finish(job_uuid, "failed", error=f"Could not load verification job: {exc}")
            except Exception:
                pass
            with self._lock:
                self._workers.pop(job_uuid, None)
            return
        if row is None:
            self._finish(job_uuid, "failed", error="Verification job scope disappeared")
            with self._lock:
                self._workers.pop(job_uuid, None)
            return
        last_progress_time = 0.0
        last_progress_bytes = 0
        cancel_cache = False
        last_cancel_check = 0.0
        size = None

        def is_cancelled(*, force: bool = False) -> bool:
            nonlocal cancel_cache, last_cancel_check
            if cancel_event.is_set():
                return True
            now = time.monotonic()
            if force or now - last_cancel_check >= 0.5:
                cancel_cache = self._cancel_requested(job_uuid)
                last_cancel_check = now
            return cancel_cache

        def progress(value: Mapping[str, int]) -> None:
            nonlocal last_progress_time, last_progress_bytes, size
            now = time.monotonic()
            read = int(value.get("bytesRead", 0))
            size = value.get("sizeBytes")
            if now - last_progress_time < 1.0 and read - last_progress_bytes < 64 * 1024**2:
                return
            self._write_progress(job_uuid, value)
            last_progress_time = now
            last_progress_bytes = read

        try:
            receipt = inspect_model_file(row["path"], max_bytes=row["max_bytes"],
                                         max_seconds=row["max_seconds"], cancelled=is_cancelled,
                                         progress=progress)
            self._write_progress(job_uuid, {"pass": 2, "bytesRead": receipt["bytesRead"],
                                            "sizeBytes": receipt["sizeBytes"]})
            if is_cancelled(force=True):
                self._finish(job_uuid, "cancelled", error="Model verification cancelled")
            else:
                self._finish(job_uuid, "completed", receipt=receipt)
        except ModelIdentityError as exc:
            cancelled = is_cancelled(force=True)
            self._finish(job_uuid, "cancelled" if cancelled else "failed",
                         error="Model verification cancelled" if cancelled else str(exc))
        except Exception as exc:
            self._finish(job_uuid, "failed", error=f"Verification worker failed: {exc}")
        finally:
            with self._lock:
                self._workers.pop(job_uuid, None)

    def _cancel_requested(self, job_uuid: str) -> bool:
        try:
            with self._transaction(write=False) as conn:
                row = conn.execute("SELECT cancel_requested FROM model_verification_job WHERE uuid=?", (job_uuid,)).fetchone()
                return row is None or bool(row["cancel_requested"])
        except Exception:
            # Fail closed: a lost catalog connection must not complete an unconfirmed job.
            return True

    def _write_progress(self, job_uuid: str, progress: Mapping[str, Any]) -> None:
        with self._transaction(write=True) as conn:
            conn.execute(
                "UPDATE model_verification_job SET progress_json=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE uuid=? AND status='running'",
                (_json({"pass": int(progress.get("pass", 0)),
                        "bytesRead": int(progress.get("bytesRead", 0)),
                        "sizeBytes": progress.get("sizeBytes")}), job_uuid))

    def _finish(self, job_uuid: str, status: str, *, receipt: Mapping[str, Any] | None = None,
                error: str | None = None) -> None:
        with self._transaction(write=True) as conn:
            if status == "completed":
                row = conn.execute("SELECT cancel_requested FROM model_verification_job "
                                   "WHERE uuid=? AND status='running'", (job_uuid,)).fetchone()
                if row is None:
                    return
                if row["cancel_requested"]:
                    status, receipt, error = "cancelled", None, "Model verification cancelled"
            conn.execute(
                "UPDATE model_verification_job SET status=?,private_receipt_json=?,error=?,finished_at="
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE uuid=? AND status='running'",
                (status, _json(receipt) if receipt is not None else None,
                 error[:1000] if error else None, job_uuid))

    def _lookup(self, conn: sqlite3.Connection, library_uuid: str, job_uuid: str):
        row = conn.execute(
            "SELECT j.uuid,j.status,j.progress_json,j.error,j.cancel_requested,j.created_at,j.updated_at,j.finished_at "
            "FROM model_verification_job j JOIN library l ON l.id=j.library_id "
            "WHERE j.uuid=? AND l.uuid=?", (job_uuid, library_uuid)).fetchone()
        if row is None:
            raise ModelVerificationJobNotFoundError("Verification job does not exist in this library scope")
        return row

    def status(self, library_uuid: str, job_uuid: str) -> dict[str, Any]:
        try:
            library_uuid = str(uuid.UUID(library_uuid))
            job_uuid = str(uuid.UUID(job_uuid))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ModelVerificationJobNotFoundError("Verification job does not exist in this library scope") from exc
        with self._transaction(write=False) as conn:
            row = self._lookup(conn, library_uuid, job_uuid)
            return {"jobUUID": row["uuid"], "status": row["status"],
                    "progress": json.loads(row["progress_json"]),
                    "cancelRequested": bool(row["cancel_requested"]),
                    "error": row["error"], "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"], "finishedAt": row["finished_at"]}

    def cancel(self, library_uuid: str, job_uuid: str) -> dict[str, Any]:
        try:
            library_uuid = str(uuid.UUID(library_uuid))
            job_uuid = str(uuid.UUID(job_uuid))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ModelVerificationJobNotFoundError("Verification job does not exist in this library scope") from exc
        with self._transaction(write=True) as conn:
            row = self._lookup(conn, library_uuid, job_uuid)
            if row["status"] == "running":
                conn.execute("UPDATE model_verification_job SET cancel_requested=1,updated_at="
                             "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE uuid=? AND status='running'",
                             (job_uuid,))
                with self._lock:
                    worker = self._workers.get(job_uuid)
                    if worker:
                        worker[1].set()
            return {"jobUUID": job_uuid, "status": row["status"], "cancelRequested": row["status"] == "running"}

    def read_verified_receipt(self, library_uuid: str, job_uuid: str) -> dict[str, Any]:
        try:
            library_uuid = str(uuid.UUID(library_uuid))
            job_uuid = str(uuid.UUID(job_uuid))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ModelVerificationJobNotFoundError("Verification job does not exist in this library scope") from exc
        with self._transaction(write=False) as conn:
            # Receipt column is fetched only after exact scope and completed-state checks.
            row = conn.execute(
                "SELECT j.private_receipt_json FROM model_verification_job j JOIN library l ON l.id=j.library_id "
                "WHERE j.uuid=? AND l.uuid=? AND j.status='completed'", (job_uuid, library_uuid)).fetchone()
            if row is None or row["private_receipt_json"] is None:
                raise ModelVerificationJobNotFoundError("Completed receipt does not exist in this library scope")
            return json.loads(row["private_receipt_json"])

    def recover_interrupted(self, *, confirm_exclusive_writer: bool = False) -> dict[str, int]:
        """Mark persisted running rows interrupted after an exclusive process restart."""
        if confirm_exclusive_writer is not True:
            raise ValueError("confirm_exclusive_writer=True is required for recovery")
        with self._lock:
            alive = [thread for thread, _ in self._workers.values() if thread.is_alive()]
            if alive:
                raise ModelVerificationJobConflictError("Cannot recover while this instance has a live worker")
            with self._transaction(write=True) as conn:
                cursor = conn.execute(
                    "UPDATE model_verification_job SET status='interrupted',error='Process restarted before verification completed',"
                    "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE status='running'")
                return {"interrupted": cursor.rowcount}
