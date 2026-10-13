"""Library-scoped model artifacts and profile-scoped human identity overrides.

Verified file hashes enter this store only through the backend's private completed-
inspection receipt path. Overrides describe identity mapping, never execution use.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from ..storage.database import initialize_schema
from .archive import MetadataArchive
from .generation_store import GenerationStore

MODEL_IDENTITY_SCHEMA_VERSION = 1
_SQL = """
CREATE TABLE model_identity_schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,
 applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE model_root(id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, library_id INTEGER NOT NULL,
 name TEXT NOT NULL, path TEXT NOT NULL, path_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT
 (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(library_id,id), UNIQUE(library_id,path_key),
 FOREIGN KEY(library_id) REFERENCES library(id) ON DELETE RESTRICT);
CREATE TABLE model_artifact(id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, library_id INTEGER NOT NULL,
 model_type TEXT NOT NULL, display_name TEXT, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 size_bytes INTEGER NOT NULL CHECK(size_bytes>0), identity_state TEXT NOT NULL CHECK(identity_state='file_hash_verified'),
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(library_id,id),
 UNIQUE(library_id,sha256), FOREIGN KEY(library_id) REFERENCES library(id) ON DELETE RESTRICT);
CREATE TABLE model_inspection_receipt(id INTEGER PRIMARY KEY, receipt_uuid TEXT NOT NULL UNIQUE,
 library_id INTEGER NOT NULL, root_id INTEGER NOT NULL, relative_path TEXT NOT NULL, path_key TEXT NOT NULL,
 sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, observation_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
 source_job_uuid TEXT NOT NULL,
 consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)), created_at TEXT NOT NULL DEFAULT
 (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(library_id,id),
 UNIQUE(library_id,source_job_uuid),
 FOREIGN KEY(library_id,root_id) REFERENCES model_root(library_id,id) ON DELETE RESTRICT);
