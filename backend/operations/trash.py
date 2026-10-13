"""Catalog-backed application trash built on the operation journal.

The store is independent from the catalog and is initialized explicitly. This
service never calls the operating-system recycle bin.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any

from ..storage.catalog import CatalogRepository
from ..storage.server_catalog import ServerCatalog
from .catalog_commit import CatalogOperationCommit
from .files import JournaledFileOperations
from .journal import OperationJournal


class TrashError(RuntimeError):
    pass


class TrashStoreUnavailable(TrashError):
    pass


class TrashStoreSchemaError(TrashError):
    pass


class _ScopedJournal:
    """Journal facade limiting trash recovery to this store's operation IDs."""
    def __init__(self, journal, operation_ids):
        self._journal = journal
        self._operation_ids = set(operation_ids)

    def __getattr__(self, name):
        return getattr(self._journal, name)

    def list_unfinished(self, *, library_uuid=None):
        return [row for row in self._journal.list_unfinished(library_uuid=library_uuid)
                if row["operation_uuid"] in self._operation_ids]


_VERSION = 1
_SCHEMA = """
CREATE TABLE trash_schema(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
CREATE TABLE trash_item(
 key TEXT PRIMARY KEY,
 library_uuid TEXT NOT NULL,
 media_uuid TEXT NOT NULL,
 revision_uuid TEXT,
 name TEXT NOT NULL,
 orig_path TEXT NOT NULL,
 payload_path TEXT NOT NULL,
 size INTEGER NOT NULL CHECK(size >= 0),
 trashed_at INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending_trash','trashed','pending_restore','restored','purging','purged','conflict')),
 trash_operation_uuid TEXT NOT NULL UNIQUE,
 restore_operation_uuid TEXT UNIQUE,
 purge_operation_uuid TEXT,
 restore_path TEXT,
 updated_at INTEGER NOT NULL
);
CREATE INDEX trash_state_time_idx ON trash_item(state,trashed_at DESC);
"""
_CHECKSUM = hashlib.sha256(f"{_VERSION}\n{_SCHEMA}".encode("utf-8")).hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root) and _path_key(path) != _path_key(root)
    except ValueError:
        return False


