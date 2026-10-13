"""Import verified legacy JSON into an isolated, non-authoritative shadow catalog."""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
from pathlib import Path
from typing import Any

from .backup import SnapshotError, verify_snapshot
from ..storage.database import connect, initialize_schema, new_identity


class SnapshotConflictError(RuntimeError):
    """A shadow catalog already belongs to a different frozen source manifest."""


class ShadowTargetError(RuntimeError):
    """The requested target is not a safe, empty or compatible shadow catalog."""


_IMPORTED_JSON = {
    "settings.json", "favorites.json", "favorites.seq.json", "tags.json", "marks.json",
}
_RETAINED_NAMES = {"recent.json", "nsfw_manual.json", "nsfw_index.json"}
_TAG_SPECIAL = set("`~!@#$%^&*()+=[]{}|\\:;\"'<>?,")
_MARK_STATES = {"review", "publish", "compare", "delete"}


def _manifest_identity(manifest: dict[str, Any]) -> str:
    files = sorted(
        ({"path": row["path"], "bytes": row["bytes"], "sha256": row["sha256"]}
         for row in manifest["files"]), key=lambda row: row["path"]
    )
    payload = json.dumps(
        {"dataDir": manifest.get("dataDir"), "version": manifest["version"], "files": files},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _separate_target(snapshot_dir: Path, source_dir: Path, database: Path) -> None:
    target = database.resolve()
    for source in (snapshot_dir.resolve(), source_dir.resolve()):
        if target == source or source in target.parents or target in source.parents:
            raise ShadowTargetError("Shadow database must be outside snapshot and source directories")


def _path_style(path: str):
    if ntpath.splitdrive(path)[0] or path.startswith("\\\\"):
        return ntpath
    return posixpath


def _root_contains(root: str, path: str) -> bool:
    module = _path_style(path)
    if _path_style(root) is not module:
        return False
    try:
        normalized_root = module.normpath(root)
        normalized_path = module.normpath(path)
        if not module.isabs(normalized_root) or not module.isabs(normalized_path):
            return False
        root_drive, root_tail = module.splitdrive(normalized_root)
        path_drive, path_tail = module.splitdrive(normalized_path)
        if root_drive != path_drive:
            return False
        separator = "\\" if module is ntpath else "/"
        root_parts = [part for part in root_tail.replace("\\", separator).replace("/", separator).split(separator) if part]
        path_parts = [part for part in path_tail.replace("\\", separator).replace("/", separator).split(separator) if part]
        return path_parts[:len(root_parts)] == root_parts
    except (ValueError, OSError):
        return False


def _tag_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.split("/") if part.strip()]
    normalized = "/".join(parts)
    if not normalized or len(normalized) > 40 or any(
        ch in _TAG_SPECIAL for ch in normalized
    ) or any(len(part) > 16 for part in parts):
        return None
    return normalized


