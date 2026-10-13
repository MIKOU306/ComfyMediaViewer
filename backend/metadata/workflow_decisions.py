"""Append-only, profile-scoped human workflow-family decisions.

Decisions associate an existing capture with a target family for one local
profile. They never rewrite capture ownership, source bindings, or execution
evidence.
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .workflow import WorkflowArchive
from .workflow_identity_store import WorkflowIdentityStore, WorkflowIdentityStoreError

SCHEMA_VERSION = 1
PAGE_SIZE_MAX = 100


def _schema_sql() -> tuple[str, ...]:
    return (
        """CREATE TABLE workflow_family_decision_event(
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            library_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL,
            capture_id INTEGER NOT NULL,
            base_workflow_id INTEGER NOT NULL,
            target_workflow_id INTEGER,
            decision_version INTEGER NOT NULL CHECK(decision_version > 0),
            event_kind TEXT NOT NULL CHECK(event_kind IN ('confirm','revoke')),
            reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(library_id,profile_id,capture_id,decision_version),
            UNIQUE(library_id,id),
            CHECK((event_kind='confirm' AND target_workflow_id IS NOT NULL) OR
                  (event_kind='revoke' AND target_workflow_id IS NULL)),
            FOREIGN KEY(library_id,profile_id)
                REFERENCES local_profile(library_id,id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,capture_id,base_workflow_id)
                REFERENCES workflow_capture(library_id,id,workflow_id) ON DELETE RESTRICT,
            FOREIGN KEY(library_id,target_workflow_id)
                REFERENCES workflow_family(library_id,id) ON DELETE RESTRICT)""",
        "CREATE INDEX workflow_family_decision_event_history_idx ON workflow_family_decision_event(library_id,profile_id,capture_id,decision_version)",
        """CREATE TRIGGER workflow_family_decision_event_no_update
            BEFORE UPDATE ON workflow_family_decision_event
            BEGIN SELECT RAISE(ABORT,'workflow family decision events are immutable'); END""",
        """CREATE TRIGGER workflow_family_decision_event_no_delete
            BEFORE DELETE ON workflow_family_decision_event
            BEGIN SELECT RAISE(ABORT,'workflow family decision events are immutable'); END""",
    )


def _checksum() -> str:
    return hashlib.sha256(f"{SCHEMA_VERSION}\n{';'.join(_schema_sql())}".encode("utf-8")).hexdigest()


class WorkflowDecisionError(RuntimeError):
    """Base error for profile workflow-family decisions."""


class WorkflowDecisionSchemaError(WorkflowDecisionError):
    """Decision or required workflow-identity schema is absent or damaged."""


class WorkflowDecisionScopeError(WorkflowDecisionError):
    """An input identity belongs to a different library/profile scope."""


class WorkflowDecisionNotFound(WorkflowDecisionError):
    """A required profile, capture, or target family does not exist."""


class WorkflowDecisionConflict(WorkflowDecisionError):
    """The caller's expected decision version is stale."""

    def __init__(self, expected: int, current: int):
        super().__init__(f"Workflow decision version conflict: expected {expected}, current {current}")
        self.expected = expected
        self.current = current


class WorkflowDecisionTransitionError(WorkflowDecisionError):
    """A revoke has no active human confirmation to revoke."""


