"""Profile-scoped recent-media storage as an isolated catalog extension."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from .database import initialize_schema


RECENT_VERSION = 1
RECENT_SQL = """CREATE TABLE recent_state (
 profile_id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL,
 serial INTEGER NOT NULL CHECK(serial >= 0),
 FOREIGN KEY(library_id,profile_id) REFERENCES local_profile(library_id,id) ON DELETE RESTRICT
);
CREATE TABLE recent_view (
 profile_id INTEGER NOT NULL, library_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
 sequence INTEGER NOT NULL CHECK(sequence > 0), viewed_at TEXT NOT NULL, action TEXT NOT NULL,
 PRIMARY KEY(profile_id,asset_id), UNIQUE(profile_id,sequence),
 FOREIGN KEY(library_id,profile_id) REFERENCES local_profile(library_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(library_id,asset_id) REFERENCES media_asset(library_id,id) ON DELETE RESTRICT
);
CREATE INDEX recent_view_order_idx ON recent_view(profile_id,sequence DESC);
CREATE TABLE recent_import (
 profile_id INTEGER PRIMARY KEY, imported_at TEXT NOT NULL,
 FOREIGN KEY(profile_id) REFERENCES local_profile(id) ON DELETE RESTRICT
);"""
RECENT_CHECKSUM = hashlib.sha256(f"{RECENT_VERSION}\n{RECENT_SQL}".encode()).hexdigest()
REPORT_SQL = "CREATE TABLE recent_import_report (profile_id INTEGER PRIMARY KEY, report_json TEXT NOT NULL, FOREIGN KEY(profile_id) REFERENCES local_profile(id) ON DELETE RESTRICT)"
REPORT_CHECKSUM = hashlib.sha256(('2\n' + REPORT_SQL).encode()).hexdigest()


class RecentStorageError(RuntimeError):
    """A recent-storage request could not be safely applied."""


class RecentSchemaError(RecentStorageError):
    """The extension schema is missing, partial, or has changed."""


class RecentNotFoundError(RecentStorageError):
    """A requested profile or media UUID does not exist."""


class RecentScopeError(RecentStorageError):
    """The media exists outside the requested profile's library."""


class RecentRepository:
    """Store the latest visit per asset; sequence orders reads and survives clear."""

    def __init__(self, database: str | Path):
        self.database = Path(database)

    def initialize(self) -> None:
        """Install or verify this extension without changing the core catalog schema."""
        with self._transaction(write=True):
            pass

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        try:
            path = self.database.resolve(strict=True)
            if not path.is_file():
                raise RecentStorageError("Catalog database does not exist")
            conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                   uri=True, timeout=5)
        except (OSError, sqlite3.Error) as exc:
            raise RecentStorageError(f"Cannot open catalog: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
                raise RecentSchemaError("Initialized catalog required")
            initialize_schema(conn)
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._validate(conn, create=write)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _validate(conn: sqlite3.Connection, *, create: bool) -> None:
        found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recent_schema_migrations'").fetchone()
        if not found:
            partial = conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'recent_%' LIMIT 1").fetchone()
            if partial or not create:
                raise RecentSchemaError("Recent extension is absent or only partially installed")
            conn.execute("CREATE TABLE recent_schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL)")
            for statement in RECENT_SQL.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO recent_schema_migrations VALUES (?,?)", (RECENT_VERSION, RECENT_CHECKSUM))
            conn.execute(REPORT_SQL)
            conn.execute("INSERT INTO recent_schema_migrations VALUES (?,?)", (2, REPORT_CHECKSUM))
            return
        rows = conn.execute("SELECT version,checksum FROM recent_schema_migrations ORDER BY version").fetchall()
        history = [(r['version'], r['checksum']) for r in rows]
        if history == [(RECENT_VERSION, RECENT_CHECKSUM)] and create:
            conn.execute(REPORT_SQL)
            conn.execute("INSERT INTO recent_schema_migrations VALUES (?,?)", (2, REPORT_CHECKSUM))
            history.append((2, REPORT_CHECKSUM))
        if history != [(RECENT_VERSION, RECENT_CHECKSUM), (2, REPORT_CHECKSUM)]:
            raise RecentSchemaError("Recent extension version or checksum mismatch")

    @staticmethod
    def _profile(conn, profile_uuid):
        row = conn.execute("SELECT id,uuid,library_id FROM local_profile WHERE uuid=?", (profile_uuid,)).fetchone()
        if row is None:
            raise RecentNotFoundError("Unknown profile UUID")
        return row

    @staticmethod
    def _asset(conn, profile, asset_uuid):
        row = conn.execute("SELECT id,uuid,library_id FROM media_asset WHERE uuid=?", (asset_uuid,)).fetchone()
        if row is None:
            raise RecentNotFoundError("Unknown media UUID")
        if row['library_id'] != profile['library_id']:
            raise RecentScopeError("Media belongs to another library")
        return row

    @staticmethod
    def _next(conn, profile):
        conn.execute("INSERT INTO recent_state(profile_id,library_id,serial) VALUES (?,?,0) ON CONFLICT(profile_id) DO NOTHING",
                     (profile['id'], profile['library_id']))
        conn.execute("UPDATE recent_state SET serial=serial+1 WHERE profile_id=?", (profile['id'],))
        return conn.execute("SELECT serial FROM recent_state WHERE profile_id=?", (profile['id'],)).fetchone()[0]

    def add(self, profile_uuid: str, asset_uuid: str, action: str, viewed_at: str | int | None = None) -> dict:
        if not isinstance(action, str) or not action:
            raise RecentStorageError("action must be non-empty text")
        timestamp = str(int(time.time()) if viewed_at is None else viewed_at)
        with self._transaction(write=True) as conn:
            profile = self._profile(conn, profile_uuid)
            asset = self._asset(conn, profile, asset_uuid)
            sequence = self._next(conn, profile)
            conn.execute("INSERT INTO recent_view(profile_id,library_id,asset_id,sequence,viewed_at,action) VALUES (?,?,?,?,?,?) "
                         "ON CONFLICT(profile_id,asset_id) DO UPDATE SET sequence=excluded.sequence,viewed_at=excluded.viewed_at,action=excluded.action",
                         (profile['id'], profile['library_id'], asset['id'], sequence, timestamp, action))
            return {"sequence": sequence, "mediaUuid": asset_uuid, "time": timestamp, "action": action}

    def clear(self, profile_uuid: str) -> dict:
        with self._transaction(write=True) as conn:
            profile = self._profile(conn, profile_uuid)
            conn.execute("DELETE FROM recent_view WHERE profile_id=?", (profile['id'],))
            sequence = self._next(conn, profile)
            return {"count": 0, "generation": sequence}

    def read(self, profile_uuid: str, *, limit: int = 200) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise RecentStorageError("limit must be a non-negative integer")
        with self._transaction(write=False) as conn:
            profile = self._profile(conn, profile_uuid)
            rows = conn.execute("SELECT a.uuid AS media_uuid,a.path,v.sequence,v.viewed_at,v.action "
                                "FROM recent_view v JOIN media_asset a ON a.id=v.asset_id AND a.library_id=v.library_id "
                                "WHERE v.profile_id=? ORDER BY v.sequence DESC LIMIT ?", (profile['id'], limit)).fetchall()
            return [{"mediaUuid": r['media_uuid'], "path": r['path'], "sequence": r['sequence'],
                     "time": r['viewed_at'], "action": r['action']} for r in rows]

    @staticmethod
    def read_generation_in_transaction(conn: sqlite3.Connection, profile_uuid: str) -> str:
        """Return a cursor-binding generation using the caller's existing transaction."""
        RecentRepository._validate(conn, create=False)
        profile = RecentRepository._profile(conn, profile_uuid)
        row = conn.execute("SELECT serial FROM recent_state WHERE profile_id=?", (profile['id'],)).fetchone()
        return f"recent:{profile['uuid']}:{row['serial'] if row else 0}"

    def import_report(self, profile_uuid: str) -> dict:
        with self._transaction(write=False) as conn:
            profile = self._profile(conn, profile_uuid)
            saved = conn.execute("SELECT report_json FROM recent_import_report WHERE profile_id=?", (profile['id'],)).fetchone()
            if saved:
                return json.loads(saved['report_json'])
            marker = conn.execute("SELECT 1 FROM recent_import WHERE profile_id=?", (profile['id'],)).fetchone()
            return {"status": "report_unavailable" if marker else "not_imported", "accepted": 0,
                    "rejected": [], "skippedExisting": [], "reportUnavailable": bool(marker)}

    def import_legacy(self, profile_uuid: str, records: list) -> dict:
        """Import legacy [{path,action,time}] once; malformed/unmatched rows are reported."""
        if not isinstance(records, list):
            raise RecentStorageError("legacy records must be a list")
        accepted, rejected, skipped_existing = 0, [], []
        with self._transaction(write=True) as conn:
            profile = self._profile(conn, profile_uuid)
            marker = conn.execute("SELECT 1 FROM recent_import WHERE profile_id=?", (profile['id'],)).fetchone()
            if marker:
                saved = conn.execute("SELECT report_json FROM recent_import_report WHERE profile_id=?", (profile['id'],)).fetchone()
                report = json.loads(saved['report_json']) if saved else {"accepted": 0, "rejected": [], "reportUnavailable": True}
                return {**report, "status": "already_imported"}
            protected_order = [r[0] for r in conn.execute(
                "SELECT asset_id FROM recent_view WHERE profile_id=? ORDER BY sequence", (profile['id'],)).fetchall()]
            protected_assets = set(protected_order)
            # Legacy files are newest-first; insert oldest-first to preserve visible order.
            for index in range(len(records) - 1, -1, -1):
                item = records[index]
                try:
                    if not isinstance(item, dict) or not item.get('path'):
                        raise ValueError("expected object with path")
                    path = str(item['path'])
                    rows = conn.execute("SELECT id,uuid,library_id FROM media_asset WHERE library_id=? AND (path=? OR live_path_key=?) ORDER BY id",
                                        (profile['library_id'], path, path)).fetchall()
                    if len(rows) != 1:
                        raise ValueError("path does not identify exactly one catalog asset")
                    asset = rows[0]
                    if asset['id'] in protected_assets:
                        skipped_existing.append(index)
                        continue
                    action = str(item.get('action') or '')
                    raw_time = item.get('time', 0)
                    if isinstance(raw_time, bool) or not isinstance(raw_time, (int, str)):
                        raise ValueError("time must be an integer or integer text")
                    timestamp = str(raw_time or 0)
                    if not re.fullmatch(r'[+-]?[0-9]+', timestamp):
                        raise ValueError("time must be an integer or integer text")
                    seq = self._next(conn, profile)
                    conn.execute("INSERT INTO recent_view VALUES (?,?,?,?,?,?) ON CONFLICT(profile_id,asset_id) DO UPDATE SET sequence=excluded.sequence,viewed_at=excluded.viewed_at,action=excluded.action",
                                 (profile['id'], profile['library_id'], asset['id'], seq, timestamp, action))
                    accepted += 1
                except (ValueError, TypeError) as exc:
                    rejected.append({"index": index, "reason": str(exc)})
            # New live visits remain above imported history without changing their payloads.
            if accepted:
                for asset_id in protected_order:
                    sequence = self._next(conn, profile)
                    conn.execute("UPDATE recent_view SET sequence=? WHERE profile_id=? AND asset_id=?",
                                 (sequence,profile['id'],asset_id))
            conn.execute("INSERT INTO recent_import(profile_id,imported_at) VALUES (?,?)",
                         (profile['id'], str(int(time.time()))))
            report = {"status": "imported", "accepted": accepted, "rejected": rejected,
                      "skippedExisting": skipped_existing}
            conn.execute("INSERT INTO recent_import_report VALUES (?,?)", (profile['id'],json.dumps(report,ensure_ascii=False)))
            return report
