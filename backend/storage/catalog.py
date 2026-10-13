"""Stable asset and revision identities for an already initialized catalog.

The repository never creates a database or schema. Paths remain exact locator
strings; this module does not infer case-insensitive matches or external moves.
"""

from __future__ import annotations

from backend.storage.integrity import foreign_key_violation
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterator, Mapping

from .database import initialize_schema, new_identity, revision_token
from .asset_lifecycle import visible_clause


class CatalogError(RuntimeError):
    """Base error for catalog identity operations."""


class CatalogUnavailableError(CatalogError):
    """The database does not exist or cannot be opened in place."""


class CatalogSchemaError(CatalogError):
    """The existing database has not been initialized by the storage layer."""


class IdentityNotFoundError(CatalogError):
    """A requested library, root, asset, or path is unknown."""


class ScopeMismatchError(CatalogError):
    """The requested UUID is not owned by the explicit library/root scope."""


class IdentityConflictError(CatalogError):
    """Two supplied identities disagree or a path is ambiguous/already bound."""


class FileChangedDuringVerification(CatalogError):
    """The file changed while its bytes were being hashed."""


def _uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise IdentityConflictError(f"Invalid {label} UUID") from exc


def _stat_values(value: Any) -> tuple[int, int]:
    try:
        size = int(value["st_size"] if isinstance(value, Mapping) else value.st_size)
        mtime_ns = int(value["st_mtime_ns"] if isinstance(value, Mapping)
                       else value.st_mtime_ns)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("stat must provide st_size and st_mtime_ns") from exc
    if size < 0:
        raise ValueError("st_size must not be negative")
    return size, mtime_ns


def _path_within(root: str, path: str) -> bool:
    try:
        root_abs = os.path.abspath(root)
        path_abs = os.path.abspath(path)
        common = os.path.commonpath((root_abs, path_abs))
    except (TypeError, ValueError, OSError):
        return False
    return os.path.normcase(common) == os.path.normcase(root_abs) and \
        os.path.normcase(path_abs) != os.path.normcase(root_abs)


def _path_owned_root(connection: sqlite3.Connection, library_id: int,
                     roots: list[sqlite3.Row], path: str) -> int:
    """Resolve established ownership first, then assign new paths to the deepest root."""
    existing = connection.execute(
        'SELECT DISTINCT root_id FROM media_asset WHERE library_id=? '
        'AND (live_path_key=? OR path=?)', (library_id, path, path)).fetchall()
    if len(existing) > 1:
        raise IdentityConflictError('Path has ambiguous historical root ownership')
    if existing:
        return int(existing[0]['root_id'])

    candidates = []
    absolute = os.path.abspath(path)
    for root in roots:
        root_path = os.path.abspath(root['path'])
        compare_root, compare_path = root_path, absolute
        if root['case_sensitive'] == 0:
            compare_root, compare_path = compare_root.casefold(), compare_path.casefold()
        try:
            if os.path.commonpath((compare_root, compare_path)) == compare_root and compare_path != compare_root:
                candidates.append(root)
        except ValueError:
            continue
    if not candidates:
        raise IdentityConflictError('No registered root owns the path')
    candidates.sort(key=lambda item: (-len(os.path.abspath(item['path'])), item['id']))
    if len(candidates) > 1 and len(os.path.abspath(candidates[0]['path'])) == len(os.path.abspath(candidates[1]['path'])):
        raise IdentityConflictError('Equivalent roots require explicit ownership')
    return int(candidates[0]['id'])


def _stat_signature(info: os.stat_result) -> tuple[int, int, int | None, int | None, int | None]:
    return (int(info.st_size), int(info.st_mtime_ns),
            getattr(info, "st_dev", None), getattr(info, "st_ino", None),
            getattr(info, "st_ctime_ns", None))


