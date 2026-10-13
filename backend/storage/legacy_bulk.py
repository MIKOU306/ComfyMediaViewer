"""Atomic replacement of legacy-shaped organization projections.

This adapter writes only an already initialized catalog and never creates assets
or a database. Exact catalog paths are resolved to UUID-backed asset rows first.
"""

from __future__ import annotations

from typing import Any, Mapping

from .database import new_identity
from .asset_lifecycle import visible_clause
from .organization import (
    DEFAULT_CATEGORY,
    MARK_STATES,
    OrganizationRepository,
    OrganizationError,
    ScopeMismatchError,
    TagError,
    _category_name,
    _tag_name,
)


class LegacyBulkRepository(OrganizationRepository):
    """Replace one profile's favorites, tags, or marks in isolated transactions."""

    def __init__(self, database, library_uuid: str, profile_uuid: str):
        super().__init__(database)
        self.profile_uuid = profile_uuid
        self.library_uuid = library_uuid

    def _scope(self, connection):
        profile = self._profile(connection, self.profile_uuid)
        library = connection.execute(
            "SELECT id FROM library WHERE uuid=?", (self.library_uuid,)
        ).fetchone()
        if library is None:
            raise OrganizationError(f"Unknown library UUID: {self.library_uuid}")
        if profile["library_id"] != library["id"]:
            raise ScopeMismatchError("Profile belongs to another library")
        return profile, library

    @staticmethod
    def _resolve_paths(connection, library_id: int, paths):
        if not isinstance(paths, Mapping):
            raise OrganizationError("Legacy projection must be a path-keyed object")
        resolved = {}
        for path in paths:
            if not isinstance(path, str):
                raise OrganizationError("Legacy paths must be strings")
            rows = connection.execute(
                "SELECT id,uuid FROM media_asset a WHERE library_id=? AND path=? AND "
                + visible_clause(connection)
                + " ORDER BY id", (library_id, path),
            ).fetchall()
            if not rows:
                raise OrganizationError(f"Unknown exact media path: {path!r}")
            if len(rows) != 1:
                raise OrganizationError(f"Ambiguous exact media path: {path!r}")
            resolved[path] = rows[0]
        return resolved

    def replace_favorites(self, favorites: dict[str, dict[str, Any]]) -> dict[str, int]:
        """Replace favorite values and category memberships, preserving seq counters."""
        if not isinstance(favorites, Mapping):
            raise OrganizationError("Favorites must be a path-keyed object")
        normalized = {}
        for path, value in favorites.items():
            if not isinstance(value, Mapping):
                raise OrganizationError(f"Favorite value for {path!r} must be an object")
            if "t" not in value or value["t"] is None or value["t"] == "":
                raise OrganizationError(f"Favorite timestamp missing for {path!r}")
            timestamp = str(value["t"])
            if isinstance(value["t"], bool):
                raise OrganizationError("Favorite timestamp must not be boolean")
            category = _category_name(value.get("cat") or DEFAULT_CATEGORY)
            normalized[path] = (timestamp, category)

        with self._transaction(write=True) as connection:
            profile, library = self._scope(connection)
            assets = self._resolve_paths(connection, library["id"], normalized)
            # Clear only favorite fields; keep favorite_seq as the cancellation
            # high-water mark and leave marks/tags untouched.
            connection.execute(
                "DELETE FROM collection_item WHERE library_id=? AND collection_id IN "
                "(SELECT id FROM collection WHERE profile_id=? AND library_id=?)",
                (library["id"], profile["id"], library["id"]),
            )
            connection.execute(
                "UPDATE user_media_state SET favorite=0,favorite_category=NULL,"
                "favorite_time=NULL,favorite_intent=0,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE profile_id=? AND library_id=?",
                (profile["id"], library["id"]),
            )
            categories = {}
            for _, category in normalized.values():
                if category in categories:
                    continue
                row = connection.execute(
                    "SELECT id FROM collection WHERE profile_id=? AND library_id=? AND name=?",
                    (profile["id"], library["id"], category),
                ).fetchone()
                if row is None:
                    row_id = connection.execute(
                        "INSERT INTO collection(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                        (new_identity(), library["id"], profile["id"], category),
                    ).lastrowid
                else:
                    row_id = row["id"]
                categories[category] = row_id

            for path, (timestamp, category) in normalized.items():
                asset = assets[path]
                connection.execute(
                    "INSERT INTO user_media_state(profile_id,library_id,asset_id,favorite,"
                    "favorite_category,favorite_time,favorite_seq,favorite_intent) "
                    "VALUES (?,?,?,1,?,?,0,1) ON CONFLICT(profile_id,asset_id) DO UPDATE SET "
                    "favorite=1,favorite_category=excluded.favorite_category,"
                    "favorite_time=excluded.favorite_time,favorite_intent=1,"
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                    (profile["id"], library["id"], asset["id"], category, timestamp),
                )
                connection.execute(
                    "INSERT INTO collection_item(collection_id,library_id,asset_id) "
                    "VALUES (?,?,?)",
                    (categories[category], library["id"], asset["id"]),
                )
        return {"favorites": len(normalized), "categories": len(categories)}

    def replace_tags(self, tags: dict[str, list[str]]) -> dict[str, int]:
        """Replace assignments while retaining catalog tag definitions, including empty ones."""
        if not isinstance(tags, Mapping):
            raise OrganizationError("Tags must be a path-keyed object")
        normalized = {}
        for path, names in tags.items():
            if not isinstance(names, list):
                raise TagError(f"Tag value for {path!r} must be a list")
            clean = []
            for raw in names:
                name = _tag_name(raw)
                if name not in clean:
                    clean.append(name)
            normalized[path] = clean

        with self._transaction(write=True) as connection:
            profile, library = self._scope(connection)
            assets = self._resolve_paths(connection, library["id"], normalized)
            connection.execute(
                "DELETE FROM tag_assignment WHERE library_id=? AND tag_id IN "
                "(SELECT id FROM tag WHERE profile_id=? AND library_id=?)",
                (library["id"], profile["id"], library["id"]),
            )
            inserted = 0
            for path, names in normalized.items():
                for name in names:
                    row = connection.execute(
                        "SELECT id FROM tag WHERE profile_id=? AND library_id=? AND name=?",
                        (profile["id"], library["id"], name),
                    ).fetchone()
                    tag_id = row["id"] if row else connection.execute(
                        "INSERT INTO tag(uuid,library_id,profile_id,name) VALUES (?,?,?,?)",
                        (new_identity(), library["id"], profile["id"], name),
                    ).lastrowid
                    connection.execute(
                        "INSERT INTO tag_assignment(tag_id,library_id,asset_id) VALUES (?,?,?)",
                        (tag_id, library["id"], assets[path]["id"]),
                    )
                    inserted += 1
        return {"paths": len(normalized), "assignments": inserted}

    def replace_marks(self, marks: dict[str, dict[str, Any]]) -> dict[str, int]:
        """Replace work marks and timestamps while leaving favorites and tags intact."""
        if not isinstance(marks, Mapping):
            raise OrganizationError("Marks must be a path-keyed object")
        normalized = {}
        for path, value in marks.items():
            if not isinstance(value, Mapping) or value.get("state") not in MARK_STATES:
                raise OrganizationError(f"Mark value for {path!r} has an unknown state")
            updated = value.get("updated")
            if updated is not None and (isinstance(updated, bool) or not isinstance(updated, (str, int, float))):
                raise OrganizationError("Mark updated time must be text or numeric")
            normalized[path] = (value["state"], None if updated is None else str(updated))

        with self._transaction(write=True) as connection:
            profile, library = self._scope(connection)
            assets = self._resolve_paths(connection, library["id"], normalized)
            connection.execute(
                "UPDATE user_media_state SET mark_state=NULL,mark_updated=NULL,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE profile_id=? AND library_id=?",
                (profile["id"], library["id"]),
            )
            for path, (state, updated) in normalized.items():
                connection.execute(
                    "INSERT INTO user_media_state(profile_id,library_id,asset_id,mark_state,mark_updated) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(profile_id,asset_id) DO UPDATE SET "
                    "mark_state=excluded.mark_state,mark_updated=excluded.mark_updated,"
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                    (profile["id"], library["id"], assets[path]["id"], state, updated),
                )
        return {"marks": len(normalized)}

