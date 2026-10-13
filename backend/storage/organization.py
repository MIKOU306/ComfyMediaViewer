"""Transactional user-organization repository for the catalog schema v1.

Public methods address a profile and media asset by their external UUIDs. Paths
are returned only as legacy projection keys; they never select an identity.
This repository does not change the application's active storage mode.
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from .database import initialize_schema, new_identity


DEFAULT_CATEGORY = "默认"
MARK_STATES = {"review", "publish", "compare", "delete"}
TAG_SPECIAL = set("`~!@#$%^&*()+=[]{}|\\:;\"'<>?,")
MAX_SQLITE_INT = 9_223_372_036_854_775_807


class OrganizationError(RuntimeError):
    """Base error for a rejected user-organization operation."""


class CatalogUnavailableError(OrganizationError):
    """The initialized catalog file is missing or cannot be opened."""


class CatalogSchemaError(OrganizationError):
    """The target file is not an initialized catalog database."""


class ProjectionConflictError(OrganizationError):
    """The legacy projection cannot represent multiple asset identities at one path."""


class NotFoundError(OrganizationError):
    """A profile, asset, or category UUID does not exist."""


class ScopeMismatchError(OrganizationError):
    """A UUID exists but belongs to another profile or logical library."""


class StaleIntentError(OrganizationError):
    """An older favorite sequence cannot replace a newer user intent."""


class IntentConflictError(OrganizationError):
    """The same favorite sequence was reused with a different payload."""


class CategoryError(OrganizationError):
    """A category operation violates the profile's category rules."""


class TagError(OrganizationError):
    """A tag name is invalid."""


def _category_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CategoryError("Category name must be a non-empty string")
    return value.strip()


def _tag_name(value: Any) -> str:
    if not isinstance(value, str):
        raise TagError("Tag names must be strings")
    parts = [part.strip() for part in value.split("/") if part.strip()]
    normalized = "/".join(parts)
    if (not normalized or len(normalized) > 40 or
            any(ch in TAG_SPECIAL for ch in normalized) or
            any(not part or len(part) > 16 for part in parts)):
        raise TagError(f"Invalid tag name: {value!r}")
    return normalized