class CatalogRepository:
    """Transactional identity repository bound to an existing SQLite file."""

    def __init__(self, database: str | os.PathLike[str]):
        self.database = Path(database).absolute()

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if self.database.is_symlink():
            raise CatalogUnavailableError("Catalog database path must not be a symlink")
        try:
            real = self.database.resolve(strict=True)
            if not real.is_file():
                raise CatalogUnavailableError("Catalog database does not exist")
            uri = real.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, CatalogError):
                raise
            raise CatalogUnavailableError(f"Cannot open existing catalog: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if foreign_key_violation(connection) is not None:
                raise CatalogSchemaError("Catalog has foreign key violations")
            if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='schema_migrations'").fetchone():
                raise CatalogSchemaError("Catalog is not initialized")
            if not connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise CatalogSchemaError("Catalog has no applied schema migration")
            # This validates the recorded checksum/version. With an initialized current
            # schema it is read-only; no empty database can be silently initialized here.
            initialize_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _scope(connection: sqlite3.Connection, library_uuid: str, root_uuid: str):
        library_uuid = _uuid(library_uuid, "library")
        root_uuid = _uuid(root_uuid, "root")
        library = connection.execute(
            "SELECT id,uuid FROM library WHERE uuid=?", (library_uuid,)
        ).fetchone()
        if library is None:
            raise IdentityNotFoundError("Unknown library UUID")
        root = connection.execute(
            "SELECT id,uuid,library_id,path FROM library_root WHERE uuid=?", (root_uuid,)
        ).fetchone()
        if root is None:
            raise IdentityNotFoundError("Unknown root UUID")
        if root["library_id"] != library["id"]:
            raise ScopeMismatchError("Root does not belong to the selected library")
        return library, root

    @staticmethod
    def _asset_record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        revision = None
        if row["current_revision_id"] is not None:
            revision = connection.execute(
                "SELECT uuid,content_hash,verification_state,size_bytes,mtime_ns "
                "FROM media_revision WHERE id=? AND library_id=? AND asset_id=?",
                (row["current_revision_id"], row["library_id"], row["id"]),
            ).fetchone()
            if revision is None:
                raise CatalogSchemaError("Asset points to a missing or foreign revision")
        return {
            "mediaId": row["uuid"],
            "libraryId": row["library_uuid"],
            "rootId": row["root_uuid"],
            "path": row["path"],
            "availability": row["availability"],
            "revisionId": revision["uuid"] if revision else None,
            "contentVersion": revision_token(revision["uuid"]) if revision else None,
            "contentHash": revision["content_hash"] if revision else None,
            "verificationState": revision["verification_state"] if revision else None,
            "size": revision["size_bytes"] if revision else None,
            "mtimeNs": revision["mtime_ns"] if revision else None,
        }

    @staticmethod
    def _asset_by_uuid(connection: sqlite3.Connection, library, root,
                       media_uuid: str) -> sqlite3.Row:
        media_uuid = _uuid(media_uuid, "media")
        row = connection.execute(
            "SELECT a.*,l.uuid AS library_uuid,r.uuid AS root_uuid "
            "FROM media_asset a JOIN library l ON l.id=a.library_id "
            "JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id "
            "WHERE a.uuid=?", (media_uuid,),
        ).fetchone()
        if row is None:
            raise IdentityNotFoundError("Unknown media UUID")
        if row["library_id"] != library["id"] or row["root_id"] != root["id"]:
            raise ScopeMismatchError("Media asset does not belong to the selected library/root")
        return row

    def resolve_asset(self, library_uuid: str, root_uuid: str, *,
                      media_uuid: str | None = None,
                      path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Resolve exact path and/or media UUID within the required scope.

        Path comparison is binary/exact. No case folding, realpath matching, basename
        matching, or content-based identity inference is performed.
        """
        if media_uuid is None and path is None:
            raise ValueError("media_uuid or path is required")
        exact_path = os.fspath(path) if path is not None else None
        if exact_path is not None and not isinstance(exact_path, str):
            raise ValueError("path must be text")
        with self._transaction(write=False) as connection:
            library, root = self._scope(connection, library_uuid, root_uuid)
            by_id = (self._asset_by_uuid(connection, library, root, media_uuid)
                     if media_uuid is not None else None)
            by_path = None
            if exact_path is not None:
                rows = connection.execute(
                    "SELECT a.*,l.uuid AS library_uuid,r.uuid AS root_uuid "
                    "FROM media_asset a JOIN library l ON l.id=a.library_id "
                    "JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id "
                    "WHERE a.library_id=? AND a.root_id=? AND a.path=? AND " +
                    (visible_clause(connection) if media_uuid is None else '1=1') + " ORDER BY a.id",
                    (library["id"], root["id"], exact_path),
                ).fetchall()
                if len(rows) > 1:
                    raise IdentityConflictError("Exact path resolves to multiple assets")
                if rows:
                    by_path = rows[0]
                else:
                    # A live path may have been claimed by another root/asset. Do not
                    # silently make the supplied path appear to identify the UUID asset.
                    owner = connection.execute(
                        "SELECT uuid,root_id FROM media_asset WHERE library_id=? "
                        "AND live_path_key=?", (library["id"], exact_path),
                    ).fetchone()
                    if owner is not None:
                        raise IdentityConflictError("Path is bound to a different media asset")
                    if by_id is not None:
                        raise IdentityConflictError("media_uuid and path do not identify the same asset")
                    raise IdentityNotFoundError("Unknown exact media path")
            if by_id is not None and by_path is not None and by_id["id"] != by_path["id"]:
                raise IdentityConflictError("media_uuid and path identify different assets")
            row = by_id or by_path
            return self._asset_record(connection, row)

    @staticmethod
    def _observe_file_in_transaction(connection: sqlite3.Connection, library, root,
                                     exact_path: str, file_stat: Any, *,
                                     scan_epoch=None, include_record=True):
        """Write one stat observation into a caller-owned transaction."""
        size, mtime_ns = _stat_values(file_stat)
        if not _path_within(root["path"], exact_path):
            raise ScopeMismatchError("File path is outside the selected root")
        rows = connection.execute(
                "SELECT a.*,l.uuid AS library_uuid,r.uuid AS root_uuid "
                "FROM media_asset a JOIN library l ON l.id=a.library_id "
                "JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id "
                "WHERE a.library_id=? AND a.root_id=? AND a.path=? AND " + visible_clause(connection) + " ORDER BY a.id",
                (library["id"], root["id"], exact_path),
        ).fetchall()
        if len(rows) > 1:
            raise IdentityConflictError("Exact path resolves to multiple assets")
        asset = rows[0] if rows else None
        claimed = connection.execute(
                "SELECT uuid FROM media_asset WHERE library_id=? AND live_path_key=? "
                "AND (? IS NULL OR id<>?)",
                (library["id"], exact_path, asset["id"] if asset else None,
                 asset["id"] if asset else None),
        ).fetchone()
        if claimed is not None:
            raise IdentityConflictError("Live path is already bound to another media asset")

        if asset is None:
            media_uuid = new_identity()
            connection.execute(
                    "INSERT INTO media_asset(uuid,library_id,root_id,path,live_path_key,availability) "
                    "VALUES (?,?,?,?,?,'present')",
                    (media_uuid, library["id"], root["id"], exact_path, exact_path),
            )
            asset = connection.execute(
                    "SELECT a.*,l.uuid AS library_uuid,r.uuid AS root_uuid "
                    "FROM media_asset a JOIN library l ON l.id=a.library_id "
                    "JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id "
                    "WHERE a.uuid=?", (media_uuid,),
            ).fetchone()
            current = None
        else:
            current = None
            if asset["current_revision_id"] is not None:
                current = connection.execute(
                        "SELECT id,size_bytes,mtime_ns FROM media_revision "
                        "WHERE id=? AND library_id=? AND asset_id=?",
                        (asset["current_revision_id"], library["id"], asset["id"]),
                ).fetchone()
                if current is None:
                    raise CatalogSchemaError("Asset current revision is missing")
            if asset["live_path_key"] != exact_path or asset["availability"] != "present":
                connection.execute(
                        "UPDATE media_asset SET live_path_key=?,availability='present' WHERE id=?",
                        (exact_path, asset["id"]),
                )
        if current is None or (current["size_bytes"], current["mtime_ns"]) != (size, mtime_ns):
            revision_uuid = new_identity()
            connection.execute(
                    "INSERT INTO media_revision(uuid,library_id,asset_id,size_bytes,mtime_ns,"
                    "verification_state) VALUES (?,?,?,?,?,'unchecked')",
                    (revision_uuid, library["id"], asset["id"], size, mtime_ns),
            )
            revision_id = connection.execute(
                    "SELECT id FROM media_revision WHERE uuid=?", (revision_uuid,)
            ).fetchone()["id"]
            connection.execute(
                    "UPDATE media_asset SET current_revision_id=? WHERE id=?",
                    (revision_id, asset["id"]),
            )
        if scan_epoch is not None:
            connection.execute('INSERT OR IGNORE INTO catalog_scan_seen(root_id,epoch,asset_id) VALUES(?,?,?)',
                               (root['id'], scan_epoch, asset['id']))
        if not include_record:
            return None
        return CatalogRepository._asset_record(connection, connection.execute(
                "SELECT a.*,l.uuid AS library_uuid,r.uuid AS root_uuid "
                "FROM media_asset a JOIN library l ON l.id=a.library_id "
                "JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id "
                "WHERE a.id=?", (asset["id"],),
        ).fetchone())

    def observe_file(self, library_uuid: str, root_uuid: str,
                     path: str | os.PathLike[str], file_stat: Any, *, scan_epoch=None,
                     expected_roots=None) -> dict[str, Any]:
        """Create a stable asset and observed revision, without hashing file bytes."""
        exact_path = os.fspath(path)
        if not isinstance(exact_path, str) or not exact_path:
            raise ValueError("path must be a non-empty string")
        with self._transaction(write=True) as connection:
            library, root = self._scope(connection, library_uuid, root_uuid)
            if scan_epoch is not None:
                from .scans import require_epoch, require_roots
                require_epoch(connection, root['id'], scan_epoch)
                require_roots(connection, library['id'], expected_roots)
            return self._observe_file_in_transaction(
                connection, library, root, exact_path, file_stat, scan_epoch=scan_epoch)

    def observe_files(self, library_uuid: str, root_uuid: str, files) -> list[dict[str, Any]]:
        """Observe a bounded set of files in one validated SQLite transaction."""
        entries = [(os.fspath(path), file_stat) for path, file_stat in files]
        if not entries:
            return []
        if any(not path for path, _ in entries):
            raise ValueError("path must be a non-empty string")
        with self._transaction(write=True) as connection:
            library, root = self._scope(connection, library_uuid, root_uuid)
            return [self._observe_file_in_transaction(connection, library, root, path, file_stat)
                    for path, file_stat in entries]

    def observe_scanned_files(self, library_uuid: str, root_uuid: str, epoch: str,
                              files, *, expected_roots=None, cancelled=lambda: False) -> dict:
        """Observe one bounded scan batch, resolving ownership in the same write transaction."""
        entries = [(os.fspath(path), file_stat) for path, file_stat in files]
        if not entries:
            return {"observed": 0, "ownedElsewhere": 0}
        if any(not path for path, _ in entries):
            raise ValueError("path must be a non-empty string")
        from .scans import require_epoch, require_roots
        observed = owned_elsewhere = 0
        with self._transaction(write=True) as connection:
            library, root = self._scope(connection, library_uuid, root_uuid)
            require_epoch(connection, root['id'], epoch)
            require_roots(connection, library['id'], expected_roots)
            registered = connection.execute(
                'SELECT id,path,case_sensitive FROM library_root WHERE library_id=? ORDER BY id',
                (library['id'],)).fetchall()
            for path, file_stat in entries:
                if cancelled():
                    from .scans import ScanCancelled
                    raise ScanCancelled('Scan cancelled before committing the current batch')
                owner_id = _path_owned_root(connection, library['id'], registered, path)
                if owner_id != root['id']:
                    owned_elsewhere += 1
                    continue
                self._observe_file_in_transaction(connection, library, root, path, file_stat,
                                                  scan_epoch=epoch, include_record=False)
                observed += 1
        return {"observed": observed, "ownedElsewhere": owned_elsewhere}

    def strict_verify(self, library_uuid: str, root_uuid: str,
                      path: str | os.PathLike[str], *,
                      media_uuid: str | None = None) -> dict[str, Any]:
        """Hash on demand; publish only if pre/post file stat signatures match."""
        exact_path = os.fspath(path)
        if media_uuid is not None:
            self.resolve_asset(library_uuid, root_uuid, media_uuid=media_uuid, path=exact_path)
        with self._transaction(write=False) as connection:
            _, root = self._scope(connection, library_uuid, root_uuid)
            if not _path_within(root["path"], exact_path):
                raise ScopeMismatchError("File path is outside the selected root")
        try:
            before = Path(exact_path).stat()
        except OSError as exc:
            raise FileChangedDuringVerification(f"Cannot stat file before hashing: {exc}") from exc
        observed = self.observe_file(library_uuid, root_uuid, exact_path, before)
        if media_uuid is not None and _uuid(media_uuid, "media") != observed["mediaId"]:
            raise IdentityConflictError("media_uuid and exact path identify different assets")
        try:
            digest = hashlib.sha256()
            with open(exact_path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = Path(exact_path).stat()
        except OSError as exc:
            raise FileChangedDuringVerification(f"Cannot read file during hashing: {exc}") from exc
        if _stat_signature(before) != _stat_signature(after):
            raise FileChangedDuringVerification("File stat changed while hashing")
        content_hash = digest.hexdigest()

        with self._transaction(write=True) as connection:
            library, root = self._scope(connection, library_uuid, root_uuid)
            asset = self._asset_by_uuid(connection, library, root, observed["mediaId"])
            if asset["path"] != exact_path:
                raise IdentityConflictError("Asset path changed during verification")
            current = connection.execute(
                "SELECT id,uuid,content_hash,verification_state,size_bytes,mtime_ns "
                "FROM media_revision WHERE id=? AND library_id=? AND asset_id=?",
                (asset["current_revision_id"], library["id"], asset["id"]),
            ).fetchone()
            if current is None or current["uuid"] != observed["revisionId"]:
                raise IdentityConflictError("Current revision changed during verification")
            size, mtime_ns = _stat_values(after)
            if (current["size_bytes"], current["mtime_ns"]) != (size, mtime_ns):
                raise FileChangedDuringVerification("Observed stat no longer matches current revision")
            if current["verification_state"] == "verified" and current["content_hash"] != content_hash:
                revision_uuid = new_identity()
                connection.execute(
                    "INSERT INTO media_revision(uuid,library_id,asset_id,content_hash,"
                    "verification_state,size_bytes,mtime_ns) VALUES (?,?,?,?,'verified',?,?)",
                    (revision_uuid, library["id"], asset["id"], content_hash, size, mtime_ns),
                )
                revision_id = connection.execute(
                    "SELECT id FROM media_revision WHERE uuid=?", (revision_uuid,)
                ).fetchone()["id"]
                connection.execute("UPDATE media_asset SET current_revision_id=? WHERE id=?",
                                   (revision_id, asset["id"]))
            elif current["verification_state"] != "verified" or current["content_hash"] != content_hash:
                connection.execute(
                    "UPDATE media_revision SET content_hash=?,verification_state='verified' "
                    "WHERE id=?", (content_hash, current["id"]),
                )
            latest = connection.execute(
                "SELECT a.*,l.uuid AS library_uuid,r.uuid AS root_uuid "
                "FROM media_asset a JOIN library l ON l.id=a.library_id "
                "JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id "
                "WHERE a.id=?", (asset["id"],),
            ).fetchone()
            return self._asset_record(connection, latest)


__all__ = [
    "CatalogError", "CatalogUnavailableError", "CatalogSchemaError",
    "IdentityNotFoundError", "ScopeMismatchError", "IdentityConflictError",
    "FileChangedDuringVerification", "CatalogRepository",
]