CREATE TABLE model_location(id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, library_id INTEGER NOT NULL,
 model_id INTEGER NOT NULL, root_id INTEGER NOT NULL, relative_path TEXT NOT NULL, path_key TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, observed_sha256 TEXT NOT NULL, identity_state TEXT NOT NULL CHECK(identity_state='file_hash_verified'),
 availability TEXT NOT NULL CHECK(availability IN ('present','replaced','missing','offline')),
 first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 last_verified_at TEXT NOT NULL, valid_to TEXT, observation_json TEXT NOT NULL,
 UNIQUE(library_id,id,model_id), FOREIGN KEY(library_id,model_id) REFERENCES model_artifact(library_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(library_id,root_id) REFERENCES model_root(library_id,id) ON DELETE RESTRICT);
CREATE UNIQUE INDEX model_location_one_present_path ON model_location(root_id,path_key) WHERE availability='present';
CREATE TABLE model_reference_override(id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, library_id INTEGER NOT NULL,
 profile_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, revision_id INTEGER NOT NULL, reference_key TEXT NOT NULL,
 reference_json TEXT NOT NULL, reference_kind TEXT NOT NULL, reference_name_raw TEXT,
 status TEXT NOT NULL CHECK(status IN ('confirmed','revoked')), model_id INTEGER, location_id INTEGER,
 version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(library_id,profile_id,asset_id,revision_id,reference_key),
 FOREIGN KEY(library_id,profile_id) REFERENCES local_profile(library_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(library_id,asset_id,revision_id) REFERENCES media_revision(library_id,asset_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(library_id,model_id) REFERENCES model_artifact(library_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(library_id,location_id,model_id) REFERENCES model_location(library_id,id,model_id) ON DELETE RESTRICT,
 CHECK((status='confirmed' AND model_id IS NOT NULL) OR (status='revoked' AND model_id IS NULL AND location_id IS NULL)));
CREATE TABLE model_reference_override_event(id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
 library_id INTEGER NOT NULL, override_id INTEGER NOT NULL, version INTEGER NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('confirm','revoke')),
 model_id INTEGER, location_id INTEGER, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(override_id,version), FOREIGN KEY(override_id) REFERENCES model_reference_override(id) ON DELETE RESTRICT,
 FOREIGN KEY(library_id,model_id) REFERENCES model_artifact(library_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(library_id,location_id,model_id) REFERENCES model_location(library_id,id,model_id) ON DELETE RESTRICT);
"""


def _checksum() -> str:
    return hashlib.sha256(f"{MODEL_IDENTITY_SCHEMA_VERSION}\n{_SQL}".encode()).hexdigest()


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def reference_key(library_uuid: str, media_uuid: str, revision_uuid: str, reference: dict) -> str:
    """Stable key for an exact source reference; excludes parser/run/database IDs."""
    if not isinstance(reference, dict):
        raise ValueError("reference must be an object")
    src = reference.get("source")
    if not isinstance(src, dict):
        src = {"kind": "comfy_graph", "nodeId": reference.get("nodeId")}
    allowed = {k: src[k] for k in ("kind", "nodeId", "side", "rawToken", "characterOffset",
                                    "characterLength", "occurrence", "offset", "path", "field",
                                    "container", "origin") if k in src}
    param_source = src.get("parameterSource")
    if param_source is not None:
        stable_keys = {"kind", "format", "field", "offset", "length", "chunk", "boxPath",
                       "box_path", "path", "container", "metadataOffset", "declaredLength",
                       "sourceField", "source"}
        def stable(value):
            if isinstance(value, dict):
                return {k: stable(v) for k, v in value.items() if k in stable_keys}
            if isinstance(value, (list, tuple)):
                return [stable(v) for v in value]
            return value if isinstance(value, (str, int, float, bool)) or value is None else None
        allowed["parameterSource"] = stable(param_source)
    if not any(k in allowed for k in ("nodeId", "rawToken", "offset", "path")):
        raise ValueError("reference source lacks a stable locator")
    family = reference.get("referenceFamily", reference.get("kind", "unknown"))
    payload = {"library": str(library_uuid), "media": str(media_uuid), "revision": str(revision_uuid),
               "family": family, "kind": reference.get("kind"), "name": reference.get("name"),
               "source": allowed}
    return "model-ref-v1:" + hashlib.sha256(b"model-reference-v1\0" + _canon(payload).encode()).hexdigest()


class ModelIdentityStoreError(RuntimeError):
    pass


class ModelIdentityConflictError(ValueError):
    """The caller's file, media revision or mapping version is no longer current."""
    pass


class ModelIdentityStore:
    def __init__(self, database):
        self.database = Path(database).absolute()

    @contextmanager
    def _transaction(self, write=True):
        if self.database.is_symlink() or not self.database.exists():
            raise ModelIdentityStoreError("Catalog database must exist and must not be a symlink")
        connection = sqlite3.connect(self.database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            initialize_schema(connection)
            MetadataArchive._validate_archive_schema(connection)
            GenerationStore._validate_schema(connection)
            self._validate_schema(connection)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_schema(c):
        rows = c.execute("SELECT version,checksum FROM model_identity_schema_migrations ORDER BY version").fetchall()
        if len(rows) != 1 or rows[0]["version"] != MODEL_IDENTITY_SCHEMA_VERSION or rows[0]["checksum"] != _checksum():
            raise ModelIdentityStoreError("Model identity schema migration checksum/version mismatch")

    def initialize(self):
        # All prerequisites must already be initialized; never silently create their schemas.
        with self._bare_transaction() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_schema_migrations'").fetchone() is None:
                raise ModelIdentityStoreError("Generation store must be initialized first")
            MetadataArchive._validate_archive_schema(c)
            GenerationStore._validate_schema(c)
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_identity_schema_migrations'").fetchone():
                self._validate_schema(c)
                return MODEL_IDENTITY_SCHEMA_VERSION
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'model_%'").fetchone():
                raise ModelIdentityStoreError("Partial model identity schema exists")
            c.executescript("BEGIN IMMEDIATE;\n" + _SQL +
                            f"\nINSERT INTO model_identity_schema_migrations(version,checksum) VALUES(1,'{_checksum()}');\nCOMMIT;")
        return MODEL_IDENTITY_SCHEMA_VERSION

    @contextmanager
    def _bare_transaction(self):
        if self.database.is_symlink() or not self.database.exists():
            raise ModelIdentityStoreError("Catalog database must exist and must not be a symlink")
        c = sqlite3.connect(self.database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _library(c, library_uuid):
        row = c.execute("SELECT id FROM library WHERE uuid=?", (str(library_uuid),)).fetchone()
        if not row: raise ValueError("Unknown library")
        return row["id"]

    def register_model_root(self, library_uuid, path, name="Models"):
        target = Path(path).expanduser().absolute()
        if target.is_symlink() or not target.is_dir(): raise ValueError("Model root must be an existing directory")
        path_s = str(target.resolve(strict=True)); key = os.path.normcase(path_s)
        with self._transaction() as c:
            lib = self._library(c, library_uuid)
            row = c.execute("SELECT uuid FROM model_root WHERE library_id=? AND path_key=?", (lib,key)).fetchone()
            if row: return row["uuid"]
            uid = str(uuid.uuid4())
            c.execute("INSERT INTO model_root(uuid,library_id,name,path,path_key) VALUES(?,?,?,?,?)", (uid,lib,str(name),path_s,key))
            return uid

    def _accept_completed_inspection(self, library_uuid, root_uuid, relative_path, inspection, *, source_job_uuid):
        """Private service-only ingestion for a server-held, completed verification job result."""
        if not isinstance(inspection, dict) or inspection.get("identityState") != "file_hash_verified":
            raise ValueError("Only verified backend inspection results are accepted")
        digest = inspection.get("sha256"); size = inspection.get("sizeBytes"); obs = inspection.get("observation")
        if not isinstance(digest,str) or not re.fullmatch(r"[0-9a-f]{64}",digest) or type(size) is not int or size < 1:
            raise ValueError("Invalid verified inspection result")
        if inspection.get("hashPasses") != 2 or inspection.get("bytesRead") != size*2 or not isinstance(obs,dict):
            raise ValueError("Inspection is missing complete verification evidence")
        try:
            source_job_uuid = str(uuid.UUID(str(source_job_uuid)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("A trusted completed job UUID is required") from error
        rel = Path(relative_path)
        if rel.is_absolute() or ".." in rel.parts: raise ValueError("Path must be relative to model root")
        with self._transaction() as c:
            lib = self._library(c,library_uuid)
            root = c.execute("SELECT id,path FROM model_root WHERE library_id=? AND uuid=?",(lib,str(root_uuid))).fetchone()
            if not root: raise ValueError("Unknown model root")
            target=self._safe_model_path(root["path"], rel)
            reported = inspection.get("path")
            if not isinstance(reported, str) or Path(reported).resolve(strict=True) != target:
                raise ValueError("Inspection receipt belongs to a different path")
            prior = c.execute("SELECT receipt_uuid,sha256,size_bytes,observation_json,relative_path,root_id FROM model_inspection_receipt WHERE library_id=? AND source_job_uuid=?",(lib,source_job_uuid)).fetchone()
            if prior:
                if (prior["sha256"] != digest or prior["size_bytes"] != size or prior["observation_json"] != _canon(obs)
                        or prior["relative_path"] != str(rel).replace("\\","/") or prior["root_id"] != root["id"]):
                    raise ValueError("Completed job UUID was already accepted with different evidence")
                return prior["receipt_uuid"]
            st=target.stat()
            actual={"mtimeNs":st.st_mtime_ns,"ctimeNs":st.st_ctime_ns,"device":st.st_dev,"inode":st.st_ino}
            if st.st_size != size or actual != obs: raise ModelIdentityConflictError("File changed since verification receipt")
            rid=str(uuid.uuid4()); path_key=os.path.normcase(str(rel).replace("\\","/"))
            c.execute("INSERT INTO model_inspection_receipt(receipt_uuid,library_id,root_id,relative_path,path_key,sha256,size_bytes,observation_json,evidence_json,source_job_uuid) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (rid,lib,root["id"],str(rel).replace("\\","/"),path_key,digest,size,_canon(actual),_canon({k:inspection[k] for k in ("hashPasses","bytesRead","verifiedAt")}),source_job_uuid))
            return rid

    def register_verified_file(self, library_uuid, root_uuid, relative_path, receipt_uuid, model_type, display_name=None):
        rel=Path(relative_path)
        if rel.is_absolute() or ".." in rel.parts: raise ValueError("Path must be relative to model root")
        key=os.path.normcase(str(rel).replace("\\","/"))
        with self._transaction() as c:
            lib=self._library(c,library_uuid)
            rec=c.execute("SELECT r.*,m.uuid AS root_uuid,m.path FROM model_inspection_receipt r JOIN model_root m ON m.id=r.root_id WHERE r.library_id=? AND r.receipt_uuid=?",(lib,str(receipt_uuid))).fetchone()
            if not rec or rec["root_uuid"]!=str(root_uuid) or rec["path_key"]!=key or rec["consumed"]: raise ValueError("Invalid, mismatched, or consumed inspection receipt")
            full=self._safe_model_path(rec["path"], rel); st=full.stat()
            actual={"mtimeNs":st.st_mtime_ns,"ctimeNs":st.st_ctime_ns,"device":st.st_dev,"inode":st.st_ino}
            if st.st_size!=rec["size_bytes"] or _canon(actual)!=rec["observation_json"]: raise ModelIdentityConflictError("File changed after verification")
            art=c.execute("SELECT * FROM model_artifact WHERE library_id=? AND sha256=?",(lib,rec["sha256"])).fetchone()
            if art and art["model_type"]!=model_type: raise ValueError("Verified hash already has a different model type")
            if not art:
                c.execute("INSERT INTO model_artifact(uuid,library_id,model_type,display_name,sha256,size_bytes,identity_state) VALUES(?,?,?,?,?,?, 'file_hash_verified')",
                          (str(uuid.uuid4()),lib,str(model_type),display_name,rec["sha256"],rec["size_bytes"]))
                art=c.execute("SELECT * FROM model_artifact WHERE library_id=? AND sha256=?",(lib,rec["sha256"])).fetchone()
            old=c.execute("SELECT * FROM model_location WHERE root_id=? AND path_key=? AND availability='present'",(rec["root_id"],key)).fetchone()
            if old and old["model_id"]!=art["id"]:
                c.execute("UPDATE model_location SET availability='replaced',valid_to=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",(old["id"],))
                old=None
            if old:
                c.execute("UPDATE model_location SET last_verified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),size_bytes=?,observed_sha256=?,observation_json=? WHERE id=?",(rec["size_bytes"],rec["sha256"],rec["observation_json"],old["id"]))
                loc=old["uuid"]
            else:
                loc=str(uuid.uuid4())
                c.execute("INSERT INTO model_location(uuid,library_id,model_id,root_id,relative_path,path_key,size_bytes,observed_sha256,identity_state,availability,last_verified_at,observation_json) VALUES(?,?,?,?,?,?,?,?, 'file_hash_verified','present',strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
                          (loc,lib,art["id"],rec["root_id"],str(rel).replace("\\","/"),key,rec["size_bytes"],rec["sha256"],rec["observation_json"]))
            c.execute("UPDATE model_inspection_receipt SET consumed=1 WHERE id=?",(rec["id"],))
            return {"modelUuid":art["uuid"],"locationUuid":loc,"identityState":"file_hash_verified"}

    def _actual_reference(self,c,lib,profile_uuid,media_uuid,revision_uuid,key):
        row=c.execute("SELECT p.id profile_id,a.id asset_id,mr.id revision_id,mr.uuid revision_uuid,r.result_json "
                       "FROM local_profile p JOIN generation_asset_current cur ON cur.library_id=p.library_id "
                       "JOIN media_asset a ON a.library_id=cur.library_id AND a.id=cur.asset_id "
                       "JOIN media_revision mr ON mr.library_id=cur.library_id AND mr.asset_id=cur.asset_id AND mr.id=cur.revision_id "
                       "JOIN generation_parse_run r ON r.id=cur.run_id AND r.library_id=cur.library_id AND r.asset_id=cur.asset_id AND r.revision_id=cur.revision_id "
                       "WHERE p.library_id=? AND p.uuid=? AND a.uuid=? AND mr.uuid=? AND a.current_revision_id=mr.id AND r.status='successful'",
                       (lib,str(profile_uuid),str(media_uuid),str(revision_uuid))).fetchone()
        if not row: raise ModelIdentityConflictError("Reference must belong to the current revision and successful parse run")
        result=json.loads(row["result_json"]); refs=[]
        for step in result.get("steps",[]): refs.extend(step.get("models",[]) if isinstance(step,dict) else [])
        for output in result.get("outputs",[]): refs.extend(output.get("models",[]) if isinstance(output,dict) else [])
        matches=[ref for ref in refs if isinstance(ref,dict) and reference_key(str(self._uuid_for_library(c,lib)),str(media_uuid),str(revision_uuid),ref)==key]
        matches=list({_canon(ref):ref for ref in matches}.values())
        if len(matches)!=1: raise ValueError("Reference key must identify exactly one current parsed reference")
        return row,matches[0]

    @staticmethod
    def _uuid_for_library(c,lib): return c.execute("SELECT uuid FROM library WHERE id=?",(lib,)).fetchone()[0]

    def confirm_reference(self, library_uuid, profile_uuid, media_uuid, revision_uuid, key, model_uuid, *,
                          expected_current_revision_uuid, expected_override_version, location_uuid=None,
                          fresh_location_receipt_uuid=None):
        if str(revision_uuid)!=str(expected_current_revision_uuid): raise ValueError("Current revision expectation mismatch")
        with self._transaction() as c:
            lib=self._library(c,library_uuid); row,ref=self._actual_reference(c,lib,profile_uuid,media_uuid,revision_uuid,key)
            art=c.execute("SELECT id,uuid FROM model_artifact WHERE library_id=? AND uuid=?",(lib,str(model_uuid))).fetchone()
            if not art: raise ValueError("Unknown model artifact")
            loc_id=None
            if location_uuid:
                loc=c.execute("SELECT * FROM model_location WHERE library_id=? AND uuid=? AND model_id=? AND availability='present'",(lib,str(location_uuid),art["id"])).fetchone()
                rec=c.execute("SELECT * FROM model_inspection_receipt WHERE library_id=? AND receipt_uuid=?",(lib,str(fresh_location_receipt_uuid))).fetchone() if fresh_location_receipt_uuid else None
                if not loc or not rec or rec["sha256"]!=loc["observed_sha256"] or rec["root_id"]!=loc["root_id"] or rec["path_key"]!=loc["path_key"]:
                    raise ValueError("A fresh completed verification receipt for the selected location is required")
                if rec["consumed"]:
                    raise ValueError("Fresh verification receipt has already been consumed")
                root_path=c.execute("SELECT path FROM model_root WHERE id=?",(loc["root_id"],)).fetchone()[0]
                full=self._safe_model_path(root_path, Path(loc["relative_path"]))
                st=full.stat(); actual={"mtimeNs":st.st_mtime_ns,"ctimeNs":st.st_ctime_ns,"device":st.st_dev,"inode":st.st_ino}
                if st.st_size!=rec["size_bytes"] or _canon(actual)!=rec["observation_json"]: raise ModelIdentityConflictError("File changed after fresh verification")
                loc_id=loc["id"]
            existing=c.execute("SELECT * FROM model_reference_override WHERE library_id=? AND profile_id=? AND asset_id=? AND revision_id=? AND reference_key=?",(lib,row["profile_id"],row["asset_id"],row["revision_id"],key)).fetchone()
            version=existing["version"] if existing else 0
            if type(expected_override_version) is not int or expected_override_version!=version: raise ModelIdentityConflictError("Override version conflict")
            new_version=version+1
            now="strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ref_json=_canon(ref)
            if existing:
                c.execute(f"UPDATE model_reference_override SET reference_json=?,reference_kind=?,reference_name_raw=?,status='confirmed',model_id=?,location_id=?,version=?,updated_at={now} WHERE id=? AND version=?",
                          (ref_json,str(ref.get("kind","unknown")),ref.get("name"),art["id"],loc_id,new_version,existing["id"],version)); oid=existing["id"]
            else:
                c.execute(f"INSERT INTO model_reference_override(uuid,library_id,profile_id,asset_id,revision_id,reference_key,reference_json,reference_kind,reference_name_raw,status,model_id,location_id,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'confirmed',?,?,?,{now},{now})",
                          (str(uuid.uuid4()),lib,row["profile_id"],row["asset_id"],row["revision_id"],key,ref_json,str(ref.get("kind","unknown")),ref.get("name"),art["id"],loc_id,new_version)); oid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
            evidence={"claim":"human_identity_mapping_only","execution_proof":False,"modelUuid":art["uuid"],"locationUuid":location_uuid}
            c.execute(f"INSERT INTO model_reference_override_event(uuid,library_id,override_id,version,action,model_id,location_id,evidence_json,created_at) VALUES(?,?,?,?,'confirm',?,?,?,{now})",
                      (str(uuid.uuid4()),lib,oid,new_version,art["id"],loc_id,_canon(evidence)))
            if fresh_location_receipt_uuid:
                c.execute("UPDATE model_inspection_receipt SET consumed=1 WHERE receipt_uuid=? AND consumed=0",(str(fresh_location_receipt_uuid),))
            return {"overrideUuid":c.execute("SELECT uuid FROM model_reference_override WHERE id=?",(oid,)).fetchone()[0],"version":new_version,"status":"confirmed","modelUuid":art["uuid"],"locationUuid":location_uuid}

    def revoke_reference(self, library_uuid, profile_uuid, media_uuid, revision_uuid, key, *,
                         expected_current_revision_uuid, expected_override_version):
        if str(revision_uuid)!=str(expected_current_revision_uuid): raise ValueError("Current revision expectation mismatch")
        with self._transaction() as c:
            lib=self._library(c,library_uuid); row,_=self._actual_reference(c,lib,profile_uuid,media_uuid,revision_uuid,key)
            old=c.execute("SELECT * FROM model_reference_override WHERE library_id=? AND profile_id=? AND asset_id=? AND revision_id=? AND reference_key=?",(lib,row["profile_id"],row["asset_id"],row["revision_id"],key)).fetchone()
            if not old or old["status"]!="confirmed" or type(expected_override_version)is not int or old["version"]!=expected_override_version: raise ModelIdentityConflictError("Override version conflict or already revoked")
            next_version=old["version"]+1; prior_model=old["model_id"]; prior_location=old["location_id"]
            c.execute("UPDATE model_reference_override SET status='revoked',model_id=NULL,location_id=NULL,version=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND version=?",(next_version,old["id"],old["version"]))
            evidence={"claim":"human_identity_mapping_only","execution_proof":False,"revokedModelId":prior_model,"revokedLocationId":prior_location}
            c.execute("INSERT INTO model_reference_override_event(uuid,library_id,override_id,version,action,model_id,location_id,evidence_json,created_at) VALUES(?,?,?,?,'revoke',?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",(str(uuid.uuid4()),lib,old["id"],next_version,prior_model,prior_location,_canon(evidence)))
            return {"version":next_version,"status":"revoked"}

    def read_reference_override(self, library_uuid, profile_uuid, media_uuid, revision_uuid, key):
        with self._transaction(write=False) as c:
            lib=self._library(c,library_uuid)
            row=c.execute("SELECT o.status,o.version,m.uuid model_uuid,l.uuid location_uuid,"
                          "m.display_name model_name,l.relative_path model_path FROM model_reference_override o "
                          "JOIN local_profile p ON p.id=o.profile_id JOIN media_asset a ON a.id=o.asset_id "
                          "JOIN media_revision r ON r.id=o.revision_id LEFT JOIN model_artifact m ON m.id=o.model_id "
                          "LEFT JOIN model_location l ON l.id=o.location_id WHERE o.library_id=? AND p.uuid=? AND a.uuid=? AND r.uuid=? AND o.reference_key=?",
                          (lib,str(profile_uuid),str(media_uuid),str(revision_uuid),key)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _safe_model_path(root_path, relative):
        from .model_paths import resolve_model_path
        return resolve_model_path(root_path, str(relative))