class OrganizationRepository:
    """Read and write favorites, favorite categories, tags, and work marks.

    Each method opens a short-lived SQLite connection. Mutations run in one
    ``BEGIN IMMEDIATE`` transaction and roll back on any exception. ``read_projection``
    returns ``favorites``, ``seq``, ``tags``, ``marks``, and ``cats`` in shapes
    compatible with the existing JSON data files.
    """

    def __init__(self, database: str | Path):
        self.database = Path(database)

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        try:
            database = self.database.resolve(strict=True)
            if not database.is_file():
                raise CatalogUnavailableError("Catalog database file does not exist")
            uri = database.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, CatalogUnavailableError):
                raise
            raise CatalogUnavailableError(f"Cannot open existing catalog: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            has_history = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not has_history:
                raise CatalogSchemaError("Catalog has not been initialized by the storage layer")
            if connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0:
                raise CatalogSchemaError("Catalog has no applied schema migration")
            initialize_schema(connection)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _profile(connection: sqlite3.Connection, profile_uuid: str):
        row = connection.execute(
            "SELECT id,uuid,library_id FROM local_profile WHERE uuid=?", (profile_uuid,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown profile UUID: {profile_uuid}")
        return row

    @staticmethod
    def _asset(connection: sqlite3.Connection, profile, asset_uuid: str):
        row = connection.execute(
            "SELECT id,uuid,library_id,path FROM media_asset WHERE uuid=?", (asset_uuid,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown media asset UUID: {asset_uuid}")
        if row["library_id"] != profile["library_id"]:
            raise ScopeMismatchError("Media asset belongs to another library")
        return row

    @staticmethod
    def _category_by_name(connection: sqlite3.Connection, profile, name: str):
        return connection.execute(
            "SELECT id,uuid,name FROM collection WHERE profile_id=? AND library_id=? AND name=?",
            (profile["id"], profile["library_id"], name),
        ).fetchone()

    def read_projection(self, profile_uuid: str) -> dict[str, Any]:
        """Return the profile's legacy-shaped organization state without path lookup."""
        with self._transaction(write=False) as connection:
            profile = self._profile(connection, profile_uuid)
            rows = connection.execute(
                "SELECT s.favorite,s.favorite_category,s.favorite_time,s.favorite_seq,"
                "s.favorite_intent,s.mark_state,s.mark_updated,a.uuid,a.path "
                "FROM user_media_state s JOIN media_asset a ON a.id=s.asset_id "
                "WHERE s.profile_id=? AND s.library_id=? ORDER BY a.path COLLATE BINARY,a.uuid",
                (profile["id"], profile["library_id"]),
            ).fetchall()
            path_owners: dict[str, str] = {}

            def claim_path(path: str, asset_uuid: str) -> None:
                existing = path_owners.get(path)
                if existing is not None and existing != asset_uuid:
                    raise ProjectionConflictError(
                        f"Legacy path projection is ambiguous for {path!r}: "
                        f"assets {existing} and {asset_uuid}"
                    )
                path_owners[path] = asset_uuid

            favorites: dict[str, dict[str, str]] = {}
            seq: dict[str, int] = {}
            marks: dict[str, dict[str, str | None]] = {}
            for row in rows:
                claim_path(row["path"], row["uuid"])
                if row["favorite"]:
                    record = {"t": row["favorite_time"]}
                    if row["favorite_category"]:
                        record["cat"] = row["favorite_category"]
                    favorites[row["path"]] = record
                if row["favorite_seq"] > 0 or row["favorite"] or row["favorite_intent"]:
                    seq[row["path"]] = row["favorite_seq"]
                if row["mark_state"] is not None:
                    marks[row["path"]] = {
                        "state": row["mark_state"], "updated": row["mark_updated"]
                    }

            # Imported sequence zero is distinguishable from a mark-only row through
            # its durable source mapping. Keep that legacy entry in the projection.
            zero_sequences = connection.execute(
                "SELECT a.path,s.favorite_seq FROM migration_mapping m "
                "JOIN migration_job j ON j.id=m.job_id "
                "JOIN media_asset a ON a.uuid=m.target_uuid "
                "JOIN user_media_state s ON s.asset_id=a.id AND s.profile_id=? "
                "WHERE j.source_kind='legacy-json' AND m.source_kind='favorites.seq.json' "
                "AND m.outcome IN ('mapped','duplicate') AND s.library_id=? AND s.favorite_seq=0",
                (profile["id"], profile["library_id"]),
            ).fetchall()
            for row in zero_sequences:
                seq[row["path"]] = 0

            tag_rows = connection.execute(
                "SELECT a.uuid,a.path,t.name FROM tag_assignment ta "
                "JOIN tag t ON t.id=ta.tag_id AND t.profile_id=? "
                "JOIN media_asset a ON a.id=ta.asset_id AND a.library_id=ta.library_id "
                "WHERE ta.library_id=? ORDER BY a.path COLLATE BINARY,t.name COLLATE BINARY",
                (profile["id"], profile["library_id"]),
            ).fetchall()
            tags: dict[str, list[str]] = {}
            for row in tag_rows:
                claim_path(row["path"], row["uuid"])
                tags.setdefault(row["path"], []).append(row["name"])

            collection_assets = connection.execute(
                "SELECT a.uuid,a.path FROM collection_item ci "
                "JOIN collection c ON c.id=ci.collection_id AND c.profile_id=? "
                "JOIN media_asset a ON a.id=ci.asset_id AND a.library_id=ci.library_id "
                "WHERE ci.library_id=?",
                (profile["id"], profile["library_id"]),
            ).fetchall()
            for row in collection_assets:
                claim_path(row["path"], row["uuid"])

            categories = connection.execute(
                "SELECT c.uuid,c.name,count(DISTINCT ci.asset_id) AS count "
                "FROM collection c LEFT JOIN collection_item ci "
                "ON ci.collection_id=c.id AND ci.library_id=c.library_id "
                "WHERE c.profile_id=? AND c.library_id=? "
                "GROUP BY c.id ORDER BY c.name COLLATE BINARY,c.uuid",
                (profile["id"], profile["library_id"]),
            ).fetchall()
            return {
                "favorites": favorites,
                "seq": seq,
                "tags": tags,
                "marks": marks,
                "cats": [{"uuid": row["uuid"], "name": row["name"], "count": row["count"]}
                         for row in categories],
            }

    def set_favorite(self, profile_uuid: str, asset_uuid: str, *, favorite: bool,
                     seq: int, category: str | None = None,
                     favorite_time: str | None = None) -> dict[str, Any]:
        """Apply a monotonic favorite intent; equal identical retries are idempotent.

        A first ``favorite=True`` with sequence zero conflicts with the implicit
        initial ``(seq=0, favorite=False)`` state. The HTTP adapter must allocate a
        sequence greater than the stored value, including for legacy clients that
        do not send a sequence.
        """
        if not isinstance(favorite, bool):
            raise OrganizationError("favorite must be a boolean")
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= MAX_SQLITE_INT:
            raise OrganizationError("seq must be a non-negative SQLite integer")
        if favorite_time is not None and not isinstance(favorite_time, str):
            raise OrganizationError("favorite_time must be text to preserve its value")
        desired_category = _category_name(category or DEFAULT_CATEGORY) if favorite else None
        if not favorite:
            favorite_time = None

        with self._transaction(write=True) as connection:
            profile = self._profile(connection, profile_uuid)
            asset = self._asset(connection, profile, asset_uuid)
            current = connection.execute(
                "SELECT favorite,favorite_category,favorite_time,favorite_seq,favorite_intent "
                "FROM user_media_state WHERE profile_id=? AND asset_id=?",
                (profile["id"], asset["id"]),
            ).fetchone()
            if current is None:
                current_favorite, current_category, current_time, current_seq, current_intent = 0, None, None, 0, 0
            else:
                current_favorite = current["favorite"]
                current_category = current["favorite_category"]
                current_time = current["favorite_time"]
                current_seq = current["favorite_seq"]
                current_intent = current["favorite_intent"]

            if seq < current_seq:
                raise StaleIntentError(f"Favorite intent sequence {seq} is older than {current_seq}")
            if seq == current_seq:
                same_payload = (
                    current_favorite == int(favorite) and current_intent == int(favorite) and
                    current_category == desired_category and
                    (favorite_time is None or current_time == favorite_time)
                )
                if same_payload:
                    return {"status": "idempotent", "favorite": bool(current_favorite),
                            "seq": current_seq, "category": current_category,
                            "favorite_time": current_time}
                raise IntentConflictError(f"Favorite sequence {seq} was already used with another payload")

            collection = None
            if favorite:
                collection = self._category_by_name(connection, profile, desired_category)
                if collection is None:
                    # 分类名必须非空。放开自动创建后，这条收紧规则容易被削弱：
                    # 空名会真的建出一条「」分类，而改动前它是 NotFoundError。
                    # 空白名同样拒绝 —— 它同样不是用户会选的分类。
                    if not desired_category or not desired_category.strip():
                        raise NotFoundError("Favorite category name is required")
                    # F3：分类不存在时**幂等创建**，而不是拒绝。
                    # 查看器（comp-viewer.js）、批量操作（comp-grid.js）与侧栏
                    # 都承诺「输入新分类名即可保存」，但只有侧栏那条路径会先建分类，
                    # 于是 SQLite 下从查看器/批量输入新名必然失败：
                    #   NotFoundError: Unknown favorite category: X
                    # 并发同名不能产生重复分类：唯一约束（profile_id,name）兜底，
                    # 撞约束时在**本事务内**重查采用已存在的那条，不回滚 ——
                    # 回滚会把同事务里已写入的收藏意图一并丢掉。
                    # 作用域仍受 profile_id / library_id 约束，不会跨库或跨用户建分类。
                    collection_uuid = new_identity()
                    try:
                        collection_id = connection.execute(
                            "INSERT INTO collection(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                            (collection_uuid, profile["library_id"], profile["id"], desired_category),
                        ).lastrowid
                    except sqlite3.IntegrityError:
                        existing = connection.execute(
                            "SELECT id,uuid,name FROM collection WHERE profile_id=? AND name=?",
                            (profile["id"], desired_category)).fetchone()
                        if existing is None:
                            raise
                        collection = dict(existing)
                    else:
                        collection = {"id": collection_id, "uuid": collection_uuid,
                                      "name": desired_category}
            effective_time = favorite_time
            if favorite and effective_time is None:
                effective_time = None
            if favorite:
                # Reasserting an existing favorite or moving it between categories
                # keeps its original timestamp, matching legacy set_fav behavior.
                # A newly favorite asset gets an int-convertible Unix-second value.
                if current_favorite and current_time is not None:
                    effective_time = current_time
                elif effective_time is None:
                    effective_time = str(int(time.time()))

            if current_category:
                old_collection = self._category_by_name(connection, profile, current_category)
                if old_collection is not None:
                    connection.execute(
                        "DELETE FROM collection_item WHERE collection_id=? AND library_id=? AND asset_id=?",
                        (old_collection["id"], profile["library_id"], asset["id"]),
                    )
            connection.execute(
                "INSERT INTO user_media_state(profile_id,library_id,asset_id,favorite,favorite_category,"
                "favorite_time,favorite_seq,favorite_intent) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,asset_id) DO UPDATE SET favorite=excluded.favorite,"
                "favorite_category=excluded.favorite_category,favorite_time=excluded.favorite_time,"
                "favorite_seq=excluded.favorite_seq,favorite_intent=excluded.favorite_intent,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (profile["id"], profile["library_id"], asset["id"], int(favorite),
                 desired_category, effective_time, seq, int(favorite)),
            )
            if favorite:
                connection.execute(
                    "INSERT INTO collection_item(collection_id,library_id,asset_id) VALUES (?,?,?) "
                    "ON CONFLICT(collection_id,asset_id) DO NOTHING",
                    (collection["id"], profile["library_id"], asset["id"]),
                )
            return {"status": "applied", "favorite": favorite, "seq": seq,
                    "category": desired_category, "favorite_time": effective_time}

    def create_category(self, profile_uuid: str, name: str) -> dict[str, str]:
        name = _category_name(name)
        with self._transaction(write=True) as connection:
            profile = self._profile(connection, profile_uuid)
            if self._category_by_name(connection, profile, name):
                raise CategoryError(f"Category already exists: {name}")
            category_uuid = new_identity()
            connection.execute(
                "INSERT INTO collection(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                (category_uuid, profile["library_id"], profile["id"], name),
            )
            return {"uuid": category_uuid, "name": name}

    def rename_category(self, profile_uuid: str, category_uuid: str,
                        name: str) -> dict[str, Any]:
        name = _category_name(name)
        with self._transaction(write=True) as connection:
            profile = self._profile(connection, profile_uuid)
            category = self._category(connection, profile, category_uuid)
            if category["name"] == name:
                return {"status": "unchanged", "uuid": category_uuid, "name": name}
            if self._category_by_name(connection, profile, name):
                raise CategoryError(f"Category already exists: {name}")
            connection.execute("UPDATE collection SET name=? WHERE id=?", (name, category["id"]))
            connection.execute(
                "UPDATE user_media_state SET favorite_category=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE profile_id=? AND library_id=? AND favorite=1 AND favorite_category=?",
                (name, profile["id"], profile["library_id"], category["name"]),
            )
            return {"status": "renamed", "uuid": category_uuid,
                    "oldName": category["name"], "name": name}

    @staticmethod
    def _category(connection: sqlite3.Connection, profile, category_uuid: str):
        row = connection.execute("SELECT id,uuid,profile_id,library_id,name FROM collection WHERE uuid=?",
                                 (category_uuid,)).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown category UUID: {category_uuid}")
        if row["profile_id"] != profile["id"] or row["library_id"] != profile["library_id"]:
            raise ScopeMismatchError("Category belongs to another profile or library")
        return row

    def delete_category(self, profile_uuid: str, category_uuid: str) -> dict[str, Any]:
        with self._transaction(write=True) as connection:
            profile = self._profile(connection, profile_uuid)
            category = self._category(connection, profile, category_uuid)
            if category["name"] == DEFAULT_CATEGORY:
                raise CategoryError("The default category cannot be deleted")
            default = self._category_by_name(connection, profile, DEFAULT_CATEGORY)
            if default is None:
                default_uuid = new_identity()
                default_id = connection.execute(
                    "INSERT INTO collection(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                    (default_uuid, profile["library_id"], profile["id"], DEFAULT_CATEGORY),
                ).lastrowid
            else:
                default_id = default["id"]
            favorites = connection.execute(
                "SELECT asset_id FROM user_media_state WHERE profile_id=? AND library_id=? "
                "AND favorite=1 AND favorite_category=?",
                (profile["id"], profile["library_id"], category["name"]),
            ).fetchall()
            for row in favorites:
                connection.execute(
                    "INSERT INTO collection_item(collection_id,library_id,asset_id) VALUES (?,?,?) "
                    "ON CONFLICT(collection_id,asset_id) DO NOTHING",
                    (default_id, profile["library_id"], row["asset_id"]),
                )
            moved = connection.execute(
                "UPDATE user_media_state SET favorite_category=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE profile_id=? AND library_id=? AND favorite=1 AND favorite_category=?",
                (DEFAULT_CATEGORY, profile["id"], profile["library_id"], category["name"]),
            ).rowcount
            connection.execute("DELETE FROM collection WHERE id=?", (category["id"],))
            return {"status": "deleted", "uuid": category_uuid,
                    "name": category["name"], "favoritesMovedToDefault": moved}

    def set_tags(self, profile_uuid: str, asset_uuid: str,
                 names: list[str]) -> dict[str, Any]:
        if not isinstance(names, list):
            raise TagError("tags must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in names:
            name = _tag_name(raw)
            if name not in seen:
                seen.add(name)
                normalized.append(name)
        with self._transaction(write=True) as connection:
            profile = self._profile(connection, profile_uuid)
            asset = self._asset(connection, profile, asset_uuid)
            connection.execute(
                "DELETE FROM tag_assignment WHERE library_id=? AND asset_id=? AND tag_id IN "
                "(SELECT id FROM tag WHERE profile_id=? AND library_id=?)",
                (profile["library_id"], asset["id"], profile["id"], profile["library_id"]),
            )
            for name in normalized:
                row = connection.execute(
                    "SELECT id FROM tag WHERE profile_id=? AND library_id=? AND name=?",
                    (profile["id"], profile["library_id"], name),
                ).fetchone()
                tag_id = row["id"] if row else connection.execute(
                    "INSERT INTO tag(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                    (new_identity(), profile["library_id"], profile["id"], name),
                ).lastrowid
                connection.execute(
                    "INSERT INTO tag_assignment(tag_id,library_id,asset_id) VALUES (?,?,?)",
                    (tag_id, profile["library_id"], asset["id"]),
                )
            return {"status": "applied", "assetUuid": asset_uuid, "tags": normalized}

    def set_mark(self, profile_uuid: str, asset_uuid: str, *,
                 state: str | None, updated: str | None = None) -> dict[str, Any]:
        if state is not None and state not in MARK_STATES:
            raise OrganizationError(f"Unknown mark state: {state}")
        if updated is not None and not isinstance(updated, str):
            raise OrganizationError("mark updated time must be text")
        if state is None:
            updated = None
        with self._transaction(write=True) as connection:
            profile = self._profile(connection, profile_uuid)
            asset = self._asset(connection, profile, asset_uuid)
            connection.execute(
                "INSERT INTO user_media_state(profile_id,library_id,asset_id,mark_state,mark_updated) "
                "VALUES (?,?,?,?,?) ON CONFLICT(profile_id,asset_id) DO UPDATE SET "
                "mark_state=excluded.mark_state,mark_updated=excluded.mark_updated,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (profile["id"], profile["library_id"], asset["id"], state, updated),
            )
            return {"status": "applied", "assetUuid": asset_uuid,
                    "state": state, "updated": updated}
