"""Project verified catalog trash into the legacy JSON/payload layout.

This is a read-only projection from an immutable recovery bundle. It never
reads the original data directory and never activates legacy storage.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from .recovery_restore import verify_recovery_bundle


_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_new(path: Path, value) -> None:
    with path.open("xb") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def _safe_payload(preserved: Path, relative: str, entries: dict) -> Path:
    pure = Path(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise ValueError("Unsafe preserved payload path")
    normalized = pure.as_posix()
    if normalized not in entries or not normalized.startswith("trash/"):
        raise ValueError("Active trash payload is absent from preserved-file evidence")
    path = preserved.joinpath(*pure.parts)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Preserved trash payload is not a regular file")
    return path


def export_legacy_trash(bundle, destination, *, library_uuid=None, profile_uuid=None):
    """Export active ``trashed`` entries to a new ``destination/trash`` tree.

    Pending/conflict operations block the whole projection. Restored and
    purged rows remain in the recovery bundle and are listed as inactive history.
    """
    report, preserved, entries = verify_recovery_bundle(bundle)
    source = Path(report["sourceDataDir"])
    identity = report.get("storageIdentity") or {}
    library_uuid = library_uuid or identity.get("libraryUuid")
    profile_uuid = profile_uuid or identity.get("profileUuid")
    if not library_uuid:
        raise ValueError("Recovery bundle has no unambiguous library identity")
    database_rel = identity.get("database")
    if not database_rel or database_rel not in entries:
        raise ValueError("Recovery bundle lacks its authoritative catalog")
    database = preserved.joinpath(*Path(database_rel).parts)
    target = Path(destination).absolute()
    bundle_path = Path(bundle).absolute()
    if (target.exists() or target == bundle_path or target in bundle_path.parents or
            bundle_path in target.parents):
        raise ValueError("Legacy trash export requires a new independent directory")
    if target == source or target in source.parents or source in target.parents:
        raise ValueError("Legacy trash export must be separate from source data")
    if not target.parent.is_dir():
        raise ValueError("Export parent directory must exist")

    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        trash_rel = "trash-state.sqlite"
        if trash_rel not in entries:
            rows = []
        else:
            from ..operations.trash import CatalogTrash
            trash_db = preserved.joinpath(*Path(trash_rel).parts)
            check = sqlite3.connect(trash_db.as_uri() + "?mode=ro", uri=True)
            check.row_factory = sqlite3.Row
            try:
                CatalogTrash._check(check)
                rows = [dict(row) for row in check.execute(
                    "SELECT * FROM trash_item WHERE library_uuid=? ORDER BY trashed_at DESC,key",
                    (library_uuid,)).fetchall()]
            finally:
                check.close()

        blockers, historical, losses, candidates = [], [], [], []
        for row in rows:
            state, key = row["state"], row["key"]
            if state in ("pending_trash", "pending_restore", "purging", "conflict"):
                blockers.append({"key": key, "state": state,
                                 "reason": "unresolved operation cannot be represented by legacy trash"})
                continue
            if state in ("restored", "purged"):
                detail = {"key": key, "state": state,
                          "reason": "inactive history retained only in verified recovery bundle"}
                historical.append(detail)
                losses.append({**detail, "field": "state",
                               "reason": "legacy trash index contains active items only"})
                continue
            if state != "trashed":
                blockers.append({"key": key, "state": state, "reason": "unsupported trash state"})
                continue
            if not _KEY.fullmatch(key):
                blockers.append({"key": key, "state": state,
                                 "reason": "key violates legacy key-plus-suffix glob naming"})
                continue
            name = row["name"]
            suffix = Path(name).suffix
            if not suffix or not re.fullmatch(r"\.[A-Za-z0-9._+-]+", suffix):
                blockers.append({"key": key, "state": state,
                                 "reason": "legacy loader requires a key plus filename suffix"})
                continue
            try:
                original_path = Path(row["payload_path"])
                if not original_path.is_absolute():
                    raise ValueError("Catalog payload path is not absolute")
                relative = original_path.relative_to(source).as_posix()
                payload = _safe_payload(preserved, relative, entries)
            except (ValueError, OSError) as exc:
                blockers.append({"key": key, "state": state, "reason": str(exc)})
                continue
            if payload.stat().st_size != row["size"]:
                blockers.append({"key": key, "state": state, "reason": "payload byte size differs from catalog"})
                continue
            if key + suffix != Path(key + suffix).name:
                blockers.append({"key": key, "state": state, "reason": "unsafe legacy payload filename"})
                continue

            if not row["revision_uuid"]:
                blockers.append({"key": key, "state": state,
                                 "reason": "trash row has no revision UUID for strict identity matching"})
                continue
            asset = db.execute(
                "SELECT a.id,r.uuid,r.content_hash,r.size_bytes FROM media_asset a JOIN media_revision r "
                "ON r.id=a.current_revision_id AND r.asset_id=a.id "
                "JOIN library l ON l.id=a.library_id "
                "WHERE a.uuid=? AND r.uuid=? AND l.uuid=?",
                (row["media_uuid"], row["revision_uuid"], library_uuid)).fetchone()
            digest = _hash(payload)
            if (asset is None or asset["size_bytes"] != row["size"] or
                    (asset["content_hash"] is not None and asset["content_hash"] != digest)):
                blockers.append({"key": key, "state": state,
                                 "reason": "payload hash or size does not match catalog media identity",
                                 "catalogHash": asset["content_hash"] if asset else None})
                continue
            org = {}
            if profile_uuid:
                profile = db.execute("SELECT id FROM local_profile WHERE uuid=? AND library_id="
                                     "(SELECT id FROM library WHERE uuid=?)",
                                     (profile_uuid, library_uuid)).fetchone()
                if profile is None:
                    blockers.append({"key": key, "state": state,
                                     "reason": "organization profile is outside selected library"})
                    continue
                state_row = db.execute("SELECT favorite,favorite_category,favorite_time,mark_state,mark_updated "
                                       "FROM user_media_state WHERE profile_id=? AND asset_id=?",
                                       (profile["id"], asset["id"])).fetchone()
                if state_row:
                    if state_row["favorite"]:
                        org["fav"] = state_row["favorite_category"] or "默认"
                        if state_row["favorite_time"] is not None:
                            losses.append({"key": key, "field": "favorite_time",
                                           "value": state_row["favorite_time"],
                                           "reason": "legacy trash supports favorite category but not its timestamp"})
                    if state_row["mark_state"] is not None:
                        org["mark"] = state_row["mark_state"]
                        if state_row["mark_updated"] is not None:
                            losses.append({"key": key, "field": "mark_updated",
                                           "value": state_row["mark_updated"],
                                           "reason": "legacy trash supports mark but not its update timestamp"})
                org["tags"] = [item[0] for item in db.execute(
                    "SELECT t.name FROM tag_assignment ta JOIN tag t ON t.id=ta.tag_id "
                    "WHERE ta.asset_id=? AND t.profile_id=? ORDER BY t.name COLLATE BINARY",
                    (asset["id"], profile["id"]))]
            else:
                has_organization = db.execute(
                    "SELECT 1 FROM user_media_state WHERE asset_id=? AND "
                    "(favorite=1 OR mark_state IS NOT NULL) UNION ALL "
                    "SELECT 1 FROM tag_assignment WHERE asset_id=? LIMIT 1",
                    (asset["id"], asset["id"])).fetchone()
                if has_organization:
                    blockers.append({"key": key, "state": state,
                                     "reason": "organization snapshots exist but no target profile identity is available"})
                    continue
            losses.append({"key": key, "field": "catalog_identity",
                           "mediaUuid": row["media_uuid"], "revisionUuid": row["revision_uuid"],
                           "reason": "legacy trash index has no catalog identity fields"})
            candidates.append({"key": key, "name": name, "orig": row["orig_path"],
                               "t": row["trashed_at"], "size": row["size"],
                               "payload": payload, "payloadName": key + suffix, **org})
    finally:
        db.close()

    projection = {item["key"]: {field: item[field] for field in
                  ("name", "orig", "t", "size", "fav", "tags", "mark") if field in item}
                  for item in candidates}
    base_result = {"version": 1, "sourceBundle": str(bundle_path),
                   "sourceDataDir": str(source), "libraryUuid": library_uuid,
                   "profileUuid": profile_uuid, "activeEntries": len(projection),
                   "historical": historical, "losses": losses, "blockers": blockers,
                   "readyForLegacyStartup": False}
    if blockers:
        base_result["files"] = []
        return base_result

    target.mkdir()
    try:
        trash_dir = target / "trash"
        trash_dir.mkdir()
        for item in candidates:
            out = trash_dir / item["payloadName"]
            with item["payload"].open("rb") as reader, out.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            if out.stat().st_size != item["size"] or _hash(out) != _hash(item["payload"]):
                raise ValueError("Exported payload failed byte/hash verification: " + item["key"])
        _write_new(trash_dir / "index.json", projection)
        result = {**base_result, "blockers": [],
                  "note": "Trash projection only; other legacy storage and startup requirements are not assessed."}
        _write_new(target / "legacy-trash-projection.json", result)
        verify_recovery_bundle(bundle)
        result["files"] = [{"path": path.relative_to(target).as_posix(),
                            "bytes": path.stat().st_size, "sha256": _hash(path)}
                           for path in sorted((target / "trash").rglob("*")) if path.is_file()]
        return result
    except Exception:
        # Keep owned partial output for diagnosis. No successful component
        # result is returned, so the assembly cannot publish it as complete.
        raise


def export_trash(bundle, destination, *, library_uuid=None, profile_uuid=None):
    """Public migration-orchestrator name for the legacy trash projection."""
    return export_legacy_trash(bundle, destination, library_uuid=library_uuid,
                               profile_uuid=profile_uuid)