class CatalogTrash:
    """Owns catalog-aware trash operations for one explicit library/root."""

    def __init__(self, database, library_uuid, root_path, trash_dir, journal_path, evidence_dir):
        self.database = Path(database).expanduser().absolute()
        self.library_uuid = str(uuid.UUID(str(library_uuid)))
        self.root_path = Path(root_path).expanduser().absolute()
        self.trash_dir = Path(trash_dir).expanduser().absolute()
        self.journal = OperationJournal(journal_path)
        self.executor = JournaledFileOperations(self.journal, evidence_dir)
        self.catalog = ServerCatalog(self.database, self.library_uuid)
        self.catalog_commit = CatalogOperationCommit(self.database)
        self.store = Path(trash_dir).expanduser().absolute().parent / "trash-state.sqlite"

    def initialize(self) -> None:
        """Explicitly create/validate the service-owned trash index database."""
        self.store.parent.mkdir(parents=True, exist_ok=True)
        if self.store.exists():
            connection = self._connect(read_only=True)
            try:
                self._check(connection)
            finally:
                connection.close()
            return
        connection = sqlite3.connect(str(self.store), timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + _SCHEMA +
                f"\nINSERT INTO trash_schema(version,checksum) VALUES ({_VERSION},'{_CHECKSUM}');\nCOMMIT;"
            )
        except Exception:
            connection.rollback()
            connection.close()
            self.store.unlink(missing_ok=True)
            raise
        connection.close()

    def _connect(self, *, read_only: bool):
        try:
            path = self.store.resolve(strict=True)
            uri = path.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            raise TrashStoreUnavailable(f"Cannot open existing trash index: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._check(connection)
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _check(connection):
        try:
            row = connection.execute("SELECT version,checksum FROM trash_schema").fetchone()
        except sqlite3.Error as exc:
            raise TrashStoreSchemaError("Trash index has no supported schema") from exc
        if row is None or row["version"] > _VERSION:
            raise TrashStoreSchemaError("Trash index is newer or uninitialized")
        if row["version"] != _VERSION or row["checksum"] != _CHECKSUM:
            raise TrashStoreSchemaError("Trash index version/checksum mismatch")

    def _write(self, callback):
        connection = self._connect(read_only=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = callback(connection)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _root_matches_catalog(self, root_uuid: str) -> None:
        connection = self.catalog.repository._transaction(write=False)
        with connection as db:
            row = db.execute(
                "SELECT r.path FROM library_root r JOIN library l ON l.id=r.library_id "
                "WHERE r.uuid=? AND l.uuid=?", (root_uuid, self.library_uuid)
            ).fetchone()
            if row is None or _path_key(Path(row["path"])) != _path_key(self.root_path):
                raise TrashError("Configured root does not match the asset's catalog root")

    def for_asset(self, media_uuid: str, revision_uuid: str) -> "CatalogTrash":
        """Bind an operation to its asset's registered root in this Library.

        The returned service retains all single-root checks. Navigation state
        never supplies the root for a different asset or historical trash item.
        """
        with self.catalog.repository._transaction(write=False) as db:
            row = db.execute(
                "SELECT rt.path FROM media_asset a "
                "JOIN library l ON l.id=a.library_id "
                "JOIN library_root rt ON rt.id=a.root_id AND rt.library_id=a.library_id "
                "JOIN media_revision mr ON mr.asset_id=a.id AND mr.library_id=a.library_id "
                "WHERE l.uuid=? AND a.uuid=? AND mr.uuid=?",
                (self.library_uuid, media_uuid, revision_uuid),
            ).fetchone()
        if row is None:
            raise TrashError("Trash target identity does not belong to this Library")
        scoped = CatalogTrash(self.database, self.library_uuid, row["path"],
                              self.trash_dir, self.journal.database,
                              self.executor.evidence_directory)
        scoped.initialize()
        return scoped

    def for_path(self, path) -> "CatalogTrash":
        asset = self.catalog.current(Path(path).expanduser().absolute())
        if asset is None:
            raise TrashError("Path has no catalog identity")
        return self.for_asset(asset["mediaId"], asset["revisionId"])

    def for_item(self, key: str) -> "CatalogTrash":
        item = self._item(key)
        return self.for_asset(item["media_uuid"], item["revision_uuid"])

    def _asset_for_path(self, path: Path) -> dict[str, Any]:
        path = path.absolute()
        if not _inside(path, self.root_path):
            raise TrashError("Path is outside the configured library root")
        asset = self.catalog.current(path)
        if asset is None:
            raise TrashError("Path has no catalog identity")
        self._root_matches_catalog(asset["rootId"])
        if asset["path"] != str(path):
            raise TrashError("Path does not match the catalog's exact locator")
        return asset

    def _payload(self, key: str, name: str) -> Path:
        return self.trash_dir / (key + Path(name).suffix)

    def move(self, path) -> tuple[str | None, str | None]:
        """Move one catalogued file to app trash; legacy form is (error, key)."""
        source = Path(path).expanduser().absolute()
        try:
            if not source.is_file() or source.is_symlink():
                raise TrashError("File is missing or is not a regular file")
            asset = self._asset_for_path(source)
            stat = source.stat()
            key = str(uuid.uuid4())
            operation_uuid = str(uuid.uuid4())
            payload = self._payload(key, source.name)
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            now = int(time.time())
            def insert(db):
                db.execute(
                    "INSERT INTO trash_item(key,library_uuid,media_uuid,revision_uuid,name,orig_path,payload_path,size,trashed_at,state,trash_operation_uuid,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'pending_trash',?,?)",
                    (key, self.library_uuid, asset["mediaId"], asset["revisionId"], source.name,
                     str(source), str(payload), int(stat.st_size), now, operation_uuid, now))
            self._write(insert)

            def commit(operation):
                self.catalog_commit(operation)
                def mark(db):
                    cursor = db.execute(
                        "UPDATE trash_item SET state='trashed',updated_at=? WHERE key=? AND trash_operation_uuid=? AND state IN ('pending_trash','trashed')",
                        (int(time.time()), key, operation["operation_uuid"]))
                    if cursor.rowcount != 1:
                        raise TrashError("Trash row is missing or no longer pending")
                self._write(mark)

            self.executor.execute_move(
                library_uuid=self.library_uuid, media_uuid=asset["mediaId"],
                revision_uuid=asset["revisionId"], action="trash", source_path=source,
                destination_path=payload, allowed_roots=[self.root_path, self.trash_dir],
                metadata_commit=commit, operation_uuid=operation_uuid)
            return None, key
        except Exception as exc:
            return str(exc), None

    @staticmethod
    def _candidate(original: Path) -> Path:
        if not os.path.lexists(original):
            return original
        stem, suffix = original.stem, original.suffix
        for number in range(1, 100000):
            candidate = original.with_name(f"{stem}_{number}{suffix}")
            if not os.path.lexists(candidate):
                return candidate
        raise TrashError("No free restore filename is available")

    def restore(self, key: str) -> dict[str, Any]:
        try:
            row = self._item(key)
            if row["state"] != "trashed":
                raise TrashError("Trash item is not in a restorable state")
            payload = Path(row["payload_path"])
            if not payload.is_file():
                raise TrashError("Trash payload is missing")
            original = Path(row["orig_path"])
            if not _inside(original, self.root_path):
                raise TrashError("Original path is outside the configured library root")
            destination = self._candidate(original)
            operation_uuid = str(uuid.uuid4())
            self._write(lambda db: db.execute(
                "UPDATE trash_item SET state='pending_restore',restore_operation_uuid=?,restore_path=?,updated_at=? WHERE key=? AND state='trashed'",
                (operation_uuid, str(destination), int(time.time()), key)))

            def commit(operation):
                self.catalog_commit(operation)
                def mark(db):
                    cursor = db.execute(
                        "UPDATE trash_item SET state='restored',updated_at=? WHERE key=? AND restore_operation_uuid=? AND state IN ('pending_restore','restored')",
                        (int(time.time()), key, operation["operation_uuid"]))
                    if cursor.rowcount != 1:
                        raise TrashError("Restore row is missing or no longer pending")
                self._write(mark)

            result = self.executor.execute_move(
                library_uuid=self.library_uuid, media_uuid=row["media_uuid"],
                revision_uuid=row["revision_uuid"], action="restore", source_path=payload,
                destination_path=destination, allowed_roots=[self.root_path, self.trash_dir],
                metadata_commit=commit, operation_uuid=operation_uuid)
            return {"ok": True, "key": key, "path": str(destination),
                    "renamed": destination.name != original.name,
                    "state": result["state"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "pending": True}

    def _item(self, key: str):
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM trash_item WHERE key=? AND library_uuid=?",
                (key, self.library_uuid),
            ).fetchone()
            if row is None:
                raise TrashError("Trash item does not exist")
            return dict(row)
        finally:
            connection.close()

    def list_items(self) -> list[dict[str, Any]]:
        """Legacy-compatible rows for actual trashed payloads only."""
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                "SELECT * FROM trash_item WHERE library_uuid=? AND state='trashed' ORDER BY trashed_at DESC,key",
                (self.library_uuid,)).fetchall()
            output = []
            for row in rows:
                if not Path(row["payload_path"]).is_file():
                    continue
                suffix = Path(row["name"]).suffix.lower()
                kind = "image" if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"} else (
                    "video" if suffix in {".mp4", ".webm", ".mov", ".mkv", ".avi"} else "file")
                output.append({"key": row["key"], "name": row["name"], "orig": row["orig_path"],
                               "t": row["trashed_at"], "size": row["size"], "type": kind})
            return output
        finally:
            connection.close()

    def pending(self) -> list[dict[str, Any]]:
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                "SELECT key,media_uuid,revision_uuid,orig_path,payload_path,state,trash_operation_uuid,restore_operation_uuid,restore_path "
                "FROM trash_item WHERE library_uuid=? AND state IN ('pending_trash','pending_restore','purging','conflict') ORDER BY updated_at,key",
                (self.library_uuid,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def index(self) -> dict[str, dict[str, Any]]:
        """Return legacy key->entry projection, filtered to live trashed payloads."""
        return {item["key"]: {"name": item["name"], "orig": item["orig"],
                              "t": item["t"], "size": item["size"]}
                for item in self.list_items()}

    def _metadata_for_operation(self, operation):
        self.catalog_commit(operation)
        opid, action = operation["operation_uuid"], operation["action"]
        def mark(db):
            if action == "trash":
                sql = "UPDATE trash_item SET state='trashed',updated_at=? WHERE trash_operation_uuid=? AND state IN ('pending_trash','trashed')"
            elif action == "restore":
                sql = "UPDATE trash_item SET state='restored',updated_at=? WHERE restore_operation_uuid=? AND state IN ('pending_restore','restored')"
            else:
                raise TrashError("Unsupported recovery action")
            cursor = db.execute(sql, (int(time.time()), opid))
            if cursor.rowcount != 1:
                raise TrashError("No matching pending trash record for operation")
        self._write(mark)

    def recover(self) -> list[dict[str, Any]]:
        """Resume journal operations; metadata callback is idempotent by operation UUID."""
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                "SELECT trash_operation_uuid,restore_operation_uuid FROM trash_item "
                "WHERE state IN ('pending_trash','trashed','pending_restore','restored')"
            ).fetchall()
            op_ids = {opid for row in rows for opid in row if opid}
        finally:
            connection.close()
        scoped = JournaledFileOperations(_ScopedJournal(self.journal, op_ids), self.executor.evidence_directory)
        results = scoped.recover(self._metadata_for_operation, library_uuid=self.library_uuid)
        connection = self._connect(read_only=True)
        try:
            purge_keys = [row[0] for row in connection.execute(
                "SELECT key FROM trash_item WHERE library_uuid=? AND state='purging'",
                (self.library_uuid,)).fetchall()]
        finally:
            connection.close()
        if purge_keys:
            results.append({"purge_recovery": self.purge(purge_keys)})
        return results

    def purge(self, keys: list[str]) -> dict[str, Any]:
        """Permanently remove only explicit keys; empty input is always a no-op."""
        if not isinstance(keys, list) or not keys:
            return {"ok": True, "purged": 0, "failed": []}
        purged, failed = 0, []
        for key in dict.fromkeys(str(item) for item in keys):
            try:
                row = self._item(key)
                if row["library_uuid"] != self.library_uuid or row["state"] not in ("trashed", "purging"):
                    raise TrashError("Item is not an explicitly selected trashed item")
                payload = Path(row["payload_path"])
                if not _inside(payload, self.trash_dir):
                    raise TrashError("Payload path is outside this service's trash directory")
                purge_op = row["purge_operation_uuid"] or str(uuid.uuid4())
                self._write(lambda db: db.execute(
                    "UPDATE trash_item SET state='purging',purge_operation_uuid=?,updated_at=? WHERE key=? AND state IN ('trashed','purging')",
                    (purge_op, int(time.time()), key)))
                if os.path.lexists(payload):
                    if payload.is_symlink() or not payload.is_file():
                        raise TrashError("Payload is not a regular file; left untouched")
                    payload.unlink()
                self._write(lambda db: db.execute(
                    "UPDATE trash_item SET state='purged',updated_at=? WHERE key=? AND purge_operation_uuid=? AND state='purging'",
                    (int(time.time()), key, purge_op)))
                purged += 1
            except Exception as exc:
                # CMV3-LOCAL-FIX(2026-10-11 · 承接 §6.1/P0-2「删不掉的文件如实报失败，且条目
                #   必须**保留在列表里**」)：上面第 413 行已把 state 置成 'purging' 并落库；
                #   若紧接着的 `payload.unlink()` 失败（只读 / 被占用 / 权限），异常直接跳到
                #   这里，state 就**停在 'purging'**。而 `list_items()` 只返回
                #   `state='trashed'` 的行 ⇒ 条目从回收站列表里消失，文件却仍躺在回收站目录
                #   里 —— 正是 legacy 侧 §6.1 修掉的「不可见孤儿」缺陷（白占空间、不受自动
                #   清理管辖、用户看不见也就无法重试）。
                #   这里把 state 回滚到 'trashed'，让用户看得见、能重试。
                #   ⚠️ 回滚失败绝不能吞掉原始错误 —— 下面的 failed 明细照常上报。
                try:
                    self._write(lambda db, _k=key, _op=purge_op: db.execute(
                        "UPDATE trash_item SET state='trashed',updated_at=? "
                        "WHERE key=? AND purge_operation_uuid=? AND state='purging'",
                        (int(time.time()), _k, _op)))
                except Exception:
                    pass
                failed.append({"key": key, "error": str(exc)})
        return {"ok": not failed, "purged": purged, "failed": failed}
