"""Application boundary between verification jobs and model identity storage."""
from pathlib import Path
import json

from .model_identity_store import ModelIdentityStore, ModelIdentityConflictError, reference_key
from .model_paths import resolve_model_path
from .model_verification_jobs import ModelVerificationJobs


class ModelService:
    def __init__(self, database, library_uuid, profile_uuid):
        self.library_uuid = library_uuid
        self.profile_uuid = profile_uuid
        self.store = ModelIdentityStore(database)
        self.jobs = ModelVerificationJobs(database)

    def initialize(self):
        self.store.initialize()
        self.jobs.initialize()

    def roots(self):
        with self.store._transaction(write=False) as connection:
            library = self.store._library(connection, self.library_uuid)
            return [dict(row) for row in connection.execute(
                "SELECT uuid AS rootUuid,name,path FROM model_root WHERE library_id=? ORDER BY id",
                (library,))]

    def register_root(self, path, name="Models"):
        return self.store.register_model_root(self.library_uuid, path, name)

    def references(self, media_uuid, revision_uuid):
        with self.store._transaction(write=False) as connection:
            library = self.store._library(connection, self.library_uuid)
            row = connection.execute(
                "SELECT run.result_json FROM generation_asset_current cur "
                "JOIN media_asset a ON a.id=cur.asset_id AND a.library_id=cur.library_id "
                "JOIN media_revision rev ON rev.id=cur.revision_id AND rev.asset_id=a.id AND rev.library_id=cur.library_id "
                "JOIN generation_parse_run run ON run.id=cur.run_id AND run.library_id=cur.library_id "
                "AND run.asset_id=cur.asset_id AND run.revision_id=cur.revision_id "
                "WHERE cur.library_id=? AND a.uuid=? AND rev.uuid=? "
                "AND a.current_revision_id=rev.id AND run.status='successful'",
                (library, media_uuid, revision_uuid)).fetchone()
            if row is None:
                current = connection.execute(
                    "SELECT 1 FROM media_asset a JOIN media_revision r ON r.id=a.current_revision_id "
                    "AND r.asset_id=a.id AND r.library_id=a.library_id "
                    "WHERE a.library_id=? AND a.uuid=? AND r.uuid=?",
                    (library, media_uuid, revision_uuid)).fetchone()
                if current is None:
                    raise ValueError("Current media revision is unavailable")
                # A valid ordinary photo or an unsupported prompt can have no
                # successful generation projection and therefore no references.
                return []
            result = json.loads(row["result_json"])
        references = []
        for section in result.get("steps", []) + result.get("outputs", []):
            if not isinstance(section, dict):
                continue
            for reference in section.get("models", []):
                if not isinstance(reference, dict):
                    continue
                try:
                    key = reference_key(self.library_uuid, media_uuid, revision_uuid, reference)
                except ValueError:
                    references.append({"referenceKey": None, "reference": reference,
                        "override": None, "confirmationAvailable": False,
                        "reason": "模型引用缺少稳定的原始来源定位"})
                    continue
                references.append({"referenceKey": key, "reference": reference,
                    "confirmationAvailable": True,
                    "override": self.store.read_reference_override(
                        self.library_uuid, self.profile_uuid, media_uuid, revision_uuid, key)})
        counts = {}
        unique = {}
        for entry in references:
            identity = (entry["referenceKey"], json.dumps(entry["reference"], sort_keys=True))
            unique.setdefault(identity, entry)
        references = list(unique.values())
        for entry in references:
            key = entry["referenceKey"]
            if key is not None:
                counts[key] = counts.get(key, 0) + 1
        for entry in references:
            if counts.get(entry["referenceKey"], 0) > 1:
                entry["confirmationAvailable"] = False
                entry["reason"] = "同一来源定位包含多个模型引用，暂不能单独确认"
        return references

    def _target(self, root_uuid, relative_path):
        root = next((row for row in self.roots() if row["rootUuid"] == root_uuid), None)
        if root is None:
            raise ValueError("Unknown model root in this library")
        return resolve_model_path(root["path"], relative_path)

    def _verify_media(self, media_uuid, revision_uuid):
        from ..storage.server_catalog import ServerCatalog
        with self.store._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT a.path,r.path AS root_path FROM media_asset a "
                "JOIN library l ON l.id=a.library_id "
                "JOIN library_root r ON r.id=a.root_id AND r.library_id=a.library_id "
                "WHERE l.uuid=? AND a.uuid=?", (self.library_uuid, media_uuid)).fetchone()
            if row is None:
                raise ValueError("Unknown media in this library")
        actual = ServerCatalog(self.store.database, self.library_uuid).verify(row["root_path"], row["path"])
        if actual["mediaId"] != media_uuid or actual["revisionId"] != revision_uuid:
            raise ModelIdentityConflictError("Media content changed; reopen before confirming its model")

    def start_verification(self, root_uuid, relative_path, budgets=None):
        target = self._target(root_uuid, relative_path)
        return self.jobs.start(self.library_uuid, target, budgets)

    def verification_status(self, job_uuid):
        return self.jobs.status(self.library_uuid, job_uuid)

    def cancel_verification(self, job_uuid):
        return self.jobs.cancel(self.library_uuid, job_uuid)

    def _receipt(self, root_uuid, relative_path, job_uuid):
        target = self._target(root_uuid, relative_path)
        inspection = self.jobs.read_verified_receipt(self.library_uuid, job_uuid)
        if Path(inspection["path"]).absolute() != target:
            raise ValueError("Completed verification belongs to a different model file")
        return self.store._accept_completed_inspection(
            self.library_uuid, root_uuid, relative_path, inspection,
            source_job_uuid=job_uuid)

    def register_verified_file(self, root_uuid, relative_path, job_uuid, model_type, display_name=None):
        receipt = self._receipt(root_uuid, relative_path, job_uuid)
        return self.store.register_verified_file(
            self.library_uuid, root_uuid, relative_path, receipt, model_type, display_name)

    def confirm_reference(self, media_uuid, revision_uuid, key, model_uuid, *,
                          expected_override_version, root_uuid, relative_path,
                          fresh_job_uuid, location_uuid):
        if not location_uuid:
            raise ValueError("Confirmation requires a verified model location")
        receipt = self._receipt(root_uuid, relative_path, fresh_job_uuid)
        self._verify_media(media_uuid, revision_uuid)
        return self.store.confirm_reference(
            self.library_uuid, self.profile_uuid, media_uuid, revision_uuid, key, model_uuid,
            expected_current_revision_uuid=revision_uuid,
            expected_override_version=expected_override_version,
            location_uuid=location_uuid, fresh_location_receipt_uuid=receipt)

    def revoke_reference(self, media_uuid, revision_uuid, key, expected_override_version):
        self._verify_media(media_uuid, revision_uuid)
        return self.store.revoke_reference(
            self.library_uuid, self.profile_uuid, media_uuid, revision_uuid, key,
            expected_current_revision_uuid=revision_uuid,
            expected_override_version=expected_override_version)
