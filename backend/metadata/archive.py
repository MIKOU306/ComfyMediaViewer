"""Durable raw-metadata snapshots attached to verified catalog revisions."""

from __future__ import annotations

from backend.storage.integrity import foreign_key_violation
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterator, Mapping
import zlib

from .raw_png import (DEFAULT_MAX_SINGLE_EXIF_BYTES, DEFAULT_MAX_TOTAL_EXIF_BYTES,
                      read_png_text)

ARCHIVE_SCHEMA_VERSION = 2
COLLECTOR_VERSION = "raw-png-exif/3"

# ---------------------------------------------------------------------------
# v2：把 result_json 里的「大块原始字节」外置到独立表。
#
# 背景（实测本机正式库 5699 条快照）：result_json 平均 172KB、最大 4.2MB，
# 整表占 944MB —— 其中 text_chunks 独占 99.6% 体积，而查询真正用到的
# dimensions 只有 49 字节。这带来两个问题：
#   1. 尺寸筛选要对约 1GB 的 JSON 做 4 次 json_extract，耗时 6.2s；
#   2. 库体积失控，且 text_chunks 内部有大量重复结构，可压缩 79%。
#
# v2 的做法：
#   · BULKY_KEYS 里的键改存到 metadata_snapshot_text（zlib 压缩）；
#   · result_json 只留精简字段，对外契约由 _rehydrate_result() 复原，
#     所有读取方（/api/meta、workflow_source_store、pipeline、
#     workflow_sources、workflow_projection）无需改动；
#   · 新增 width_px/height_px 生成列 + 索引，供尺寸筛选走索引。
#
# 为什么不直接把整列压缩：result_json 是 NOT NULL 且被 json_extract 直查，
# 改成 BLOB 会让 SQL 层无法索引，所以只外置「查询从不使用」的大块字段。
# ---------------------------------------------------------------------------

# 各采集器的原始字节字段。判定依据是 backend/metadata/raw_*.py 的返回值：
#   PNG  → text_chunks / exif_chunks
#   WebP → chunks
#   MP4  → records / text_candidates
#   EBML → elements / text_candidates
# 空列表也照样外置，保证 result_json 体积与内容无关。
BULKY_KEYS = ("text_chunks", "exif_chunks", "chunks", "records",
              "text_candidates", "elements")

# result_json 里保留下来的「查询与界面真正用到」的键。
# 刻意保留 dimensions（供生成列提取）、status/coverage/errors/limits 等诊断字段。


class MetadataArchiveError(RuntimeError):
    """Base error for archive schema, identity, and consistency failures."""


class MetadataArchiveUnavailableError(MetadataArchiveError):
    """The existing catalog cannot be opened without creating or initializing it."""


class MetadataArchiveSchemaError(MetadataArchiveError):
    """The catalog or independent metadata archive schema is absent or invalid."""


class MetadataRevisionError(MetadataArchiveError):
    """The caller's verified revision does not match the current catalog revision."""


class MetadataSourceChangedError(MetadataArchiveError):
    """The source bytes or filesystem identity changed during capture."""