def _timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise ValueError("timestamp is not numeric")


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _legacy_json(snapshot: Path, entries: dict[str, dict[str, Any]], name: str):
    entry = entries.get(name)
    if entry is None:
        return None
    try:
        return json.loads((snapshot / "files" / Path(*name.split("/"))).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # verify_snapshot already checks hashes and preflight JSON validity. This
        # branch protects against a malformed but otherwise self-consistent manifest.
        raise SnapshotError(f"Cannot decode verified snapshot file {name}: {exc}") from exc


def _mapping_row(connection, job_id: int, source_hash: str, source_kind: str, source_key: str):
    return connection.execute(
        "SELECT target_kind,target_uuid,outcome FROM migration_mapping "
        "WHERE source_kind=? AND source_hash=? AND source_key=?",
        (source_kind, source_hash, source_key),
    ).fetchone()


def _record(connection, job_id: int, source_hash: str, source_kind: str,
            source_key: str, target_kind: str, target_uuid: str, outcome: str) -> None:
    connection.execute(
        "INSERT INTO migration_mapping(job_id,source_kind,source_hash,source_key,target_kind,target_uuid,outcome) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, source_kind, source_hash, source_key, target_kind, target_uuid, outcome),
    )


def import_snapshot(snapshot, database) -> dict[str, Any]:
    """Import one verified legacy snapshot to a new or resumable shadow catalog.

    The source snapshot and original data directory are read-only inputs. All catalog
    rows, mapping progress, and terminal job state commit in one SQLite transaction.
    The returned report contains decoded original values for quarantined entries;
    the verified snapshot remains the byte-exact source evidence.
    """
    snapshot_dir = Path(snapshot).resolve(strict=True)
    manifest = verify_snapshot(snapshot_dir)
    source_dir = Path(manifest["dataDir"]).expanduser().resolve(strict=False)
    target = Path(database).expanduser().absolute()
    _separate_target(snapshot_dir, source_dir, target)
    if target.exists() and not target.is_file():
        raise ShadowTargetError("Shadow target must be a SQLite file path")

    manifest_hash = _manifest_identity(manifest)
    report: dict[str, Any] = {
        "sourceManifestHash": manifest_hash,
        "snapshot": str(snapshot_dir),
        "sourceDataDir": manifest.get("dataDir"),
        "snapshotVerified": True,
        "databaseRole": "shadow_only",
        "legacyAuthority": "unchanged",
        "mappings": [],
        "internalMappings": [],
        "quarantined": [],
        "compatibleRetained": [],
        "counts": {"mapped": 0, "quarantined": 0, "duplicate": 0},
    }
    manifest_files = {row["path"]: row for row in manifest["files"]}
    raw_docs = {name: _legacy_json(snapshot_dir, manifest_files, name)
                for name in _IMPORTED_JSON if name in manifest_files}
    for path, entry in manifest_files.items():
        leaf = path.rsplit("/", 1)[-1]
        if leaf in _RETAINED_NAMES or path == "trash" or path.startswith("trash/"):
            report["compatibleRetained"].append({"path": path, "bytes": entry["bytes"],
                                                "sha256": entry["sha256"], "kind": entry["kind"],
                                                "authority": "legacy_json"})
        elif path == "settings.json":
            report["compatibleRetained"].append({
                "path": path, "bytes": entry["bytes"], "sha256": entry["sha256"],
                "kind": entry["kind"], "authority": "legacy_json",
                "status": "partially_imported", "importedFields": ["roots", "fav_cats"],
            })

    target.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(target)
    try:
        initialize_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        prior_jobs = connection.execute(
            "SELECT id,source_hash,state FROM migration_job WHERE source_kind='legacy-json'"
        ).fetchall()
        mismatched = [row for row in prior_jobs if row["source_hash"] != manifest_hash]
        if mismatched:
            raise SnapshotConflictError(
                "This shadow database already contains a different source manifest; create a new shadow database"
            )
        if not prior_jobs:
            occupied = sum(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "library", "library_root", "local_profile", "media_asset", "media_revision",
                    "user_media_state", "collection", "collection_item", "tag", "tag_assignment",
                    "migration_job", "migration_mapping",
                )
            )
            if occupied:
                raise ShadowTargetError("Existing catalog is not an empty compatible shadow target")
        job = next((row for row in prior_jobs if row["source_hash"] == manifest_hash), None)
        if job is None:
            connection.execute(
                "INSERT INTO migration_job(uuid,source_kind,source_hash,state) VALUES (?,?,?,'importing')",
                (new_identity(), "legacy-json", manifest_hash),
            )
            job_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            job_id = job["id"]
            if job["state"] not in ("importing", "reconciled"):
                raise ShadowTargetError(f"Cannot resume import from job state {job['state']}")

        def mapped(source_kind: str, source_key: str, target_kind: str,
                   target_uuid: str, *, duplicate: bool = False) -> None:
            previous = _mapping_row(connection, job_id, manifest_hash, source_kind, source_key)
            if previous:
                outcome = "quarantined" if previous["outcome"] == "quarantined" else "duplicate"
                target_kind = previous["target_kind"]
                target_uuid = previous["target_uuid"]
            else:
                outcome = "duplicate" if duplicate else "mapped"
                _record(connection, job_id, manifest_hash, source_kind, source_key,
                        target_kind, target_uuid, outcome)
            row = {"sourceKind": source_kind, "sourceKey": source_key,
                   "targetKind": target_kind, "targetUuid": target_uuid, "outcome": outcome}
            report["mappings"].append(row)
            report["counts"][outcome] += 1

        def internal_mapping(source_key: str, target_kind: str,
                             target_uuid: str, *, duplicate: bool = False) -> None:
            report["internalMappings"].append({
                "sourceKey": source_key, "targetKind": target_kind,
                "targetUuid": target_uuid,
                "outcome": "duplicate" if duplicate else "mapped",
            })

        def quarantine(source_kind: str, source_key: str, raw: Any, reason: str) -> None:
            previous = _mapping_row(connection, job_id, manifest_hash, source_kind, source_key)
            target_uuid = previous["target_uuid"] if previous else new_identity()
            if not previous:
                _record(connection, job_id, manifest_hash, source_kind, source_key,
                        "quarantine", target_uuid, "quarantined")
            report["quarantined"].append({"sourceKind": source_kind, "sourceKey": source_key,
                                          "reason": reason, "raw": raw})
            report["mappings"].append({"sourceKind": source_kind, "sourceKey": source_key,
                                       "targetKind": "quarantine", "targetUuid": target_uuid,
                                       "outcome": "quarantined"})
            report["counts"]["quarantined"] += 1

        def entity_uuid(source_key: str, kind: str, table: str, create):
            previous = _mapping_row(connection, job_id, manifest_hash, "catalog", source_key)
            if previous:
                row = connection.execute(f"SELECT id,uuid FROM {table} WHERE uuid=?",
                                         (previous["target_uuid"],)).fetchone()
                if row is None:
                    raise ShadowTargetError(f"Stored mapping target is missing: {source_key}")
                internal_mapping(source_key, kind, row["uuid"], duplicate=True)
                return row["id"], row["uuid"], False
            external_id = new_identity()
            internal_id = create(external_id)
            _record(connection, job_id, manifest_hash, "catalog", source_key,
                    kind, external_id, "mapped")
            internal_mapping(source_key, kind, external_id)
            return internal_id, external_id, True

        library_id, library_uuid, _ = entity_uuid(
            "catalog:library", "library", "library",
            lambda external_id: (connection.execute(
                "INSERT INTO library(uuid,name) VALUES (?,?)", (external_id, "Local library")
            ).lastrowid),
        )
        profile_id, profile_uuid, _ = entity_uuid(
            "catalog:profile", "profile", "local_profile",
            lambda external_id: connection.execute(
                "INSERT INTO local_profile(uuid,library_id,name) VALUES (?,?,?)",
                (external_id, library_id, "Local"),
            ).lastrowid,
        )
        def ensure_root(path: str, source_key: str, source_kind: str):
            previous = _mapping_row(connection, job_id, manifest_hash, "catalog", source_key)
            if previous:
                row = connection.execute("SELECT id,uuid,path FROM library_root WHERE uuid=?",
                                         (previous["target_uuid"],)).fetchone()
                if row is None:
                    raise ShadowTargetError(f"Stored root mapping is missing: {source_key}")
                if source_kind == "catalog":
                    internal_mapping(source_key, "root", row["uuid"], duplicate=True)
                else:
                    mapped(source_kind, source_key, "root", row["uuid"])
                return row["id"], row["uuid"]
            # CMV3-LOCAL-FIX(D2, 2026-10-12) —— 按 path_key 兜底复用已存在的 root。
            # 上游只用 source_key 去重，但 schema 的唯一约束是
            # UNIQUE(library_id, path_key)（storage/schema.py:24）—— 这两个键不是
            # 同一个东西。resolve_root() 为「无主文件」造占位 root 时，
            # source_key = f"catalog:placeholder-root:{path}"（**文件**路径），
            # 而 INSERT 的 path_key = path（**父目录**）。于是同一目录的第二个无主
            # 文件：source_key 不同 ⇒ 查不到 previous ⇒ 再 INSERT 同一 path_key
            # ⇒ UNIQUE constraint failed: library_root.library_id, library_root.path_key
            # ⇒ prepare_migration 整体 failed（readyForActivation=False）。
            # 触发条件（2026-10-12 最小重现实测，dev/_stage5_d2_minimal.py）：
            # favorites / tags / marks / favorites.seq 中出现 ≥2 个文件同目录，且该目录
            # 不落在任何已登记 settings.roots 之下 —— 收藏夹里同目录多文件是**常态**，
            # 故真实用户几乎必中（主人真库 layer2 即因此 failed）。
            # import_trash 不受影响（它 SELECT ... WHERE uuid=? 复用已有 root，不新建）。
            # 修复：source_key 未命中时，按 (library_id, path_key) 复用既有 root 并照实
            # 记 mapping —— 同一 path_key 本就该是同一个 root（唯一约束即此意）。
            existing = connection.execute(
                "SELECT id,uuid,path FROM library_root WHERE library_id=? AND path_key=?",
                (library_id, path)).fetchone()
            if existing is not None:
                _record(connection, job_id, manifest_hash, "catalog", source_key,
                        "root", existing["uuid"], "mapped")
                if source_kind == "catalog":
                    internal_mapping(source_key, "root", existing["uuid"])
                else:
                    mapped(source_kind, source_key, "root", existing["uuid"])
                return existing["id"], existing["uuid"]
            external_id = new_identity()
            cursor = connection.execute(
                "INSERT INTO library_root(uuid,library_id,path,path_key,case_sensitive,availability) "
                "VALUES (?,?,?,?,NULL,'unknown')",
                (external_id, library_id, path, path),
            )
            _record(connection, job_id, manifest_hash, "catalog", source_key,
                    "root", external_id, "mapped")
            if source_kind == "catalog":
                internal_mapping(source_key, "root", external_id)
            else:
                mapped(source_kind, source_key, "root", external_id)
            return cursor.lastrowid, external_id

        settings = raw_docs.get("settings.json")
        configured_roots: list[tuple[str, int, str]] = []
        if settings is not None:
            if not isinstance(settings, dict):
                quarantine("settings.json", "settings.json#", settings, "settings must be a JSON object")
            else:
                roots_value = settings.get("roots", [])
                if not isinstance(roots_value, list):
                    quarantine("settings.json", "settings.json#/roots", roots_value,
                               "roots must be an array")
                else:
                    first_root_by_path: dict[str, tuple[int, str]] = {}
                    for index, value in enumerate(roots_value):
                        key = f"settings.json#/roots/{index}"
                        if not isinstance(value, str) or not value.strip():
                            quarantine("settings.json", key, value, "root must be a non-empty path string")
                            continue
                        if value in first_root_by_path:
                            root_id, root_uuid = first_root_by_path[value]
                            mapped("settings.json", key, "root", root_uuid, duplicate=True)
                        else:
                            root_id, root_uuid = ensure_root(value, key, "settings.json")
                            first_root_by_path[value] = (root_id, root_uuid)
                        configured_roots.append((value, root_id, root_uuid))

        def resolve_root(path: str) -> tuple[int, str]:
            matches = [(root, root_id, root_uuid) for root, root_id, root_uuid in configured_roots
                       if _root_contains(root, path)]
            if matches:
                matches.sort(key=lambda item: len(_path_style(item[0]).normpath(item[0])), reverse=True)
                return matches[0][1], matches[0][2]
            module = _path_style(path)
            parent = module.dirname(path.rstrip("\\/")) or path
            key = f"catalog:placeholder-root:{path}"
            return ensure_root(parent, key, "catalog")

        def ensure_asset(path: str) -> tuple[int, str]:
            key = f"catalog:asset:{path}"
            previous = _mapping_row(connection, job_id, manifest_hash, "catalog", key)
            if previous:
                row = connection.execute("SELECT id,uuid FROM media_asset WHERE uuid=?",
                                         (previous["target_uuid"],)).fetchone()
                if row is None:
                    raise ShadowTargetError(f"Stored asset mapping is missing: {path}")
                return row["id"], row["uuid"]
            root_id, _ = resolve_root(path)
            external_id = new_identity()
            cursor = connection.execute(
                "INSERT INTO media_asset(uuid,library_id,root_id,path,live_path_key,availability) "
                "VALUES (?,?,?,?,NULL,'missing')",
                (external_id, library_id, root_id, path),
            )
            _record(connection, job_id, manifest_hash, "catalog", key,
                    "asset", external_id, "mapped")
            return cursor.lastrowid, external_id

        def legacy_path(source_kind: str, key: str, raw: Any) -> tuple[int, str] | None:
            if not isinstance(raw, str) or not raw.strip():
                quarantine(source_kind, key, raw, "path key must be a non-empty string")
                return None
            return ensure_asset(raw)

        # Preserve empty user-defined categories as first-class collections.
        category_cache: dict[str, tuple[int, str]] = {}

        def ensure_category(name: str, source_key: str, source_kind: str,
                            duplicate: bool = False) -> tuple[int, str]:
            if name in category_cache:
                collection_id, collection_uuid = category_cache[name]
                mapped(source_kind, source_key, "collection", collection_uuid,
                       duplicate=duplicate)
                return collection_id, collection_uuid
            previous = _mapping_row(connection, job_id, manifest_hash, "catalog",
                                    f"catalog:collection:{name}")
            if previous:
                found = connection.execute("SELECT id,uuid FROM collection WHERE uuid=?",
                                           (previous["target_uuid"],)).fetchone()
                if not found:
                    raise ShadowTargetError(f"Stored collection mapping is missing: {name}")
                collection_id, collection_uuid = found["id"], found["uuid"]
                created = False
            else:
                found = connection.execute(
                    "SELECT id,uuid FROM collection WHERE profile_id=? AND name=?",
                    (profile_id, name),
                ).fetchone()
                if found:
                    collection_id, collection_uuid, created = found["id"], found["uuid"], False
                else:
                    collection_uuid = new_identity()
                    collection_id = connection.execute(
                        "INSERT INTO collection(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                        (collection_uuid, library_id, profile_id, name),
                    ).lastrowid
                    created = True
                _record(connection, job_id, manifest_hash, "catalog",
                        f"catalog:collection:{name}", "collection", collection_uuid,
                        "mapped" if created else "duplicate")
            category_cache[name] = (collection_id, collection_uuid)
            mapped(source_kind, source_key, "collection", collection_uuid, duplicate=duplicate)
            return collection_id, collection_uuid

        fav_categories = []
        if isinstance(settings, dict):
            fav_categories = settings.get("fav_cats", [])
            if not isinstance(fav_categories, list):
                quarantine("settings.json", "settings.json#/fav_cats", fav_categories,
                           "fav_cats must be an array")
                fav_categories = []
        for index, value in enumerate(fav_categories):
            key = f"settings.json#/fav_cats/{index}"
            if not isinstance(value, str) or not value.strip():
                quarantine("settings.json", key, value, "category must be a non-empty string")
                continue
            ensure_category(value, key, "settings.json", duplicate=value in category_cache)

        favorites = raw_docs.get("favorites.json")
        favorite_values: dict[str, dict[str, Any]] = {}
        if favorites is not None:
            if not isinstance(favorites, dict):
                quarantine("favorites.json", "favorites.json#", favorites,
                           "favorites must be a JSON object")
            else:
                for path, value in favorites.items():
                    key = f"favorites.json#/{_pointer(path)}"
                    try:
                        if isinstance(value, dict):
                            raw_time = value.get("t")
                            if not raw_time:
                                raise ValueError("favorite timestamp missing or zero")
                            timestamp = _timestamp(raw_time)
                            category = str(value.get("cat") or "默认")
                        elif isinstance(value, (int, float)) and not isinstance(value, bool):
                            timestamp = _timestamp(value)
                            if not timestamp:
                                raise ValueError("favorite timestamp is zero")
                            category = "默认"
                        else:
                            raise ValueError("favorite value must contain a timestamp")
                    except (TypeError, ValueError, OverflowError) as exc:
                        quarantine("favorites.json", key, value, str(exc))
                        continue
                    asset = legacy_path("favorites.json", key, path)
                    if asset is None:
                        continue
                    favorite_values[path] = {"asset": asset, "timestamp": str(timestamp),
                                             "category": category, "raw": value, "key": key}
                    _, asset_uuid = asset
                    mapped("favorites.json", key, "asset", asset_uuid)
                    ensure_category(category, key + ":category", "favorites.json")

        seq_values: dict[str, int] = {}
        sequences = raw_docs.get("favorites.seq.json")
        if sequences is not None:
            if not isinstance(sequences, dict):
                quarantine("favorites.seq.json", "favorites.seq.json#", sequences,
                           "favorite sequence data must be a JSON object")
            else:
                for path, raw_seq in sequences.items():
                    key = f"favorites.seq.json#/{_pointer(path)}"
                    try:
                        seq = _timestamp(raw_seq)
                        if seq < 0 or seq > 9_223_372_036_854_775_807:
                            raise ValueError("favorite sequence is outside SQLite integer range")
                    except (TypeError, ValueError, OverflowError) as exc:
                        quarantine("favorites.seq.json", key, raw_seq, str(exc))
                        continue
                    asset = legacy_path("favorites.seq.json", key, path)
                    if asset is None:
                        continue
                    seq_values[path] = seq
                    mapped("favorites.seq.json", key, "asset", asset[1])

        # Store current and cancelled favorite intent together with its original sequence.
        all_user_paths = sorted(set(favorite_values) | set(seq_values))
        for path in all_user_paths:
            asset_id, _ = ensure_asset(path)
            favorite = favorite_values.get(path)
            category = favorite["category"] if favorite else None
            favorite_time = favorite["timestamp"] if favorite else None
            connection.execute(
                "INSERT INTO user_media_state(profile_id,library_id,asset_id,favorite,favorite_category,"
                "favorite_time,favorite_seq,favorite_intent) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,asset_id) DO UPDATE SET favorite=excluded.favorite,"
                "favorite_category=excluded.favorite_category,favorite_time=excluded.favorite_time,"
                "favorite_seq=excluded.favorite_seq,favorite_intent=excluded.favorite_intent",
                (profile_id, library_id, asset_id, int(favorite is not None), category,
                 favorite_time, seq_values.get(path, 0), int(favorite is not None)),
            )
            if favorite:
                collection_id, _ = category_cache[favorite["category"]]
                connection.execute(
                    "INSERT OR IGNORE INTO collection_item(collection_id,library_id,asset_id) VALUES (?,?,?)",
                    (collection_id, library_id, asset_id),
                )

        tags = raw_docs.get("tags.json")
        if tags is not None:
            if not isinstance(tags, dict):
                quarantine("tags.json", "tags.json#", tags, "tags must be a JSON object")
            else:
                for path, values in tags.items():
                    key = f"tags.json#/{_pointer(path)}"
                    if not isinstance(values, list):
                        quarantine("tags.json", key, values, "tag value must be an array")
                        continue
                    asset = legacy_path("tags.json", key, path)
                    if asset is None:
                        continue
                    mapped("tags.json", key, "asset", asset[1])
                    seen_tags: set[str] = set()
                    tag_cache: dict[str, tuple[int, str]] = {}
                    for index, raw_tag in enumerate(values):
                        tag_key = f"{key}/{index}"
                        name = _tag_name(raw_tag)
                        if name is None:
                            quarantine("tags.json", tag_key, raw_tag, "invalid legacy tag name")
                            continue
                        if name in seen_tags:
                            mapped("tags.json", tag_key, "tag_assignment", tag_cache[name][1], duplicate=True)
                            continue
                        seen_tags.add(name)
                        catalog_key = f"catalog:tag:{name}"
                        previous = _mapping_row(connection, job_id, manifest_hash, "catalog", catalog_key)
                        if previous:
                            tag_row = connection.execute("SELECT id,uuid FROM tag WHERE uuid=?",
                                                         (previous["target_uuid"],)).fetchone()
                            if not tag_row:
                                raise ShadowTargetError(f"Stored tag mapping is missing: {name}")
                            tag_id, tag_uuid = tag_row["id"], tag_row["uuid"]
                            created = False
                        else:
                            tag_row = connection.execute(
                                "SELECT id,uuid FROM tag WHERE profile_id=? AND name=?",
                                (profile_id, name),
                            ).fetchone()
                            if tag_row:
                                tag_id, tag_uuid, created = tag_row["id"], tag_row["uuid"], False
                            else:
                                tag_uuid = new_identity()
                                tag_id = connection.execute(
                                    "INSERT INTO tag(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                                    (tag_uuid, library_id, profile_id, name),
                                ).lastrowid
                                created = True
                            _record(connection, job_id, manifest_hash, "catalog", catalog_key,
                                    "tag", tag_uuid, "mapped" if created else "duplicate")
                        tag_cache[name] = (tag_id, tag_uuid)
                        connection.execute(
                            "INSERT OR IGNORE INTO tag_assignment(tag_id,library_id,asset_id) VALUES (?,?,?)",
                            (tag_id, library_id, asset[0]),
                        )
                        mapped("tags.json", tag_key, "tag_assignment", tag_uuid)

        marks = raw_docs.get("marks.json")
        if marks is not None:
            if not isinstance(marks, dict):
                quarantine("marks.json", "marks.json#", marks, "marks must be a JSON object")
            else:
                for path, value in marks.items():
                    key = f"marks.json#/{_pointer(path)}"
                    if not isinstance(value, dict) or value.get("state") not in _MARK_STATES:
                        quarantine("marks.json", key, value, "mark must have a recognized state")
                        continue
                    try:
                        updated = _timestamp(value.get("updated") or 0)
                    except (TypeError, ValueError, OverflowError) as exc:
                        quarantine("marks.json", key, value, f"invalid mark timestamp: {exc}")
                        continue
                    asset = legacy_path("marks.json", key, path)
                    if asset is None:
                        continue
                    connection.execute(
                        "INSERT INTO user_media_state(profile_id,library_id,asset_id,mark_state,mark_updated) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(profile_id,asset_id) DO UPDATE SET "
                        "mark_state=excluded.mark_state,mark_updated=excluded.mark_updated",
                        (profile_id, library_id, asset[0], value["state"], str(updated)),
                    )
                    mapped("marks.json", key, "asset", asset[1])

        # Every valid source-key mapping gets a target UUID; duplicate map outcomes
        # remain visible to the reconciliation report instead of being silently lost.
        current_job_state = connection.execute(
            "SELECT state FROM migration_job WHERE id=?", (job_id,)
        ).fetchone()["state"]
        if current_job_state != "reconciled":
            connection.execute("UPDATE migration_job SET state='reconciled' WHERE id=?", (job_id,))
        connection.commit()
        report["libraryUuid"] = library_uuid
        report["profileUuid"] = profile_uuid
        report["jobUuid"] = connection.execute(
            "SELECT uuid FROM migration_job WHERE id=?", (job_id,)
        ).fetchone()["uuid"]
        report["assetCount"] = connection.execute("SELECT count(*) FROM media_asset").fetchone()[0]
        report["rootCount"] = connection.execute("SELECT count(*) FROM library_root").fetchone()[0]
        report["collectionCount"] = connection.execute("SELECT count(*) FROM collection").fetchone()[0]
        report["tagCount"] = connection.execute("SELECT count(*) FROM tag").fetchone()[0]
        report["state"] = "reconciled"
        return report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
