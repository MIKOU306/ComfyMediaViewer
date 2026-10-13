"""Initial normalized SQLite catalog schema."""

SCHEMA_VERSION = 1


def schema_sql() -> str:
    return r"""
CREATE TABLE library (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE library_root (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    case_sensitive INTEGER CHECK (case_sensitive IN (0,1)),
    availability TEXT NOT NULL DEFAULT 'unknown'
        CHECK (availability IN ('unknown','online','offline','unreadable')),
    UNIQUE (library_id, id),
    UNIQUE (library_id, path_key),
    FOREIGN KEY (library_id) REFERENCES library(id) ON DELETE RESTRICT
);
CREATE TABLE local_profile (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT 'Local',
    UNIQUE (library_id, id),
    FOREIGN KEY (library_id) REFERENCES library(id) ON DELETE RESTRICT
);
CREATE TABLE media_asset (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    root_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    live_path_key TEXT,
    current_revision_id INTEGER,
    availability TEXT NOT NULL DEFAULT 'unknown'
        CHECK (availability IN ('unknown','present','missing','offline','unreadable')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (library_id, id),
    UNIQUE (library_id, root_id, id),
    FOREIGN KEY (library_id, root_id) REFERENCES library_root(library_id, id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX media_asset_current_path_uq
    ON media_asset(library_id, live_path_key) WHERE live_path_key IS NOT NULL;
CREATE TABLE media_revision (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    content_hash TEXT,
    verification_state TEXT NOT NULL DEFAULT 'unchecked'
        CHECK (verification_state IN ('unchecked','checking','verified','error')),
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    mtime_ns INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (asset_id, id),
    UNIQUE (library_id, asset_id, id),
    FOREIGN KEY (library_id, asset_id) REFERENCES media_asset(library_id, id) ON DELETE RESTRICT
);
CREATE INDEX media_revision_asset_idx ON media_revision(asset_id);
CREATE TRIGGER media_asset_current_revision_insert
BEFORE INSERT ON media_asset WHEN NEW.current_revision_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM media_revision r
        WHERE r.library_id=NEW.library_id AND r.asset_id=NEW.id AND r.id=NEW.current_revision_id
    ) THEN RAISE(ABORT, 'current revision must belong to the same asset and library') END;
END;
CREATE TRIGGER media_revision_current_delete
BEFORE DELETE ON media_revision
WHEN EXISTS (
    SELECT 1 FROM media_asset a
    WHERE a.library_id=OLD.library_id AND a.id=OLD.asset_id AND a.current_revision_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'cannot delete an asset current revision');
END;
CREATE TRIGGER media_revision_current_identity_update
BEFORE UPDATE OF id, asset_id, library_id ON media_revision
WHEN EXISTS (
    SELECT 1 FROM media_asset a
    WHERE a.library_id=OLD.library_id AND a.id=OLD.asset_id AND a.current_revision_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'cannot change identity of an asset current revision');
END;
CREATE TRIGGER media_asset_current_revision_update
BEFORE UPDATE OF current_revision_id, library_id, id ON media_asset
WHEN NEW.current_revision_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM media_revision r
        WHERE r.library_id=NEW.library_id AND r.asset_id=NEW.id AND r.id=NEW.current_revision_id
    ) THEN RAISE(ABORT, 'current revision must belong to the same asset and library') END;
END;
CREATE TABLE user_media_state (
    profile_id INTEGER NOT NULL,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0,1)),
    favorite_category TEXT,
    favorite_time TEXT,
    favorite_seq INTEGER NOT NULL DEFAULT 0 CHECK (favorite_seq >= 0),
    favorite_intent INTEGER NOT NULL DEFAULT 0 CHECK (favorite_intent IN (0,1)),
    mark_state TEXT,
    mark_updated TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (profile_id, asset_id),
    FOREIGN KEY (library_id, profile_id) REFERENCES local_profile(library_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (library_id, asset_id) REFERENCES media_asset(library_id, id) ON DELETE RESTRICT
);
CREATE TABLE collection (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (library_id, id),
    UNIQUE (profile_id, name),
    FOREIGN KEY (library_id, profile_id) REFERENCES local_profile(library_id, id) ON DELETE RESTRICT
);
CREATE TABLE collection_item (
    collection_id INTEGER NOT NULL,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (collection_id, asset_id),
    FOREIGN KEY (library_id, collection_id) REFERENCES collection(library_id, id) ON DELETE CASCADE,
    FOREIGN KEY (library_id, asset_id) REFERENCES media_asset(library_id, id) ON DELETE RESTRICT
);
CREATE TABLE tag (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (library_id, id),
    UNIQUE (profile_id, name),
    FOREIGN KEY (library_id, profile_id) REFERENCES local_profile(library_id, id) ON DELETE RESTRICT
);
CREATE TABLE tag_assignment (
    tag_id INTEGER NOT NULL,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    assigned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (tag_id, asset_id),
    FOREIGN KEY (library_id, tag_id) REFERENCES tag(library_id, id) ON DELETE CASCADE,
    FOREIGN KEY (library_id, asset_id) REFERENCES media_asset(library_id, id) ON DELETE RESTRICT
);
CREATE TABLE migration_job (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preflight','backed_up','importing','reconciled','switching','active','failed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE migration_mapping (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    source_key TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_uuid TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'mapped' CHECK (outcome IN ('mapped','quarantined','duplicate')),
    UNIQUE (source_kind, source_hash, source_key),
    FOREIGN KEY (job_id) REFERENCES migration_job(id) ON DELETE CASCADE
);
"""