class WorkflowDecisions:
    """Store profile decisions independently from immutable capture ownership."""

    def __init__(self, database: str | Path):
        self.archive = WorkflowArchive(database)

    @staticmethod
    def _identity_ready(connection: sqlite3.Connection) -> None:
        try:
            WorkflowIdentityStore._validate(connection)
        except WorkflowIdentityStoreError as error:
            raise WorkflowDecisionSchemaError(
                "Workflow identity extension must be initialized and valid before family decisions"
            ) from error
        matches = []
        for index in connection.execute("PRAGMA index_list('workflow_capture')"):
            if index["name"] == "workflow_capture_identity_scope_idx" and index["unique"]:
                matches = [row["name"] for row in connection.execute(
                    "PRAGMA index_info('workflow_capture_identity_scope_idx')")]
                break
        if matches != ["library_id", "id", "workflow_id"]:
            raise WorkflowDecisionSchemaError("Workflow capture identity scope index is invalid")

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        migration = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_family_decision_schema_migrations'"
        ).fetchone()
        if not migration:
            raise WorkflowDecisionSchemaError("Workflow family decisions are not initialized")
        rows = connection.execute(
            "SELECT version,checksum FROM workflow_family_decision_schema_migrations ORDER BY version"
        ).fetchall()
        if [(row["version"], row["checksum"]) for row in rows] != [(SCHEMA_VERSION, _checksum())]:
            raise WorkflowDecisionSchemaError("Workflow family decision schema checksum/version mismatch")
        expected_objects = {
            ("table", "workflow_family_decision_event"),
            ("index", "workflow_family_decision_event_history_idx"),
            ("trigger", "workflow_family_decision_event_no_update"),
            ("trigger", "workflow_family_decision_event_no_delete"),
        }
        actual_objects = {(row["type"], row["name"]) for row in connection.execute(
            "SELECT type,name FROM sqlite_master WHERE name LIKE 'workflow_family_decision_event%'")}
        if not expected_objects.issubset(actual_objects):
            raise WorkflowDecisionSchemaError("Workflow family decision schema object is missing")
        columns = [row["name"] for row in connection.execute(
            "PRAGMA table_info('workflow_family_decision_event')")]
        expected_columns = ["id", "uuid", "library_id", "profile_id", "capture_id", "base_workflow_id",
                            "target_workflow_id", "decision_version", "event_kind", "reason", "created_at"]
        if columns != expected_columns:
            raise WorkflowDecisionSchemaError("Workflow family decision event table columns are invalid")
        foreign_keys = {(row["table"], row["from"], row["to"])
                        for row in connection.execute("PRAGMA foreign_key_list('workflow_family_decision_event')")}
        required_fks = {
            ("local_profile", "library_id", "library_id"), ("local_profile", "profile_id", "id"),
            ("workflow_capture", "library_id", "library_id"), ("workflow_capture", "capture_id", "id"),
            ("workflow_capture", "base_workflow_id", "workflow_id"),
            ("workflow_family", "library_id", "library_id"),
            ("workflow_family", "target_workflow_id", "id"),
        }
        if not required_fks.issubset(foreign_keys):
            raise WorkflowDecisionSchemaError("Workflow family decision scope foreign keys are invalid")

    def initialize(self) -> int:
        """Create the additive decision extension after workflow identity setup."""
        with self.archive._transaction(write=True) as connection:
            self._identity_ready(connection)
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_family_decision_schema_migrations'"
            ).fetchone()
            objects = connection.execute(
                "SELECT type,name FROM sqlite_master WHERE name LIKE 'workflow_family_decision_%'"
            ).fetchall()
            if present:
                self._validate(connection)
                return SCHEMA_VERSION
            if objects:
                raise WorkflowDecisionSchemaError("Partial workflow family decision schema exists")
            connection.execute(
                "CREATE TABLE workflow_family_decision_schema_migrations (version INTEGER PRIMARY KEY, "
                "checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
            )
            for statement in _schema_sql():
                connection.execute(statement)
            connection.execute(
                "INSERT INTO workflow_family_decision_schema_migrations(version,checksum) VALUES (?,?)",
                (SCHEMA_VERSION, _checksum()),
            )
            self._validate(connection)
        return SCHEMA_VERSION

    @staticmethod
    def _expected_version(value: int) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("expected_decision_version must be a non-negative integer")
        return value

    @staticmethod
    def _reason(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 1000:
            raise ValueError("reason must contain 1 through 1000 non-whitespace characters")
        return value.strip()

    @staticmethod
    def _profile(connection: sqlite3.Connection, profile_uuid: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id,uuid,library_id FROM local_profile WHERE uuid=?", (profile_uuid,)
        ).fetchone()
        if row is None:
            raise WorkflowDecisionNotFound("Unknown profile UUID")
        return row

    @staticmethod
    def _capture(connection: sqlite3.Connection, library_id: int, capture_uuid: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT c.id,c.uuid,c.library_id,c.workflow_id,f.uuid AS base_workflow_uuid,f.name AS base_name "
            "FROM workflow_capture c JOIN workflow_family f ON f.id=c.workflow_id AND f.library_id=c.library_id "
            "WHERE c.library_id=? AND c.uuid=?", (library_id, capture_uuid)
        ).fetchone()
        if row is None:
            other = connection.execute("SELECT 1 FROM workflow_capture WHERE uuid=?", (capture_uuid,)).fetchone()
            if other:
                raise WorkflowDecisionScopeError("Capture belongs to a different library than the profile")
            raise WorkflowDecisionNotFound("Unknown capture UUID")
        return row

    @staticmethod
    def _family(connection: sqlite3.Connection, library_id: int, workflow_uuid: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id,uuid,name FROM workflow_family WHERE library_id=? AND uuid=?",
            (library_id, workflow_uuid),
        ).fetchone()
        if row is None:
            other = connection.execute("SELECT 1 FROM workflow_family WHERE uuid=?", (workflow_uuid,)).fetchone()
            if other:
                raise WorkflowDecisionScopeError("Target family belongs to a different library")
            raise WorkflowDecisionNotFound("Unknown target workflow family UUID")
        return row

    @staticmethod
    def _version(connection: sqlite3.Connection, library_id: int,
                 profile_id: int, capture_id: int) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(decision_version),0) AS current FROM workflow_family_decision_event "
            "WHERE library_id=? AND profile_id=? AND capture_id=?",
            (library_id, profile_id, capture_id),
        ).fetchone()
        return int(row["current"])

    def _append(self, profile_uuid: str, capture_uuid: str, *, event_kind: str,
                target_workflow_uuid: str | None, expected_decision_version: int,
                reason: str) -> dict[str, Any]:
        expected = self._expected_version(expected_decision_version)
        clean_reason = self._reason(reason)
        with self.archive._transaction(write=True) as connection:
            self._validate(connection)
            profile = self._profile(connection, profile_uuid)
            capture = self._capture(connection, profile["library_id"], capture_uuid)
            current = self._version(connection, profile["library_id"], profile["id"], capture["id"])
            if current != expected:
                raise WorkflowDecisionConflict(expected, current)
            target = self._family(connection, profile["library_id"], target_workflow_uuid) \
                if target_workflow_uuid is not None else None
            if event_kind == "revoke":
                latest = connection.execute(
                    "SELECT event_kind FROM workflow_family_decision_event WHERE library_id=? AND profile_id=? "
                    "AND capture_id=? ORDER BY decision_version DESC LIMIT 1",
                    (profile["library_id"], profile["id"], capture["id"]),
                ).fetchone()
                if latest is None or latest["event_kind"] != "confirm":
                    raise WorkflowDecisionTransitionError("No active human workflow-family confirmation to revoke")
            next_version = current + 1
            event_uuid = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO workflow_family_decision_event(uuid,library_id,profile_id,capture_id,base_workflow_id,"
                "target_workflow_id,decision_version,event_kind,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                (event_uuid, profile["library_id"], profile["id"], capture["id"], capture["workflow_id"],
                 target["id"] if target else None, next_version, event_kind, clean_reason),
            )
            return self._read(connection, profile["id"], capture["id"])

    def confirm(self, profile_uuid: str, capture_uuid: str, target_workflow_uuid: str, *,
                expected_decision_version: int, reason: str) -> dict[str, Any]:
        """Append a profile-specific human family override using optimistic versioning."""
        if not isinstance(target_workflow_uuid, str) or not target_workflow_uuid:
            raise ValueError("target_workflow_uuid must be non-empty text")
        return self._append(profile_uuid, capture_uuid, event_kind="confirm",
                            target_workflow_uuid=target_workflow_uuid,
                            expected_decision_version=expected_decision_version, reason=reason)

    def revoke(self, profile_uuid: str, capture_uuid: str, *,
               expected_decision_version: int, reason: str) -> dict[str, Any]:
        """Append a revocation event; prior confirmations remain in history."""
        return self._append(profile_uuid, capture_uuid, event_kind="revoke", target_workflow_uuid=None,
                            expected_decision_version=expected_decision_version, reason=reason)

    @staticmethod
    def _read(connection: sqlite3.Connection, profile_id: int, capture_id: int) -> dict[str, Any]:
        capture = connection.execute(
            "SELECT c.uuid AS capture_uuid,c.library_id,c.workflow_id,f.uuid AS family_uuid,f.name AS family_name "
            "FROM workflow_capture c JOIN workflow_family f ON f.id=c.workflow_id AND f.library_id=c.library_id "
            "WHERE c.id=?", (capture_id,)
        ).fetchone()
        latest = connection.execute(
            "SELECT e.uuid,e.decision_version,e.event_kind,e.reason,e.created_at,f.uuid AS target_uuid,f.name AS target_name "
            "FROM workflow_family_decision_event e LEFT JOIN workflow_family f "
            "ON f.id=e.target_workflow_id AND f.library_id=e.library_id "
            "WHERE e.profile_id=? AND e.capture_id=? ORDER BY e.decision_version DESC LIMIT 1",
            (profile_id, capture_id),
        ).fetchone()
        base = {"workflowUuid": capture["family_uuid"], "name": capture["family_name"]}
        override = None
        effective = base
        revoked = False
        version = 0
        if latest:
            version = int(latest["decision_version"])
            revoked = latest["event_kind"] == "revoke"
            target = ({"workflowUuid": latest["target_uuid"], "name": latest["target_name"]}
                      if latest["target_uuid"] else None)
            override = {"status": "revoked" if revoked else "confirmed",
                        "targetFamily": target, "reason": latest["reason"],
                        "decisionVersion": version, "eventUuid": latest["uuid"],
                        "createdAt": latest["created_at"]}
            if target and not revoked:
                effective = target
        return {"captureUuid": capture["capture_uuid"], "baseFamily": base,
                "importedFamily": {**base, "relationship": "capture_original_assignment"},
                "humanOverride": override, "effectiveFamily": effective,
                "decisionVersion": version, "revoked": revoked,
                "executionRelationship": "not_asserted"}

    def read(self, profile_uuid: str, capture_uuid: str) -> dict[str, Any]:
        """Read original ownership and the profile's effective human decision."""
        with self.archive._transaction(write=False) as connection:
            self._validate(connection)
            profile = self._profile(connection, profile_uuid)
            capture = self._capture(connection, profile["library_id"], capture_uuid)
            return self._read(connection, profile["id"], capture["id"])

    def history(self, profile_uuid: str, capture_uuid: str, *, after_id: int = 0,
                limit: int = 50) -> dict[str, Any]:
        """Page immutable confirmation/revocation history by event row id."""
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= PAGE_SIZE_MAX:
            raise ValueError("Invalid workflow decision history page")
        with self.archive._transaction(write=False) as connection:
            self._validate(connection)
            profile = self._profile(connection, profile_uuid)
            capture = self._capture(connection, profile["library_id"], capture_uuid)
            rows = connection.execute(
                "SELECT e.id,e.uuid,e.decision_version,e.event_kind,e.reason,e.created_at,"
                "f.uuid AS target_uuid,f.name AS target_name FROM workflow_family_decision_event e "
                "LEFT JOIN workflow_family f ON f.id=e.target_workflow_id AND f.library_id=e.library_id "
                "WHERE e.library_id=? AND e.profile_id=? AND e.capture_id=? AND e.id>? ORDER BY e.id LIMIT ?",
                (profile["library_id"], profile["id"], capture["id"], after_id, limit + 1),
            ).fetchall()
            items = [{"eventUuid": row["uuid"], "decisionVersion": int(row["decision_version"]),
                      "event": row["event_kind"], "targetFamily": ({"workflowUuid": row["target_uuid"],
                          "name": row["target_name"]} if row["target_uuid"] else None),
                      "reason": row["reason"], "createdAt": row["created_at"], "eventId": row["id"]}
                     for row in rows[:limit]]
            return {"items": items, "nextAfterId": rows[limit - 1]["id"] if len(rows) > limit else None,
                    "executionRelationship": "not_asserted"}


__all__ = ["WorkflowDecisions", "WorkflowDecisionError", "WorkflowDecisionSchemaError",
           "WorkflowDecisionScopeError", "WorkflowDecisionNotFound", "WorkflowDecisionConflict",
           "WorkflowDecisionTransitionError"]