def _archive_schema_sql(version: int = ARCHIVE_SCHEMA_VERSION) -> str:
    """Build the DDL for one archive schema version.

    v1 is the original single-table shape, kept verbatim so an existing v1
    database still matches its recorded checksum and can migrate forward.
    v2 adds the bulky-payload table plus the dimensions generated columns.
    """
    if version == 1:
        return """CREATE TABLE metadata_snapshot (
        id INTEGER PRIMARY KEY,
        uuid TEXT NOT NULL UNIQUE,
        library_id INTEGER NOT NULL,
        asset_id INTEGER NOT NULL,
        revision_id INTEGER NOT NULL,
        source_format TEXT NOT NULL,
        collector_version TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('complete','partial','no_metadata','error','too_large')),
        coverage TEXT NOT NULL CHECK (coverage IN ('complete','partial','none')),
        source_sha256 TEXT NOT NULL,
        result_json TEXT NOT NULL,
        captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE (revision_id, collector_version),
        FOREIGN KEY (library_id) REFERENCES library(id) ON DELETE RESTRICT,
        FOREIGN KEY (library_id, asset_id)
            REFERENCES media_asset(library_id, id) ON DELETE RESTRICT,
        FOREIGN KEY (library_id, asset_id, revision_id)
            REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT
    );
    CREATE INDEX metadata_snapshot_asset_idx
        ON metadata_snapshot(library_id, asset_id, revision_id);
    """
    if version != 2:
        raise MetadataArchiveSchemaError(f"No archive schema DDL for version {version}")
    return """CREATE TABLE metadata_snapshot (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    library_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    source_format TEXT NOT NULL,
    collector_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('complete','partial','no_metadata','error','too_large')),
    coverage TEXT NOT NULL CHECK (coverage IN ('complete','partial','none')),
    source_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (revision_id, collector_version),
    FOREIGN KEY (library_id) REFERENCES library(id) ON DELETE RESTRICT,
    FOREIGN KEY (library_id, asset_id)
        REFERENCES media_asset(library_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (library_id, asset_id, revision_id)
        REFERENCES media_revision(library_id, asset_id, id) ON DELETE RESTRICT
);
CREATE INDEX metadata_snapshot_asset_idx
    ON metadata_snapshot(library_id, asset_id, revision_id);
CREATE TABLE metadata_snapshot_text (
    snapshot_id INTEGER PRIMARY KEY,
    payload BLOB NOT NULL,
    raw_bytes INTEGER NOT NULL CHECK (raw_bytes >= 0),
    stored_bytes INTEGER NOT NULL CHECK (stored_bytes >= 0),
    FOREIGN KEY (snapshot_id) REFERENCES metadata_snapshot(id) ON DELETE CASCADE
);
"""


def _schema_checksum(version: int = ARCHIVE_SCHEMA_VERSION) -> str:
    return hashlib.sha256(
        f"{version}\n{_archive_schema_sql(version)}".encode("utf-8")
    ).hexdigest()


