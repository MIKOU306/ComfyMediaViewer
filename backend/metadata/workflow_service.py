"""Library-scoped workflow reads, retaining original capture bytes separately."""
import base64
import hashlib
import uuid
import json
from contextlib import nullcontext

from .workflow import WorkflowArchive, WorkflowIdentityError, WorkflowArchiveError, MAX_WORKFLOW_BYTES
from .workflow_identity_store import WorkflowIdentityStore


class WorkflowService:
    def __init__(self, database, library_uuid):
        self.archive = WorkflowArchive(database)
        self.library_uuid = library_uuid
        self.identities = WorkflowIdentityStore(database)

    def initialize(self):
        self.archive.initialize()
        self.identities.initialize()

    def initialize_sources(self):
        """Explicit extension setup after the metadata archive is initialized."""
        from .workflow_source_store import WorkflowSourceStore
        return WorkflowSourceStore(self.archive.database).initialize()

    def initialize_source_bindings(self):
        from .workflow_source_bindings import WorkflowSourceBindings
        return WorkflowSourceBindings(self.archive.database).initialize()

    def source_bindings(self, capture_uuid, *, after_id=0, limit=50):
        from .workflow_source_bindings import WorkflowSourceBindings
        result = WorkflowSourceBindings(self.archive.database).list_for_capture(
            self.library_uuid, capture_uuid, after_id=after_id, limit=limit)
        return {"items": result["items"], "nextAfterId": result["next_after_id"],
                "textIncluded": False, "evidenceKind": "embedded_metadata_claim"}

    def import_source(self, source_uuid, *, workflow_uuid, name):
        from .workflow_source_store import WorkflowSourceStore, WorkflowSourceStoreConflict
        from .workflow_source_bindings import WorkflowSourceBindings
        if not isinstance(workflow_uuid, str) or not workflow_uuid.strip() or not isinstance(name, str) or not name.strip():
            raise ValueError("Explicit workflow family identity and name are required")
        source = WorkflowSourceStore(self.archive.database).read_source(self.library_uuid, source_uuid)
        record = source["record"]
        if record.get("status") != "available" or not isinstance(record.get("text"), str):
            raise ValueError("This source has no verified text available for import")
        with self.archive._transaction(write=True) as connection:
            result = self.capture(record["text"].encode("utf-8"), workflow_uuid=workflow_uuid,
                                  name=name, _connection=connection)
            binding = WorkflowSourceBindings(self.archive.database).bind(
                self.library_uuid, source_uuid, result["capture_uuid"], _connection=connection)
            if binding["source_record_sha256"] != source["record_sha256"]:
                raise WorkflowSourceStoreConflict("Source evidence changed during import")
            return {**result, "sourceBinding": binding}

    def publish_sources(self, snapshot_uuid):
        from .workflow_source_store import WorkflowSourceStore
        return WorkflowSourceStore(self.archive.database).publish_snapshot(self.library_uuid, snapshot_uuid)

    def sources(self, snapshot_uuid, *, after_id=0, limit=50):
        from .workflow_source_store import WorkflowSourceStore
        result = WorkflowSourceStore(self.archive.database).list_sources(
            self.library_uuid, snapshot_uuid, after_id=after_id, limit=limit)
        return {"items": result["items"], "nextAfterId": result["next_after_id"],
                "summary": result["summary"], "textIncluded": False,
                "evidenceKind": "embedded_metadata_claim"}

    def read_source(self, source_uuid):
        from .workflow_source_store import WorkflowSourceStore
        result = WorkflowSourceStore(self.archive.database).read_source(self.library_uuid, source_uuid)
        return {"sourceUuid": result["uuid"], "snapshotUuid": result["snapshot_uuid"],
                "mediaUuid": result["asset_uuid"], "revisionUuid": result["revision_uuid"],
                "mediaPath": result["media_path"],
                "extractorVersion": result["extractor_version"], "record": result["record"],
                "evidenceKind": "embedded_metadata_claim", "executionEvidence": "not_proven"}

    def definitions(self, workflow_uuid, *, after_id=0, limit=50):
        return self.identities.list_definitions(self.library_uuid, workflow_uuid,
                                                after_id=after_id, limit=limit)

    def read_definition(self, definition_uuid):
        return self.identities.read_definition(self.library_uuid, definition_uuid)

    def read_capture_identity(self, capture_uuid):
        return self.identities.read_capture_identity(self.library_uuid, capture_uuid)

    def candidates(self, capture_uuid, *, after_id=0, limit=50):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid workflow candidate page")
        source = self.read_capture_identity(capture_uuid)
        if source["status"] != "comparable":
            return {"items":[], "nextAfterId":None, "coverage":"unsupported"}
        with self.archive._transaction(write=False) as connection:
            library = self.archive._library(connection, self.library_uuid)
            rows = connection.execute(
                "SELECT d.id,d.uuid AS definitionUuid,f.uuid AS workflowUuid,f.name,"
                "CASE WHEN d.definition_hash=? THEN 'definition_equal' ELSE 'structure_candidate' END AS matchKind "
                "FROM workflow_definition_version d JOIN workflow_family f "
                "ON f.id=d.workflow_id AND f.library_id=d.library_id "
                "WHERE d.library_id=? AND d.algorithm=? AND d.adapter_version=? AND d.graph_format=? "
                "AND d.structure_hash=? AND d.id>? AND d.uuid<>? ORDER BY d.id LIMIT ?",
                (source["definition_hash"],library["id"],source["algorithm"],source["adapter_version"],
                 source["graph_format"],source["structure_hash"],after_id,source["definition_uuid"],limit+1)).fetchall()
            return {"items":[dict(row) for row in rows[:limit]],
                    "nextAfterId":rows[limit-1]["id"] if len(rows)>limit else None,
                    "coverage":"indexed_captures_only", "sourceIdentityCoverage":source["coverage"],
                    "familyMembership":"not_confirmed"}

    def families(self, *, after_id=0, limit=50):
        return self._page("family", None, after_id, limit)

    def versions(self, workflow_uuid, *, after_id=0, limit=50):
        return self._page("version", workflow_uuid, after_id, limit)

    def _page(self, kind, workflow_uuid, after_id, limit):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid workflow page")
        with self.archive._transaction(write=False) as connection:
            library = self.archive._library(connection, self.library_uuid)
            if kind == "family":
                rows = connection.execute(
                    "SELECT id,uuid AS workflow_uuid,name FROM workflow_family "
                    "WHERE library_id=? AND id>? ORDER BY id LIMIT ?",
                    (library["id"], after_id, limit+1)).fetchall()
            else:
                family = connection.execute("SELECT id FROM workflow_family WHERE library_id=? AND uuid=?",
                                            (library["id"], workflow_uuid)).fetchone()
                if family is None:
                    raise WorkflowIdentityError("Unknown workflow family in selected library")
                rows = connection.execute(
                    "SELECT id,uuid AS version_uuid,version_no,source_format,canonical_version,normalized_hash "
                    "FROM workflow_version WHERE library_id=? AND workflow_id=? AND id>? ORDER BY id LIMIT ?",
                    (library["id"], family["id"], after_id, limit+1)).fetchall()
            return {"items":[dict(row) for row in rows[:limit]],
                    "versionSemantics":"saved_graph_snapshot" if kind == "version" else None,
                    "nextAfterId":rows[limit-1]["id"] if len(rows)>limit else None}

    def read_version(self, version_uuid):
        result = self.archive.read_version(version_uuid, library_uuid=self.library_uuid,
                                           include_captures=False)
        identity = json.dumps({"canonical_version":result["canonical_version"],
                               "source_format":result["source_format"], "graph":result["graph"]},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if hashlib.sha256(identity.encode("utf-8")).hexdigest() != result["normalized_hash"]:
            raise WorkflowArchiveError("Stored workflow graph checksum mismatch")
        return {**result, "versionSemantics":"saved_graph_snapshot",
                "executionEvidence":"not_proven", "captures": self.captures(version_uuid)}

    def captures(self, version_uuid, *, after_id=0, limit=50):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid workflow capture page")
        with self.archive._transaction(write=False) as connection:
            library = self.archive._library(connection, self.library_uuid)
            version = connection.execute("SELECT id FROM workflow_version WHERE library_id=? AND uuid=?",
                                         (library["id"], version_uuid)).fetchone()
            if version is None:
                raise WorkflowIdentityError("Unknown workflow version in selected library")
            rows = connection.execute(
                "SELECT c.id,c.uuid AS capture_uuid,c.collector_version,c.raw_sha256,"
                "length(c.raw_payload) AS size_bytes,c.captured_at,"
                "EXISTS(SELECT 1 FROM workflow_capture_identity ci WHERE ci.capture_id=c.id "
                "AND ci.library_id=c.library_id) AS identity_available FROM workflow_capture c "
                "WHERE c.library_id=? AND c.version_id=? AND c.id>? ORDER BY c.id LIMIT ?",
                (library["id"], version["id"], after_id, limit+1)).fetchall()
            return {"items": [dict(row) for row in rows[:limit]],
                    "nextAfterId": rows[limit-1]["id"] if len(rows)>limit else None}

    def export_capture(self, version_uuid, capture_uuid):
        with self.archive._transaction(write=False) as connection:
            library = self.archive._library(connection, self.library_uuid)
            row = connection.execute(
                "SELECT c.raw_payload,c.raw_sha256 FROM workflow_capture c "
                "JOIN workflow_version v ON v.id=c.version_id AND v.library_id=c.library_id "
                "WHERE c.library_id=? AND v.uuid=? AND c.uuid=? AND length(c.raw_payload)<=?",
                (library["id"], version_uuid, capture_uuid, MAX_WORKFLOW_BYTES)).fetchone()
            if row is None:
                raise WorkflowIdentityError("Unknown or over-budget workflow capture")
            raw = bytes(row["raw_payload"])
            if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
                raise WorkflowArchiveError("Stored workflow capture checksum mismatch")
            return {"raw_payload":base64.b64encode(raw).decode("ascii"),
                    "raw_encoding":"base64", "raw_sha256":row["raw_sha256"]}

    def capture(self, raw, *, workflow_uuid=None, name=None, source_format=None, _connection=None):
        # A matching graph is a candidate, not evidence that two author families
        # are the same. An explicit import creates its own family unless selected.
        family = workflow_uuid or str(uuid.uuid4())
        transaction = self.archive._transaction(write=True) if _connection is None else nullcontext(_connection)
        with transaction as connection:
            result = self.archive.capture_workflow(self.library_uuid, raw, workflow_uuid=family,
                                                  name=name, source_format=source_format,
                                                  reuse_exact_capture=True, _connection=connection)
            identity = self.identities.publish_capture(self.library_uuid, result["capture_uuid"],
                                                       _connection=connection)
        return {**result, "identity": identity}
