"""Idempotent import of legacy application-trash entries from a verified snapshot.

This operates only on an isolated, already-initialized shadow catalog and trash
store. The catalog, trash index, and copied payload cannot be committed as one
SQLite transaction; stable migration mappings make interrupted work resumable.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import time
import uuid
from typing import Any

from .backup import SnapshotError, verify_snapshot
from .import_legacy import _manifest_identity, _root_contains
from ..storage.organization import DEFAULT_CATEGORY, ProjectionConflictError, _category_name
from ..storage.database import initialize_schema
from ..operations.trash import _CHECKSUM as _TRASH_CHECKSUM, _VERSION as _TRASH_VERSION


class TrashImportError(RuntimeError):
    pass


class TrashImportConflict(TrashImportError):
    pass


_EXTENSION_VERSION = 1
_EXTENSION_SQL = """
CREATE TABLE legacy_trash_extension(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
CREATE TABLE legacy_trash_import_state(source_hash TEXT PRIMARY KEY, created_at INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE legacy_trash_snapshot(
 source_hash TEXT NOT NULL,
 source_key TEXT NOT NULL,
 status TEXT NOT NULL,
 raw_entry_json TEXT NOT NULL,
 favorite_raw_json TEXT,
 tags_raw_json TEXT,
 mark_raw_json TEXT,
 loss_json TEXT NOT NULL,
 detail TEXT,
 PRIMARY KEY(source_hash,source_key)
);
CREATE TABLE legacy_trash_stage(
 source_hash TEXT NOT NULL,
 source_key TEXT NOT NULL,
 source_path TEXT NOT NULL,
 destination_path TEXT NOT NULL,
 staging_path TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 size INTEGER NOT NULL,
 state TEXT NOT NULL,
 PRIMARY KEY(source_hash,source_key)
);
"""
_EXTENSION_CHECKSUM = hashlib.sha256(
    f"{_EXTENSION_VERSION}\n{_EXTENSION_SQL}".encode("utf-8")
).hexdigest()
_SUPPORTED_MARKS = {"review", "publish", "compare", "delete"}
_UUID_NAMESPACE = uuid.UUID("85563dda-7f0e-4f98-9cc0-c65d5316ab40")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_file_record(path: Path) -> dict[str, Any]:
    before = path.stat()
    digest = _sha256(path)
    after = path.stat()
    left = (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
    right = (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
    if left != right:
        raise TrashImportError(f"Snapshot payload changed while reading: {path.name}")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns,
            "device": after.st_dev, "inode": after.st_ino, "sha256": digest}


def _read_json_unique(path: Path):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise TrashImportError(f"Duplicate JSON object key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, ValueError, UnicodeError) as exc:
        raise TrashImportError(f"Cannot parse frozen trash index: {exc}") from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _uuid_for(source_hash: str, source_key: str, kind: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{source_hash}\0{source_key}\0{kind}"))


def _safe_key(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 180 and all(
        char.isalnum() or char in "-_" for char in value
    )


def _load_catalog(database: Path):
    if database.is_symlink() or not database.is_file():
        raise TrashImportError("Target catalog must be an existing initialized database")
    uri = database.resolve(strict=True).as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        connection.close()
        raise TrashImportError("Target catalog schema is not initialized")
    try:
        initialize_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _load_trash_state(database: Path):
    if database.is_symlink() or not database.is_file():
        raise TrashImportError("Target trash-state database must already be initialized")
    uri = database.resolve(strict=True).as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        row = connection.execute("SELECT version,checksum FROM trash_schema").fetchone()
        if row is None or row["version"] != _TRASH_VERSION or row["checksum"] != _TRASH_CHECKSUM:
            raise TrashImportError("Target trash-state schema version/checksum mismatch")
        extension_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_trash_extension'"
        ).fetchone()
        extension = connection.execute(
            "SELECT version,checksum FROM legacy_trash_extension"
        ).fetchone() if extension_table else None
        if extension and (extension["version"] != _EXTENSION_VERSION or
                          extension["checksum"] != _EXTENSION_CHECKSUM):
            raise TrashImportError("Legacy trash extension version/checksum mismatch")
        if extension is None:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + _EXTENSION_SQL +
                f"\nINSERT INTO legacy_trash_extension(version,checksum) VALUES ({_EXTENSION_VERSION},'{_EXTENSION_CHECKSUM}');\nCOMMIT;"
            )
    except Exception:
        connection.close()
        raise
    return connection


def _ensure_job(connection, source_hash: str):
    rows = connection.execute(
        "SELECT id,source_hash FROM migration_job WHERE source_kind='legacy-trash'"
    ).fetchall()
    if any(row["source_hash"] != source_hash for row in rows):
        raise TrashImportConflict("Target shadow already contains a different legacy-trash manifest")
    row = connection.execute(
        "SELECT id FROM migration_job WHERE source_kind='legacy-trash' AND source_hash=?",
        (source_hash,)).fetchone()
    if row:
        return row["id"]
    cursor = connection.execute(
        "INSERT INTO migration_job(uuid,source_kind,source_hash,state) VALUES(?,?,?,'importing')",
        (str(uuid.uuid4()), "legacy-trash", source_hash))
    return cursor.lastrowid


def _mapping(connection, source_hash: str, source_key: str):
    return connection.execute(
        "SELECT target_kind,target_uuid,outcome FROM migration_mapping "
        "WHERE source_kind='legacy-trash' AND source_hash=? AND source_key=?",
        (source_hash, source_key)).fetchone()


def _put_mapping(connection, job_id: int, source_hash: str, source_key: str,
                 target_kind: str, target_uuid: str, outcome="mapped"):
    existing = _mapping(connection, source_hash, source_key)
    if existing:
        if (existing["target_kind"], existing["target_uuid"], existing["outcome"]) != (
                target_kind, target_uuid, outcome):
            raise TrashImportConflict(f"Migration mapping changed for {source_key}")
        return False
    connection.execute(
        "INSERT INTO migration_mapping(job_id,source_kind,source_hash,source_key,target_kind,target_uuid,outcome) "
        "VALUES(?,'legacy-trash',?,?,?,?,?)",
        (job_id, source_hash, source_key, target_kind, target_uuid, outcome))
    return True


def _raw_snapshot(trash_db, source_hash, source_key, status, entry, losses, detail=None):
    trash_db.execute(
        "INSERT INTO legacy_trash_snapshot(source_hash,source_key,status,raw_entry_json,"
        "favorite_raw_json,tags_raw_json,mark_raw_json,loss_json,detail) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source_hash,source_key) DO UPDATE SET status=excluded.status,"
        "raw_entry_json=excluded.raw_entry_json,favorite_raw_json=excluded.favorite_raw_json,"
        "tags_raw_json=excluded.tags_raw_json,mark_raw_json=excluded.mark_raw_json,"
        "loss_json=excluded.loss_json,detail=excluded.detail",
        (source_hash, source_key, status, _json(entry),
         _json(entry.get("fav")) if "fav" in entry else None,
         _json(entry.get("tags")) if "tags" in entry else None,
         _json(entry.get("mark")) if "mark" in entry else None,
         _json(losses), detail))


def _payload_source(snapshot: Path, manifest: dict[str, Any], key: str) -> Path | None:
    matches = []
    for item in manifest["files"]:
        rel = PurePosixPath(item["path"])
        if rel.parent.as_posix() == "trash" and (rel.name == key or rel.name.startswith(key + ".")):
            matches.append(snapshot / "files" / Path(*rel.parts))
    return matches[0] if len(matches) == 1 else None


def _copy_payload(source: Path, destination: Path, evidence: dict[str, Any], *,
                  owned_staging: bool = False) -> None:
    before = _stable_file_record(source)
    if evidence.get("sha256") != before["sha256"] or evidence.get("size") != before["size"]:
        raise TrashImportError("Frozen payload no longer matches the verified manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".legacy-import.stage")
    if destination.exists():
        target = _stable_file_record(destination)
        if target["sha256"] != before["sha256"] or target["size"] != before["size"]:
            raise TrashImportConflict(f"Target trash payload exists with different bytes: {destination.name}")
        if staging.exists():
            src_st, dst_st = staging.stat(), destination.stat()
            if (src_st.st_dev, src_st.st_ino) == (dst_st.st_dev, dst_st.st_ino):
                staging.unlink()
            elif _stable_file_record(staging)["sha256"] == before["sha256"]:
                staging.unlink()
            elif owned_staging:
                staging.unlink()
            else:
                raise TrashImportConflict("Conflicting stale import staging file")
    elif staging.exists():
        current = _stable_file_record(staging)
        if current["sha256"] != before["sha256"] or current["size"] != before["size"]:
            if not owned_staging:
                raise TrashImportConflict("Staging payload differs; it was preserved")
            staging.unlink()
            with source.open("rb") as src, staging.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            shutil.copystat(source, staging, follow_symlinks=False)
            fd = os.open(staging, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            current = _stable_file_record(staging)
            if current["sha256"] != before["sha256"] or current["size"] != before["size"]:
                raise TrashImportError("Rebuilt staged payload failed verification")
    else:
        try:
            with source.open("rb") as src, staging.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            shutil.copystat(source, staging, follow_symlinks=False)
            fd = os.open(staging, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            # Leave a partial staging file as evidence; retry refuses to replace it.
            raise
        staged = _stable_file_record(staging)
        after = _stable_file_record(source)
        if (staged["sha256"], staged["size"]) != (before["sha256"], before["size"]):
            raise TrashImportError("Copied trash payload failed full hash/size verification")
        if after != before:
            raise TrashImportError("Frozen payload source changed during copy")
    if not destination.exists():
        try:
            os.link(staging, destination, follow_symlinks=False)
        except FileExistsError:
            raise TrashImportConflict("Target trash payload appeared during publish")
        staging.unlink()
    final = _stable_file_record(destination)
    source_after = _stable_file_record(source)
    if (final["sha256"], final["size"]) != (before["sha256"], before["size"]):
        raise TrashImportError("Published trash payload failed full hash verification")
    if source_after != before:
        raise TrashImportError("Frozen payload source changed during import")


def _ensure_payload_stage(trash_db, source_hash: str, source_key: str,
                          source: Path, destination: Path, evidence: dict[str, Any]) -> None:
    staging = destination.with_name(destination.name + ".legacy-import.stage")
    row = trash_db.execute(
        "SELECT * FROM legacy_trash_stage WHERE source_hash=? AND source_key=?",
        (source_hash, source_key)).fetchone()
    expected = (str(source), str(destination), str(staging), evidence["sha256"], evidence["size"])
    if row:
        actual = tuple(row[name] for name in
                       ("source_path", "destination_path", "staging_path", "sha256", "size"))
        if actual != expected:
            raise TrashImportConflict(f"Staging identity changed for {source_key}")
    else:
        trash_db.execute(
            "INSERT INTO legacy_trash_stage(source_hash,source_key,source_path,destination_path,staging_path,sha256,size,state) "
            "VALUES(?,?,?,?,?,?,?,'copying')", (source_hash, source_key, *expected))
    trash_db.commit()
    _copy_payload(source, destination, evidence, owned_staging=row is not None)
    trash_db.execute("UPDATE legacy_trash_stage SET state='published' WHERE source_hash=? AND source_key=?",
                     (source_hash, source_key))
    trash_db.commit()


def _insert_historical_asset(catalog, job_id, source_hash, source_key, entry,
                             media_uuid, revision_uuid, payload_hash, payload_size,
                             library_uuid, profile_id, root):
    mapped_asset = _mapping(catalog, source_hash, source_key + "#asset")
    mapped_revision = _mapping(catalog, source_hash, source_key + "#revision")
    if mapped_asset:
        media_uuid = mapped_asset["target_uuid"]
        revision_uuid = mapped_revision["target_uuid"] if mapped_revision else revision_uuid
        asset = catalog.execute("SELECT id,library_id,root_id,current_revision_id,path,availability FROM media_asset WHERE uuid=?",
                                (media_uuid,)).fetchone()
        revision = catalog.execute("SELECT uuid FROM media_revision WHERE uuid=? AND asset_id=?",
                                   (revision_uuid, asset["id"] if asset else -1)).fetchone()
        if asset is None or revision is None or asset["availability"] not in ("missing", "present"):
            raise TrashImportConflict(f"Mapped historical identity is incomplete for {source_key}")
        return media_uuid, revision_uuid, False

    connection_root = catalog.execute(
        "SELECT id FROM library_root WHERE uuid=? AND library_id=(SELECT id FROM library WHERE uuid=?)",
        (root["uuid"], library_uuid)).fetchone()
    library = catalog.execute("SELECT id FROM library WHERE uuid=?", (library_uuid,)).fetchone()
    if library is None or connection_root is None:
        raise TrashImportError("Selected library/root ownership does not exist")
    orig = entry["orig"]
    now = str(int(entry["t"]))
    catalog.execute(
        "INSERT INTO media_asset(uuid,library_id,root_id,path,live_path_key,availability) VALUES(?,?,?,?,NULL,'missing')",
        (media_uuid, library["id"], connection_root["id"], orig))
    asset_id = catalog.execute("SELECT id FROM media_asset WHERE uuid=?", (media_uuid,)).fetchone()["id"]
    catalog.execute(
        "INSERT INTO media_revision(uuid,library_id,asset_id,content_hash,verification_state,size_bytes,mtime_ns) "
        "VALUES(?,?,?,?,'verified',?,NULL)",
        (revision_uuid, library["id"], asset_id, payload_hash, payload_size))
    revision_id = catalog.execute("SELECT id FROM media_revision WHERE uuid=?", (revision_uuid,)).fetchone()["id"]
    catalog.execute("UPDATE media_asset SET current_revision_id=? WHERE id=?", (revision_id, asset_id))

    if profile_id is not None:
        favorite = bool(entry.get("fav"))
        category = entry.get("fav") if isinstance(entry.get("fav"), str) and entry.get("fav") else DEFAULT_CATEGORY
        if favorite:
            category = _category_name(category)
            collection = catalog.execute(
                "SELECT id FROM collection WHERE profile_id=? AND name=?", (profile_id, category)).fetchone()
            if collection is None:
                catalog.execute(
                    "INSERT INTO collection(uuid,library_id,profile_id,name) VALUES(?,?,?,?)",
                    (str(uuid.uuid5(_UUID_NAMESPACE, f"category\0{profile_id}\0{category}")),
                     library["id"], profile_id, category))
        mark = entry.get("mark")
        if mark is not None and mark not in _SUPPORTED_MARKS:
            raise TrashImportError(f"Unsupported legacy mark value: {mark!r}")
        catalog.execute(
            "INSERT INTO user_media_state(profile_id,library_id,asset_id,favorite,favorite_category,"
            "favorite_time,favorite_seq,favorite_intent,mark_state,mark_updated) VALUES(?,?,?,?,?,NULL,0,?,?,NULL)",
            (profile_id, library["id"], asset_id, int(favorite), category if favorite else None,
             int(favorite), mark))
        if favorite:
            # Membership creation time records this import, not the unknown
            # historical favorite time retained as NULL in user_media_state.
            collection_id = catalog.execute(
                "SELECT id FROM collection WHERE profile_id=? AND name=?",
                (profile_id, category)).fetchone()["id"]
            catalog.execute(
                "INSERT INTO collection_item(collection_id,library_id,asset_id) VALUES(?,?,?)",
                (collection_id, library["id"], asset_id))
        tags = entry.get("tags") or []
        for raw_tag in tags:
            if not isinstance(raw_tag, str) or not raw_tag.strip():
                raise TrashImportError(f"Unsupported legacy tag value: {raw_tag!r}")
            tag_name = raw_tag.strip()
            tag = catalog.execute("SELECT id FROM tag WHERE profile_id=? AND name=?",
                                  (profile_id, tag_name)).fetchone()
            if tag is None:
                tag_uuid = str(uuid.uuid5(_UUID_NAMESPACE, f"tag\0{profile_id}\0{tag_name}"))
                catalog.execute("INSERT INTO tag(uuid,library_id,profile_id,name) VALUES(?,?,?,?)",
                                (tag_uuid, library["id"], profile_id, tag_name))
                tag = catalog.execute("SELECT id FROM tag WHERE uuid=?", (tag_uuid,)).fetchone()
            catalog.execute("INSERT OR IGNORE INTO tag_assignment(tag_id,library_id,asset_id) VALUES(?,?,?)",
                            (tag["id"], library["id"], asset_id))

    _put_mapping(catalog, job_id, source_hash, source_key + "#asset", "media_asset", media_uuid)
    _put_mapping(catalog, job_id, source_hash, source_key + "#revision", "media_revision", revision_uuid)
    return media_uuid, revision_uuid, True


def import_trash(snapshot, database, trash_state_database, trash_dir, *,
                 library_uuid: str, profile_uuid: str | None = None) -> dict[str, Any]:
    """Import frozen legacy trash into an initialized shadow catalog/trash pair.

    ``database`` and ``trash_state_database`` must already be initialized. The
    caller must provide a separate destination ``trash_dir`` and an explicit
    library UUID. No source/live gallery is scanned or modified.
    """
    snapshot_path = Path(snapshot).resolve(strict=True)
    database_path = Path(database).expanduser().absolute()
    trash_state_path = Path(trash_state_database).expanduser().absolute()
    destination_trash = Path(trash_dir).expanduser().absolute()
    manifest = verify_snapshot(snapshot_path)
    source_hash = _manifest_identity(manifest)
    index_entry = next((row for row in manifest["files"] if row["path"] == "trash/index.json"), None)
    report: dict[str, Any] = {
        "sourceHash": source_hash, "mapped": [], "duplicates": [], "quarantined": [],
        "blockers": [], "legacyLoss": [], "projectionConflicts": [],
        "readyToSwitch": True, "imported": 0,
    }
    if index_entry is None:
        # server.load_trash() treats a missing index as an empty mapping.
        report["legacyIndexMissing"] = True
        index = {}
    else:
        index_path = snapshot_path / "files" / "trash" / "index.json"
        try:
            index = _read_json_unique(index_path)
        except TrashImportError as exc:
            report["readyToSwitch"] = False
            report["blockers"].append({"code": "trash_index_invalid", "detail": str(exc)})
            return report
    if not isinstance(index, dict):
        report["readyToSwitch"] = False
        report["blockers"].append({"code": "trash_index_wrong_shape"})
        return report

    library_uuid = str(uuid.UUID(str(library_uuid)))
    catalog = _load_catalog(database_path)
    try:
        trash_db = _load_trash_state(trash_state_path)
    except Exception:
        catalog.close()
        raise
    try:
        catalog.execute("BEGIN IMMEDIATE")
        library = catalog.execute("SELECT id FROM library WHERE uuid=?", (library_uuid,)).fetchone()
        if library is None:
            raise TrashImportError("Target library UUID does not exist")
        roots = catalog.execute(
            "SELECT id,uuid,path FROM library_root WHERE library_id=?", (library["id"],)
        ).fetchall()
        if not roots:
            raise TrashImportError("Target library has no registered roots for restoration")
        if profile_uuid is None:
            profiles = catalog.execute("SELECT id,uuid FROM local_profile WHERE library_id=? ORDER BY id",
                                       (library["id"],)).fetchall()
            if len(profiles) == 1:
                profile_uuid, profile_id = profiles[0]["uuid"], profiles[0]["id"]
            elif len(profiles) == 0:
                profile_id = None
            else:
                profile_id = None
        else:
            profile_uuid = str(uuid.UUID(str(profile_uuid)))
            profile = catalog.execute("SELECT id FROM local_profile WHERE uuid=? AND library_id=?",
                                      (profile_uuid, library["id"])).fetchone()
            if profile is None:
                raise TrashImportError("Requested profile does not belong to the target library")
            profile_id = profile["id"]
        job_id = _ensure_job(catalog, source_hash)
        catalog.commit()
    except Exception:
        catalog.rollback(); catalog.close(); trash_db.close()
        raise

    trash_db.execute("BEGIN IMMEDIATE")
    try:
        prior = trash_db.execute("SELECT source_hash FROM legacy_trash_import_state LIMIT 1").fetchone()
        if prior and prior["source_hash"] != source_hash:
            raise TrashImportConflict("Target trash store already contains a different snapshot")
        if prior is None:
            trash_db.execute("INSERT INTO legacy_trash_import_state(source_hash,created_at,state) VALUES(?,?,'importing')",
                             (source_hash, int(time.time())))
        trash_db.commit()
    except Exception:
        trash_db.rollback(); catalog.close(); trash_db.close()
        raise

    for source_key, raw in index.items():
        entry_key = f"trash-index:{source_key}"
        item_report = {"key": source_key, "status": "quarantined"}
        losses: list[str] = []
        detail = None
        try:
            if not _safe_key(source_key):
                raise TrashImportError("Unsafe or unsupported legacy trash key")
            if not isinstance(raw, dict):
                raise TrashImportError("Trash entry must be a JSON object")
            name, orig = raw.get("name"), raw.get("orig")
            timestamp, old_size = raw.get("t"), raw.get("size")
            if not isinstance(name, str) or not name or not isinstance(orig, str) or not orig:
                raise TrashImportError("Trash entry requires string name and orig")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                raise TrashImportError("Trash entry t must be an integer Unix timestamp")
            if isinstance(old_size, bool) or not isinstance(old_size, int) or old_size < 0:
                raise TrashImportError("Trash entry size must be a non-negative integer")
            payload_source = _payload_source(snapshot_path, manifest, source_key)
            if payload_source is None:
                raise TrashImportError("Payload is missing or ambiguous in frozen snapshot")
            source_record = next(row for row in manifest["files"] if
                                 (snapshot_path / "files" / Path(*PurePosixPath(row["path"]).parts)) == payload_source)
            source_evidence = {"sha256": source_record["sha256"], "size": source_record["bytes"]}
            if old_size != source_evidence["size"]:
                losses.append(f"legacy_size_mismatch:{old_size}!={source_evidence['size']}")
                report["blockers"].append({"code": "legacy_size_mismatch", "key": source_key,
                                           "recorded": old_size, "payload": source_evidence["size"]})
                report["readyToSwitch"] = False
            favorite = raw.get("fav")
            tags = raw.get("tags") or []
            mark = raw.get("mark")
            if favorite is not None and (not isinstance(favorite, str) or not favorite):
                raise TrashImportError("Unsupported favorite snapshot type")
            if not isinstance(tags, list):
                raise TrashImportError("Unsupported tags snapshot type")
            if mark is not None and (not isinstance(mark, str) or mark not in _SUPPORTED_MARKS):
                raise TrashImportError(f"Unsupported mark snapshot: {mark!r}")
            if favorite:
                losses.append("favorite_timestamp_missing_in_legacy_trash_entry")
                report["legacyLoss"].append({"key": source_key,
                                              "field": "favorite_time",
                                              "rawFavorite": favorite,
                                              "reason": "legacy trash index stores category but no favorite timestamp"})
            if mark:
                losses.append("mark_updated_missing_in_legacy_trash_entry")
                report["legacyLoss"].append({"key": source_key, "field": "mark_updated",
                                              "rawMark": mark,
                                              "reason": "legacy trash index stores mark but no update timestamp"})
            if profile_id is None and (favorite or tags or mark):
                raise TrashImportError("No unambiguous target local profile for organization snapshots")
            roots_for_path = [root for root in roots if _root_contains(root["path"], orig)]
            if not roots_for_path:
                raise TrashImportError("Original path is outside every registered target root")
            root = max(roots_for_path, key=lambda row: len(row["path"]))

            media_uuid = _uuid_for(source_hash, entry_key, "asset")
            revision_uuid = _uuid_for(source_hash, entry_key, "revision")
            trash_entry_uuid = _uuid_for(source_hash, entry_key, "trash-item")
            with catalog:
                catalog.execute("BEGIN IMMEDIATE")
                current_job = _ensure_job(catalog, source_hash)
                media_uuid, revision_uuid, is_new = _insert_historical_asset(
                    catalog, current_job, source_hash, entry_key, raw, media_uuid,
                    revision_uuid, source_evidence["sha256"], source_evidence["size"],
                    library_uuid, profile_id, root)
                _put_mapping(catalog, current_job, source_hash, entry_key + "#trash",
                             "legacy_trash_item", trash_entry_uuid)
                catalog.commit()

            suffix = PurePosixPath(source_record["path"]).suffix
            destination_payload = destination_trash / (source_key + suffix)
            operation_uuid = str(uuid.uuid5(_UUID_NAMESPACE, f"trash-op\0{source_hash}\0{source_key}"))
            with trash_db:
                trash_db.execute("BEGIN IMMEDIATE")
                existing = trash_db.execute("SELECT * FROM trash_item WHERE key=?", (source_key,)).fetchone()
                expected = (library_uuid, media_uuid, revision_uuid, name, orig,
                            str(destination_payload), old_size, timestamp, operation_uuid)
                if existing:
                    actual = tuple(existing[field] for field in (
                        "library_uuid", "media_uuid", "revision_uuid", "name", "orig_path",
                        "payload_path", "size", "trashed_at", "trash_operation_uuid"))
                    if actual != expected or existing["state"] not in (
                            "trashed", "pending_restore", "restored", "purging", "purged"):
                        raise TrashImportConflict(f"Existing target trash row differs for {source_key}")
                    duplicate = True
                    if existing["state"] == "trashed":
                        _ensure_payload_stage(trash_db, source_hash, entry_key, payload_source,
                                              destination_payload, source_evidence)
                else:
                    _ensure_payload_stage(trash_db, source_hash, entry_key, payload_source,
                                          destination_payload, source_evidence)
                    trash_db.execute(
                        "INSERT INTO trash_item(key,library_uuid,media_uuid,revision_uuid,name,orig_path,payload_path,size,trashed_at,state,trash_operation_uuid,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'trashed',?,?)",
                        (source_key, library_uuid, media_uuid, revision_uuid, name, orig,
                         str(destination_payload), old_size, timestamp, operation_uuid, int(time.time())))
                    duplicate = False
                _raw_snapshot(trash_db, source_hash, entry_key,
                              "duplicate" if duplicate else "mapped", raw, losses)
                trash_db.commit()

            # Re-import after restore/purge must not recreate a trashed copy or
            # overwrite the service's later state transition.
            if duplicate and existing["state"] in ("restored", "purged") and destination_payload.exists():
                report["readyToSwitch"] = False
                report["blockers"].append({"code": "inactive_trash_payload_reappeared",
                                           "key": source_key, "state": existing["state"]})

            item_report.update({"status": "duplicate" if duplicate else "mapped",
                                "target_state": existing["state"] if duplicate else "trashed",
                                "media_uuid": media_uuid, "revision_uuid": revision_uuid,
                                "target_key": source_key, "new_identity": is_new,
                                "payload_sha256": source_evidence["sha256"],
                                "losses": losses})
            report["mapped" if not duplicate else "duplicates"].append(item_report)
            report["imported"] += 0 if duplicate else 1
        except Exception as exc:
            detail = str(exc)
            item_report["detail"] = detail
            report["quarantined"].append(item_report)
            report["blockers"].append({"code": "trash_entry_quarantined", "key": source_key,
                                       "detail": detail})
            report["readyToSwitch"] = False
            # Raw malformed/unsupported material remains inspectable in target.
            try:
                quarantine_uuid = _uuid_for(source_hash, entry_key, "quarantine")
                with catalog:
                    catalog.execute("BEGIN IMMEDIATE")
                    current_job = _ensure_job(catalog, source_hash)
                    _put_mapping(catalog, current_job, source_hash, entry_key,
                                 "legacy_trash_quarantine", quarantine_uuid, "quarantined")
                    catalog.commit()
                with trash_db:
                    trash_db.execute("BEGIN IMMEDIATE")
                    _raw_snapshot(trash_db, source_hash, entry_key,
                                  "quarantined", raw if isinstance(raw, dict) else {"raw": raw},
                                  losses, detail)
                    trash_db.commit()
            except Exception as persist_exc:
                report["blockers"].append({"code": "quarantine_persistence_failed",
                                           "key": source_key, "detail": str(persist_exc)})

    try:
        verify_snapshot(snapshot_path)
    except SnapshotError as exc:
        report["readyToSwitch"] = False
        report["blockers"].append({"code": "snapshot_changed_during_import", "detail": str(exc)})
    if profile_uuid:
        try:
            from ..storage.organization import OrganizationRepository
            OrganizationRepository(database_path).read_projection(profile_uuid)
        except ProjectionConflictError as exc:
            report["readyToSwitch"] = False
            report["projectionConflicts"].append(str(exc))
            report["blockers"].append({"code": "legacy_projection_path_collision", "detail": str(exc)})
    final_state = "reconciled" if report["readyToSwitch"] else "failed"
    try:
        catalog.execute("UPDATE migration_job SET state=? WHERE source_kind='legacy-trash' AND source_hash=?",
                        (final_state, source_hash))
        catalog.commit()
        trash_db.execute("UPDATE legacy_trash_import_state SET state=? WHERE source_hash=?",
                         (final_state, source_hash))
        trash_db.commit()
    except sqlite3.Error as exc:
        report["readyToSwitch"] = False
        report["blockers"].append({"code": "migration_state_persist_failed", "detail": str(exc)})
    catalog.close()
    trash_db.close()
    return report