def _split_result(result: Mapping[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    """Split bulky raw-byte fields off a collector result.

    Returns the slim mapping to store as ``result_json`` and the compressed
    bulky payload, or ``None`` when the result carries no bulky field at all.
    """
    slim = dict(result)
    bulky: dict[str, Any] = {}
    for key in BULKY_KEYS:
        if key in slim:
            bulky[key] = slim.pop(key)
    if not bulky:
        return slim, None
    raw = json.dumps(bulky, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return slim, zlib.compress(raw, 6)


def rehydrate_snapshot_result(slim: Mapping[str, Any],
                             payload: bytes | None) -> dict[str, Any]:
    """Restore bulky fields so callers see the same shape the collector produced.

    Public because other stores (workflow_source_store) read snapshot rows with
    their own SQL and must rehydrate before handing results to the extractors.
    """
    result = dict(slim)
    if payload is None:
        # A v1 snapshot that had no bulky keys still needs the keys present so
        # readers iterating BULKY_KEYS do not have to special-case None.
        return result
    bulky = json.loads(zlib.decompress(payload).decode("utf-8"))
    result.update(bulky)
    return result


# Internal alias kept so the archive's own call sites read consistently.
_rehydrate_result = rehydrate_snapshot_result


def _stat_signature(info: os.stat_result) -> tuple[int, int, int | None, int | None, int | None]:
    return (int(info.st_size), int(info.st_mtime_ns), getattr(info, "st_dev", None),
            getattr(info, "st_ino", None), getattr(info, "st_ctime_ns", None))


def _stream_sha256(stream) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


class MetadataArchive:
    """Archive raw metadata for revisions in an already initialized catalog.

    Construction has no side effects. Call ``initialize`` explicitly to add the
    metadata-only schema. Capture never changes the catalog's core schema.
    """

    def __init__(self, database: str | os.PathLike[str]):
        self.database = Path(database).absolute()

    @contextmanager
    def _transaction(self, *, write: bool, require_archive: bool = True) -> Iterator[sqlite3.Connection]:
        if self.database.is_symlink():
            raise MetadataArchiveUnavailableError("Catalog path must not be a symlink")
        try:
            real = self.database.resolve(strict=True)
            if not real.is_file():
                raise MetadataArchiveUnavailableError("Catalog database does not exist")
            uri = real.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, MetadataArchiveError):
                raise
            raise MetadataArchiveUnavailableError(f"Cannot open existing catalog: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if foreign_key_violation(connection) is not None:
                raise MetadataArchiveSchemaError("Catalog has foreign key violations")
            catalog = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not catalog or not connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                raise MetadataArchiveSchemaError("Catalog schema is not initialized")
            # Import lazily to avoid changing the catalog at construction. Existing
            # initialized schemas are validated by this function without migration.
            from ..storage.database import initialize_schema
            initialize_schema(connection)
            if require_archive:
                self._validate_archive_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_archive_schema(connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_schema_migrations'"
        ).fetchone()
        if not exists:
            raise MetadataArchiveSchemaError("Metadata archive is not initialized")
        rows = connection.execute(
            "SELECT version,checksum FROM metadata_schema_migrations ORDER BY version"
        ).fetchall()
        if len(rows) != 1 or rows[0]["version"] != ARCHIVE_SCHEMA_VERSION \
                or rows[0]["checksum"] != _schema_checksum():
            raise MetadataArchiveSchemaError("Metadata archive migration checksum/version mismatch")
        if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_snapshot'").fetchone():
            raise MetadataArchiveSchemaError("Metadata snapshot table is missing")
        if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='metadata_snapshot_text'").fetchone():
            raise MetadataArchiveSchemaError("Metadata snapshot text table is missing")
        # Generated columns are what make dimension filtering indexable; if they
        # are absent the query still works but falls back to scanning every
        # result_json, which measured 6.2s on the full catalog. Verify by name.
        # NOTE: PRAGMA table_info hides VIRTUAL generated columns — table_xinfo
        # is required, otherwise every correct v2 database looks broken here.
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(metadata_snapshot)")}
        if not {"width_px", "height_px"} <= columns:
            raise MetadataArchiveSchemaError("Metadata snapshot dimension columns are missing")

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> int:
        """Bring a v1 archive forward to v2 without losing any captured bytes.

        Runs under the caller's write transaction. The bulky payload is copied
        into metadata_snapshot_text first and only then is result_json slimmed,
        so an interruption leaves every snapshot still readable by the v1 shape.
        """
        rows = connection.execute(
            "SELECT version,checksum FROM metadata_schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            raise MetadataArchiveSchemaError("Metadata archive is not initialized")
        current = int(rows[-1]["version"])
        if current > ARCHIVE_SCHEMA_VERSION:
            raise MetadataArchiveSchemaError(
                "Metadata archive schema is newer than this build supports")
        if current == ARCHIVE_SCHEMA_VERSION:
            return ARCHIVE_SCHEMA_VERSION
        if current != 1 or rows[-1]["checksum"] != _schema_checksum(1):
            raise MetadataArchiveSchemaError("Metadata archive migration checksum/version mismatch")

        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata_snapshot_text ("
            "snapshot_id INTEGER PRIMARY KEY, payload BLOB NOT NULL, "
            "raw_bytes INTEGER NOT NULL CHECK (raw_bytes >= 0), "
            "stored_bytes INTEGER NOT NULL CHECK (stored_bytes >= 0), "
            "FOREIGN KEY (snapshot_id) REFERENCES metadata_snapshot(id) ON DELETE CASCADE)")
        # ALTER TABLE ADD COLUMN cannot add STORED, and VIRTUAL generated columns
        # are exactly what we want: they read from result_json at query time.
        connection.execute(
            "ALTER TABLE metadata_snapshot ADD COLUMN width_px INTEGER "
            "GENERATED ALWAYS AS (CASE WHEN json_type(result_json,'$.dimensions.width')='integer' "
            "THEN json_extract(result_json,'$.dimensions.width') END) VIRTUAL")
        connection.execute(
            "ALTER TABLE metadata_snapshot ADD COLUMN height_px INTEGER "
            "GENERATED ALWAYS AS (CASE WHEN json_type(result_json,'$.dimensions.height')='integer' "
            "THEN json_extract(result_json,'$.dimensions.height') END) VIRTUAL")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS metadata_snapshot_dimensions_idx "
            "ON metadata_snapshot(width_px, height_px)")
        # Dimension filtering keeps an exact "latest snapshot per revision" guard;
        # this index is what lets that correlated subquery resolve by seek instead
        # of scanning (measured 6.2s -> 1.2s together with the generated columns).
        connection.execute(
            "CREATE INDEX IF NOT EXISTS metadata_snapshot_latest_idx "
            "ON metadata_snapshot(asset_id, revision_id, captured_at DESC, id DESC)")

        migrated = 0
        for row in connection.execute(
                "SELECT id,result_json FROM metadata_snapshot "
                "WHERE id NOT IN (SELECT snapshot_id FROM metadata_snapshot_text)").fetchall():
            try:
                result = json.loads(row["result_json"])
            except (TypeError, ValueError) as exc:
                raise MetadataArchiveSchemaError(
                    f"Snapshot {row['id']} has unreadable result_json: {exc}") from exc
            if not isinstance(result, dict):
                raise MetadataArchiveSchemaError(
                    f"Snapshot {row['id']} result_json is not an object")
            slim, payload = _split_result(result)
            if payload is None:
                continue
            raw_bytes = len(json.dumps(
                {key: result[key] for key in BULKY_KEYS if key in result},
                ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            connection.execute(
                "INSERT INTO metadata_snapshot_text(snapshot_id,payload,raw_bytes,stored_bytes) "
                "VALUES (?,?,?,?)",
                (row["id"], payload, raw_bytes, len(payload)))
            connection.execute(
                "UPDATE metadata_snapshot SET result_json=? WHERE id=?",
                (json.dumps(slim, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")), row["id"]))
            migrated += 1

        orphan = connection.execute(
            "SELECT COUNT(*) FROM metadata_snapshot_text t "
            "WHERE NOT EXISTS(SELECT 1 FROM metadata_snapshot s WHERE s.id=t.snapshot_id)"
        ).fetchone()[0]
        if orphan:
            raise MetadataArchiveSchemaError("Bulky payload rows are orphaned")
        connection.execute("DELETE FROM metadata_schema_migrations")
        connection.execute(
            "INSERT INTO metadata_schema_migrations(version,checksum) VALUES (?,?)",
            (ARCHIVE_SCHEMA_VERSION, _schema_checksum()))
        return migrated

    def initialize(self) -> int:
        """Explicitly create and checksum the independent metadata archive schema."""
        with self._transaction(write=True, require_archive=False) as connection:
            migration_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_schema_migrations'"
            ).fetchone()
            if migration_table:
                self._migrate_schema(connection)
                self._validate_archive_schema(connection)
                return ARCHIVE_SCHEMA_VERSION
            partial = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_snapshot'"
            ).fetchone()
            if partial:
                raise MetadataArchiveSchemaError("Partial metadata archive schema exists")
            connection.execute(
                "CREATE TABLE metadata_schema_migrations (version INTEGER PRIMARY KEY, "
                "checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
            )
            for statement in _archive_schema_sql().split(";"):
                if statement.strip():
                    connection.execute(statement)
            # Fresh v2 databases also get the generated columns and their index;
            # they are part of the query contract, not an optional extra.
            connection.execute(
                "ALTER TABLE metadata_snapshot ADD COLUMN width_px INTEGER "
                "GENERATED ALWAYS AS (CASE WHEN json_type(result_json,'$.dimensions.width')='integer' "
                "THEN json_extract(result_json,'$.dimensions.width') END) VIRTUAL")
            connection.execute(
                "ALTER TABLE metadata_snapshot ADD COLUMN height_px INTEGER "
                "GENERATED ALWAYS AS (CASE WHEN json_type(result_json,'$.dimensions.height')='integer' "
                "THEN json_extract(result_json,'$.dimensions.height') END) VIRTUAL")
            connection.execute(
                "CREATE INDEX metadata_snapshot_dimensions_idx "
                "ON metadata_snapshot(width_px, height_px)")
            connection.execute(
                "CREATE INDEX metadata_snapshot_latest_idx "
                "ON metadata_snapshot(asset_id, revision_id, captured_at DESC, id DESC)")
            connection.execute(
                "INSERT INTO metadata_schema_migrations(version,checksum) VALUES (?,?)",
                (ARCHIVE_SCHEMA_VERSION, _schema_checksum()),
            )
        return ARCHIVE_SCHEMA_VERSION

    def capture_png(self, verified_revision, file_path=None, **options):
        return self._capture_image(verified_revision, file_path, **options)

    def capture_jpeg(self, verified_revision, file_path=None, *, collector_version='raw-jpeg-segments/3',
                     max_total_bytes=8*1024*1024, max_segments=4096,max_scan_bytes=128*1024*1024):
        return self._capture_image(verified_revision,file_path,collector_version=collector_version,
                                   _source_format='jpeg',_jpeg_limits={'max_total_bytes':max_total_bytes,'max_segments':max_segments,
                                                                   'full_scan':True,'max_scan_bytes':max_scan_bytes})

    def capture_webp(self, verified_revision, file_path=None, *,
                     collector_version='raw-webp-chunks/1',
                     max_metadata_bytes=8*1024*1024, max_chunk_bytes=64*1024*1024,
                     max_chunks=4096):
        return self._capture_image(verified_revision, file_path,
                                  collector_version=collector_version,
                                  _source_format='webp', _webp_limits={
                                      'max_metadata_bytes': max_metadata_bytes,
                                      'max_chunk_bytes': max_chunk_bytes,
                                      'max_chunks': max_chunks})

    def capture_mp4(self, verified_revision, file_path=None, *,
                    collector_version='raw-mp4-boxes/2',
                    max_metadata_bytes=8*1024*1024, max_chunk_bytes=1024*1024,
                    max_boxes=4096, max_depth=16):
        """Capture container material for a strictly verified revision.

        The collector skips media payloads; integrity hashing still reads the
        entire file before and after capture. This is an on-demand operation.
        """
        return self._capture_image(verified_revision, file_path,
                                  collector_version=collector_version,
                                  _source_format='mp4', _mp4_limits={
                                      'max_metadata_bytes': max_metadata_bytes,
                                      'max_chunk_bytes': max_chunk_bytes,
                                      'max_boxes': max_boxes, 'max_depth': max_depth})

    def capture_ebml(self, verified_revision, file_path=None, *,
                     collector_version='raw-ebml-elements/1',
                     max_metadata_bytes=8*1024*1024, max_chunk_bytes=1024*1024,
                     max_elements=4096, max_depth=16):
        """Capture WebM/MKV materials; full-file integrity hashing still applies."""
        return self._capture_image(verified_revision, file_path,
                                  collector_version=collector_version,
                                  _source_format='ebml', _ebml_limits={
                                      'max_metadata_bytes': max_metadata_bytes,
                                      'max_chunk_bytes': max_chunk_bytes,
                                      'max_elements': max_elements, 'max_depth': max_depth})

    def _capture_image(self, verified_revision: Mapping[str, Any],
                    file_path: str | os.PathLike[str] | None = None,
                    *, collector_version: str = COLLECTOR_VERSION,
                    max_single_text_bytes: int = 1 * 1024 * 1024,
                    max_total_text_bytes: int = 8 * 1024 * 1024,
                    max_decompressed_bytes: int = 8 * 1024 * 1024,
                    max_chunk_bytes: int = 64 * 1024 * 1024,
                    max_single_exif_bytes: int = DEFAULT_MAX_SINGLE_EXIF_BYTES,
                    max_total_exif_bytes: int = DEFAULT_MAX_TOTAL_EXIF_BYTES,
                    max_chunks: int = 4096,
                    _source_format='png', _jpeg_limits=None, _webp_limits=None,
                    _mp4_limits=None, _ebml_limits=None) -> dict[str, Any]:
        """Capture a media source once for a currently verified revision.

        The supplied record must be a catalog identity record containing libraryId,
        mediaId, revisionId, contentHash, verificationState, and path. A repeated
        revision/collector pair returns its stored snapshot without reopening the
        source, so snapshots remain readable after the original file is removed.
        """
        if not isinstance(verified_revision, Mapping):
            raise MetadataRevisionError("verified_revision must be a catalog record")
        required = ("libraryId", "mediaId", "revisionId", "contentHash", "verificationState", "path")
        if any(not verified_revision.get(key) for key in required):
            raise MetadataRevisionError("Verified revision record is incomplete")
        if verified_revision.get("verificationState") != "verified":
            raise MetadataRevisionError("Revision is not verified")
        content_hash = str(verified_revision["contentHash"]).lower()
        if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
            raise MetadataRevisionError("Revision contentHash is not a SHA-256 digest")
        if not isinstance(collector_version, str) or not collector_version:
            raise ValueError("collector_version must be a non-empty string")

        with self._transaction(write=False) as connection:
            scope = self._validated_revision(connection, verified_revision)
            existing = connection.execute(
                "SELECT s.uuid,s.source_format,s.collector_version,s.state,s.coverage,"
                "s.source_sha256,s.result_json,s.captured_at,t.payload "
                "FROM metadata_snapshot s LEFT JOIN metadata_snapshot_text t "
                "ON t.snapshot_id=s.id WHERE s.revision_id=? AND s.collector_version=?",
                (scope["revision_id"], collector_version),
            ).fetchone()
            if existing is not None:
                return self._snapshot_record(existing)

        source_path = Path(file_path if file_path is not None else verified_revision["path"]).absolute()
        if os.fspath(source_path) != str(verified_revision["path"]):
            raise MetadataRevisionError("Capture path differs from the catalog path")
        try:
            before = source_path.stat()
            with source_path.open("rb") as stream:
                fd_before = os.fstat(stream.fileno())
                digest_before = _stream_sha256(stream)
                stream.seek(0)
                if _source_format == 'jpeg':
                    from backend.metadata.raw_jpeg import read_jpeg_headers
                    result = read_jpeg_headers(stream,**_jpeg_limits)
                elif _source_format == 'webp':
                    from backend.metadata.raw_webp import read_webp_metadata
                    result = read_webp_metadata(stream, **_webp_limits)
                elif _source_format == 'mp4':
                    from backend.metadata.raw_mp4 import read_mp4_metadata
                    result = read_mp4_metadata(stream, **_mp4_limits)
                elif _source_format == 'ebml':
                    from backend.metadata.raw_ebml import read_ebml_metadata
                    result = read_ebml_metadata(stream, **_ebml_limits)
                else:
                    result = read_png_text(
                    stream,
                    max_single_text_bytes=max_single_text_bytes,
                    max_total_text_bytes=max_total_text_bytes,
                    max_decompressed_bytes=max_decompressed_bytes,
                    max_chunk_bytes=max_chunk_bytes,
                    max_single_exif_bytes=max_single_exif_bytes,
                    max_total_exif_bytes=max_total_exif_bytes,
                    max_chunks=max_chunks,
                )
                digest_after = _stream_sha256(stream)
                fd_after = os.fstat(stream.fileno())
            after = source_path.stat()
        except OSError as exc:
            raise MetadataSourceChangedError(f"Cannot capture verified source file: {exc}") from exc
        # CMV3-LOCAL-FIX(W1) — 上游 Windows 平台缺陷适配，详见 doc/上游缺陷适配清单.md
        # 上游原代码为三向比对：
        #     _stat_signature(before) != _stat_signature(after)
        #     or _stat_signature(fd_before) != _stat_signature(fd_after)
        #     or _stat_signature(before) != _stat_signature(fd_before)   <-- 本行已移除
        # 第 3 条做的是「跨 API 比对」：stat() 走目录项、fstat() 走文件句柄，
        # Windows 上两者读到的 st_ctime_ns 来源与粒度不同（实测 200 个真实文件
        # 有 180 个不一致，偏差 0.5ms ~ 75s；POSIX 上两者同源故恒等）。
        # 该条在 Windows 上恒假，会使全部 5 个 capture_* (png/jpeg/webp/mp4/ebml)
        # 对任何媒体都抛 MetadataSourceChangedError，元数据采集彻底不可用。
        # 移除后仍保留「同源比对」三层保护：路径前后、fd 前后、以及下方 digest
        # 前后 + digest 对 content_hash + size/mtime 对 revision，语义覆盖完整。
        if (_stat_signature(before) != _stat_signature(after)
                or _stat_signature(fd_before) != _stat_signature(fd_after)
                or digest_before != digest_after):
            raise MetadataSourceChangedError("File changed during raw metadata capture")
        if digest_before != content_hash:
            raise MetadataSourceChangedError("Source bytes do not match the verified revision hash")
        if verified_revision.get("size") is not None and int(verified_revision["size"]) != after.st_size:
            raise MetadataSourceChangedError("Source size does not match the verified revision")
        if verified_revision.get("mtimeNs") is not None and int(verified_revision["mtimeNs"]) != after.st_mtime_ns:
            raise MetadataSourceChangedError("Source mtime does not match the verified revision")

        full_recovery = bool(result.get("complete"))
        result["full_recovery"] = full_recovery
        result["not_full_recovery"] = not full_recovery
        result["source_sha256"] = content_hash
        slim, bulky_payload = _split_result(result)
        result_json = json.dumps(slim, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        with self._transaction(write=True) as connection:
            scope = self._validated_revision(connection, verified_revision)
            existing = connection.execute(
                "SELECT s.uuid,s.source_format,s.collector_version,s.state,s.coverage,"
                "s.source_sha256,s.result_json,s.captured_at,t.payload "
                "FROM metadata_snapshot s LEFT JOIN metadata_snapshot_text t "
                "ON t.snapshot_id=s.id WHERE s.revision_id=? AND s.collector_version=?",
                (scope["revision_id"], collector_version),
            ).fetchone()
            if existing is not None:
                return self._snapshot_record(existing)
            snapshot_uuid = str(uuid.uuid4())
            cursor = connection.execute(
                "INSERT INTO metadata_snapshot(uuid,library_id,asset_id,revision_id,source_format,"
                "collector_version,state,coverage,source_sha256,result_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (snapshot_uuid, scope["library_id"], scope["asset_id"], scope["revision_id"],
                 _source_format, collector_version,
                 'error' if result['status'] == 'unsupported' else result['status'], result["coverage"],
                 content_hash, result_json),
            )
            if bulky_payload is not None:
                bulky_raw = json.dumps(
                    {key: result[key] for key in BULKY_KEYS if key in result},
                    ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")
                connection.execute(
                    "INSERT INTO metadata_snapshot_text(snapshot_id,payload,raw_bytes,stored_bytes) "
                    "VALUES (?,?,?,?)",
                    (cursor.lastrowid, bulky_payload, len(bulky_raw), len(bulky_payload)))
            row = connection.execute(
                "SELECT s.uuid,s.source_format,s.collector_version,s.state,s.coverage,"
                "s.source_sha256,s.result_json,s.captured_at,t.payload "
                "FROM metadata_snapshot s LEFT JOIN metadata_snapshot_text t "
                "ON t.snapshot_id=s.id WHERE s.id=?", (cursor.lastrowid,),
            ).fetchone()
            return self._snapshot_record(row)

    @staticmethod
    def _validated_revision(connection: sqlite3.Connection,
                            record: Mapping[str, Any]) -> dict[str, Any]:
        row = connection.execute(
            "SELECT l.id AS library_id,a.id AS asset_id,a.path,a.uuid AS media_uuid,"
            "a.current_revision_id,r.id AS revision_id,r.uuid AS revision_uuid,"
            "r.content_hash,r.verification_state,r.size_bytes,r.mtime_ns "
            "FROM library l JOIN media_asset a ON a.library_id=l.id "
            "JOIN media_revision r ON r.library_id=a.library_id AND r.asset_id=a.id "
            "AND r.id=a.current_revision_id WHERE l.uuid=? AND a.uuid=? AND r.uuid=?",
            (record["libraryId"], record["mediaId"], record["revisionId"]),
        ).fetchone()
        if row is None:
            raise MetadataRevisionError("Revision is missing, foreign, or no longer current")
        if row["verification_state"] != "verified" or row["content_hash"] != str(record["contentHash"]).lower():
            raise MetadataRevisionError("Catalog revision is not verified with the caller's content hash")
        if row["path"] != record["path"]:
            raise MetadataRevisionError("Catalog path does not match the verified revision record")
        if record.get("size") is not None and row["size_bytes"] != int(record["size"]):
            raise MetadataRevisionError("Caller size differs from the catalog revision")
        if record.get("mtimeNs") is not None and row["mtime_ns"] != int(record["mtimeNs"]):
            raise MetadataRevisionError("Caller mtime differs from the catalog revision")
        return dict(row)

    @staticmethod
    def _snapshot_record(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        payload = row["payload"] if "payload" in keys else None
        return {
            "uuid": row["uuid"],
            "source_format": row["source_format"],
            "collector_version": row["collector_version"],
            "state": row["state"],
            "coverage": row["coverage"],
            "source_sha256": row["source_sha256"],
            "result": _rehydrate_result(json.loads(row["result_json"]), payload),
            "captured_at": row["captured_at"],
        }

    def read_snapshot(self, snapshot_uuid: str, *, library_uuid: str | None = None) -> dict[str, Any]:
        """Read one durable result without touching the source media file."""
        columns = ("s.uuid,s.source_format,s.collector_version,s.state,s.coverage,"
                   "s.source_sha256,s.result_json,s.captured_at,t.payload")
        with self._transaction(write=False) as connection:
            if library_uuid is None:
                row = connection.execute(
                    "SELECT " + columns.replace("s.", "") +
                    " FROM metadata_snapshot s LEFT JOIN metadata_snapshot_text t "
                    "ON t.snapshot_id=s.id WHERE s.uuid=?", (snapshot_uuid,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT " + columns +
                    " FROM metadata_snapshot s LEFT JOIN metadata_snapshot_text t "
                    "ON t.snapshot_id=s.id JOIN library l ON l.id=s.library_id "
                    "WHERE s.uuid=? AND l.uuid=?", (snapshot_uuid, library_uuid),
                ).fetchone()
            if row is None:
                raise MetadataArchiveError(f"Unknown metadata snapshot UUID: {snapshot_uuid}")
            return self._snapshot_record(row)

    def read_media(self, library_uuid: str, media_uuid: str, revision_uuid: str | None = None) -> dict[str, Any]:
        """Read archived detail in explicit library scope without touching media."""
        with self._transaction(write=False) as connection:
            join = 'r.uuid=?' if revision_uuid else 'r.id=a.current_revision_id'
            parameters = [revision_uuid] if revision_uuid else []
            parameters.extend([library_uuid, media_uuid])
            row = connection.execute(
                'SELECT a.uuid AS media_uuid,a.path,a.availability,r.uuid AS revision_uuid,'
                'r.size_bytes,r.mtime_ns,r.id=a.current_revision_id AS is_current,'
                'm.uuid,m.source_format,m.collector_version,m.state,m.coverage,m.source_sha256,'
                'm.result_json,m.captured_at,t.payload '
                'FROM media_asset a JOIN library l ON l.id=a.library_id '
                'LEFT JOIN media_revision r ON r.asset_id=a.id AND ' + join + ' '
                'LEFT JOIN metadata_snapshot m ON m.asset_id=a.id AND m.revision_id=r.id '
                'LEFT JOIN metadata_snapshot_text t ON t.snapshot_id=m.id '
                'WHERE l.uuid=? AND a.uuid=? ORDER BY m.captured_at DESC,m.id DESC LIMIT 1', parameters).fetchone()
            if row is None or (revision_uuid and row['revision_uuid'] is None):
                raise MetadataRevisionError('Unknown media or revision in this library')
            return {'mediaId': row['media_uuid'], 'revisionId': row['revision_uuid'],
                    'path': row['path'], 'availability': row['availability'],
                    'size': row['size_bytes'], 'mtimeNs': row['mtime_ns'],
                    'historical': not bool(row['is_current']),
                    'metadataArchive': self._snapshot_record(row) if row['uuid'] else None}

