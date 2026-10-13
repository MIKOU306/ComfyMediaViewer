"""Journaled, non-overwriting moves for explicitly authorized regular files.

Cross-volume moves copy through a durable, operation-specific staging file and
publish without replacement. Metadata callbacks must be idempotent by
operation UUID because a crash can occur after their commit but before the
journal records metadata_done.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable, Iterable, Any

from .journal import OperationJournal


class FileOperationError(RuntimeError):
    pass


class DestinationConflictError(FileOperationError):
    pass


class CrossVolumeUnsupportedError(FileOperationError):
    pass


class EvidenceError(FileOperationError):
    pass


MetadataCommit = Callable[[dict[str, Any]], None]


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    before = path.stat()
    digest = _hash(path)
    after = path.stat()
    before_key = (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
    after_key = (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
    if before_key != after_key:
        raise EvidenceError(f"file changed while hashing: {path}")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns,
            "device": after.st_dev, "inode": after.st_ino, "sha256": digest}


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_evidence(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _within(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        allowed = root.resolve(strict=True)
        try:
            if os.path.commonpath((str(resolved), str(allowed))) == str(allowed):
                return True
        except ValueError:
            continue
    return False


def _move_no_replace(source: Path, destination: Path) -> str:
    """Move a file without replacing a destination, returning its method."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        move.restype = ctypes.c_int
        if not move(str(source), str(destination)):
            error = ctypes.get_last_error()
            if error in (80, 183):
                raise DestinationConflictError("destination appeared before atomic move")
            raise OSError(error, ctypes.FormatError(error), str(source))
        return "MoveFileW"
    # link() is an atomic exclusive publish on the same filesystem. If the
    # process stops before unlink, recovery can prove both names are one inode.
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise DestinationConflictError("destination appeared before atomic publish") from exc
    os.unlink(source)
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)
    return "exclusive-hardlink"


def _publish_stage_no_replace(staging: Path, destination: Path) -> str:
    """Publish a same-volume staged file without replacing any existing name."""
    return _move_no_replace(staging, destination)


class JournaledFileOperations:
    def __init__(self, journal: OperationJournal, evidence_directory: str | Path,
                 *, move_strategy: Callable[[Path, Path], str] | None = None):
        self.journal = journal
        self.evidence_directory = Path(evidence_directory).expanduser().absolute()
        self.move_strategy = move_strategy or _move_no_replace

    def _evidence_path(self, operation_uuid: str) -> Path:
        return self.evidence_directory / (operation_uuid + ".json")

    @staticmethod
    def _paths(operation: dict[str, Any]) -> tuple[Path, Path]:
        return Path(operation["source_path"]), Path(operation["destination_path"])

    def _load_evidence(self, operation: dict[str, Any]) -> dict[str, Any]:
        path = self._evidence_path(operation["operation_uuid"])
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EvidenceError("operation evidence is missing or unreadable") from exc
        for key in ("operation_uuid", "library_uuid", "media_uuid", "revision_uuid",
                    "action", "source_path", "destination_path"):
            if evidence.get(key) != operation.get(key):
                raise EvidenceError("evidence identity does not match the immutable journal row")
        return evidence

    @staticmethod
    def _same_snapshot(path: Path, expected: dict[str, Any], *, strict_stat: bool) -> bool:
        try:
            actual = _identity(path)
        except OSError:
            return False
        if actual["sha256"] != expected.get("sha256") or actual["size"] != expected.get("size"):
            return False
        if strict_stat and (actual["mtime_ns"] != expected.get("mtime_ns") or
                            actual["device"] != expected.get("device") or
                            actual["inode"] != expected.get("inode")):
            return False
        return True

    def _authorized(self, source: Path, destination: Path, allowed_roots: Iterable[str | Path]) -> None:
        roots = [Path(item).expanduser() for item in allowed_roots]
        if not roots or not _within(source, roots) or not _within(destination, roots):
            raise FileOperationError("source and destination must be inside caller-authorized roots")
        if not source.parent.is_dir() or not destination.parent.is_dir():
            raise FileOperationError("source and destination parent directories must already exist")
        if source == destination:
            raise FileOperationError("source and destination must differ")

    def execute_move(self, *, library_uuid: str, media_uuid: str,
                     revision_uuid: str | None, action: str,
                     source_path: str | Path, destination_path: str | Path,
                     allowed_roots: Iterable[str | Path], metadata_commit: MetadataCommit,
                     operation_uuid: str | None = None) -> dict[str, Any]:
        source = Path(source_path).expanduser().absolute()
        destination = Path(destination_path).expanduser().absolute()
        self._authorized(source, destination, allowed_roots)
        if not source.is_file() or source.is_symlink():
            raise FileOperationError("source must be an existing regular file, not a symlink")
        if _exists(destination):
            raise DestinationConflictError("destination already exists; nothing was changed")
        snapshot = _identity(source)
        operation = self.journal.plan(
            library_uuid=library_uuid, media_uuid=media_uuid, revision_uuid=revision_uuid,
            action=action, source_path=str(source), destination_path=str(destination),
            operation_uuid=operation_uuid,
        )
        evidence_path = self._evidence_path(operation["operation_uuid"])
        evidence = {key: operation.get(key) for key in (
            "operation_uuid", "library_uuid", "media_uuid", "revision_uuid", "action",
            "source_path", "destination_path")}
        evidence.update({"source_snapshot": snapshot, "phase": "planned", "method": None})
        if evidence_path.exists():
            prior = self._load_evidence(operation)
            if prior.get("source_snapshot") != snapshot:
                raise EvidenceError("operation UUID already has different source evidence")
            evidence = prior
        else:
            _write_evidence(evidence_path, evidence)
        current = self.journal.get(operation["operation_uuid"])
        if current["state"] == "planned":
            self._finish_disk(current, evidence)
        return self._finish_metadata(operation["operation_uuid"], metadata_commit)

    def _finish_disk(self, operation: dict[str, Any], evidence: dict[str, Any]) -> None:
        source, destination = self._paths(operation)
        expected = evidence["source_snapshot"]
        source_exists, destination_exists = _exists(source), _exists(destination)
        if source_exists and destination_exists:
            if evidence.get("method") == "copy-publish":
                # A cross-volume publish has independent source and destination
                # files. Delete the source only after both match the planned
                # content and the original source still has its captured stat id.
                if (not self._same_snapshot(source, expected, strict_stat=True) or
                        not self._same_snapshot(destination, expected, strict_stat=False)):
                    self.journal.mark_conflict(operation["operation_uuid"], "source or published destination fails copy-publish evidence")
                    raise EvidenceError("both files exist but copy evidence does not prove a safe source unlink")
                source.unlink()
                _fsync_directory(source.parent)
                staging = self._staging_path(operation, evidence)
                if _exists(staging):
                    self._unlink_verified_stage(staging, expected)
                source_exists = False
            else:
            # Only the POSIX exclusive-link publish has a verifiable two-name
            # intermediate. Never remove a second, independent file.
                try:
                    src, dst = source.stat(), destination.stat()
                    same_file = (src.st_dev, src.st_ino) == (dst.st_dev, dst.st_ino)
                except OSError:
                    same_file = False
                if evidence.get("method") != "exclusive-hardlink" or not same_file or not self._same_snapshot(destination, expected, strict_stat=False):
                    self.journal.mark_conflict(operation["operation_uuid"], "source and destination both exist without provable executor-owned duplicate")
                    raise DestinationConflictError("both paths exist; neither file was removed")
                source.unlink()
                _fsync_directory(source.parent)
                source_exists = False
        if source_exists:
            if not self._same_snapshot(source, expected, strict_stat=True):
                self.journal.mark_conflict(operation["operation_uuid"], "source no longer matches persisted hash/stat evidence")
                raise EvidenceError("source changed after operation planning")
            if destination_exists:
                self.journal.mark_conflict(operation["operation_uuid"], "destination exists")
                raise DestinationConflictError("destination exists; no overwrite attempted")
            cross_volume = source.stat().st_dev != destination.parent.stat().st_dev
            if cross_volume:
                self._copy_publish(operation, evidence)
            else:
                # Persist the method before publication so a POSIX hard-link
                # crash between link and unlink is distinguishable on restart.
                if os.name != "nt":
                    evidence["method"] = "exclusive-hardlink"
                    _write_evidence(self._evidence_path(operation["operation_uuid"]), evidence)
                try:
                    evidence["method"] = self.move_strategy(source, destination)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        if isinstance(exc, FileExistsError):
                            self.journal.mark_conflict(operation["operation_uuid"], "destination appeared before exclusive publish")
                            raise DestinationConflictError("destination appeared before atomic move") from exc
                        raise
                    self._copy_publish(operation, evidence)
                except DestinationConflictError:
                    self.journal.mark_conflict(operation["operation_uuid"], "destination appeared before exclusive publish")
                    raise
                if evidence.get("method") != "copy-publish" and not self._same_snapshot(destination, expected, strict_stat=True):
                    self.journal.mark_conflict(operation["operation_uuid"], "same-volume destination failed post-move identity check")
                    raise EvidenceError("same-volume destination changed during move")
            evidence["phase"] = "disk_done"
            _write_evidence(self._evidence_path(operation["operation_uuid"]), evidence)
        elif destination_exists:
            if not self._same_snapshot(destination, expected, strict_stat=False):
                self.journal.mark_conflict(operation["operation_uuid"], "destination does not match persisted source hash evidence")
                raise EvidenceError("destination hash differs from the planned source")
            if evidence.get("method") == "copy-publish":
                staging = self._staging_path(operation, evidence)
                if _exists(staging):
                    self._unlink_verified_stage(staging, expected)
            evidence["phase"] = "disk_done"
            _write_evidence(self._evidence_path(operation["operation_uuid"]), evidence)
        else:
            self.journal.mark_conflict(operation["operation_uuid"], "both source and destination are missing")
            raise EvidenceError("cannot establish where the planned file went")
        self.journal.mark_disk_done(operation["operation_uuid"])

    def _staging_path(self, operation: dict[str, Any], evidence: dict[str, Any]) -> Path:
        saved = evidence.get("staging_path")
        expected = self._paths(operation)[1].with_name(
            self._paths(operation)[1].name + "." + operation["operation_uuid"] + ".stage")
        if saved is not None and saved != str(expected):
            raise EvidenceError("staging path does not match deterministic operation path")
        return expected

    @staticmethod
    def _unlink_verified_stage(staging: Path, expected: dict[str, Any]) -> None:
        if staging.is_symlink() or not JournaledFileOperations._same_snapshot(staging, expected, strict_stat=False):
            raise EvidenceError("staging file is not the verified operation copy; leaving it untouched")
        staging.unlink()
        _fsync_directory(staging.parent)

    def _copy_publish(self, operation: dict[str, Any], evidence: dict[str, Any]) -> None:
        source, destination = self._paths(operation)
        expected = evidence["source_snapshot"]
        staging = self._staging_path(operation, evidence)
        evidence.update({"method": "copy-publish", "staging_path": str(staging), "phase": "copying"})
        _write_evidence(self._evidence_path(operation["operation_uuid"]), evidence)

        if _exists(destination):
            if (not _exists(source) or not self._same_snapshot(source, expected, strict_stat=True) or
                    not self._same_snapshot(destination, expected, strict_stat=False)):
                self.journal.mark_conflict(operation["operation_uuid"], "existing copy-publish destination/source evidence mismatch")
                raise EvidenceError("destination cannot be safely associated with this source")
            # The normal both-present recovery branch performs the source unlink.
            return

        if _exists(staging):
            if staging.is_symlink():
                self.journal.mark_conflict(operation["operation_uuid"], "staging file hash/stat does not match source")
                raise EvidenceError("unverified staging data is preserved for review")
            if not self._same_snapshot(staging, expected, strict_stat=False):
                owned = evidence.get('staging_identity')
                actual = staging.stat()
                if (not isinstance(owned, dict) or
                        [actual.st_dev,actual.st_ino] != [owned.get('device'),owned.get('inode')] or
                        actual.st_size >= expected['size'] or
                        not self._same_snapshot(source, expected, strict_stat=True)):
                    self.journal.mark_conflict(operation['operation_uuid'], 'incomplete stage ownership/source mismatch')
                    raise EvidenceError('unverified staging data is preserved for review')
                with source.open('rb') as src, staging.open('r+b') as dst:
                    opened=os.fstat(dst.fileno())
                    if [opened.st_dev,opened.st_ino] != [owned['device'],owned['inode']]:
                        raise EvidenceError('staging identity changed before resume')
                    remaining=opened.st_size
                    while remaining:
                        count=min(1024*1024,remaining)
                        if src.read(count)!=dst.read(count):
                            self.journal.mark_conflict(operation['operation_uuid'], 'partial stage prefix differs from source')
                            raise EvidenceError('partial staging content is preserved for review')
                        remaining-=count
                    shutil.copyfileobj(src,dst,length=1024*1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                shutil.copystat(source,staging,follow_symlinks=False)
                if not self._same_snapshot(staging,expected,strict_stat=False) or not self._same_snapshot(source,expected,strict_stat=True):
                    self.journal.mark_conflict(operation['operation_uuid'], 'resumed copy verification failed')
                    raise EvidenceError('resumed staging data is preserved for review')
        else:
            if not self._same_snapshot(source, expected, strict_stat=True):
                self.journal.mark_conflict(operation["operation_uuid"], "source changed before cross-volume copy")
                raise EvidenceError("source no longer matches persisted identity")
            try:
                with source.open("rb") as src, staging.open("xb") as dst:
                    identity=os.fstat(dst.fileno())
                    evidence['staging_identity']={'device':identity.st_dev,'inode':identity.st_ino}
                    _write_evidence(self._evidence_path(operation['operation_uuid']),evidence)
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                shutil.copystat(source, staging, follow_symlinks=False)
                staged_fd = os.open(staging, os.O_RDWR)
                try:
                    os.fsync(staged_fd)
                finally:
                    os.close(staged_fd)
                _fsync_directory(staging.parent)
            except FileExistsError:
                self.journal.mark_conflict(operation["operation_uuid"], "staging path unexpectedly exists")
                raise EvidenceError("staging path collision; no existing file was overwritten")
            if not self._same_snapshot(staging, expected, strict_stat=False):
                self.journal.mark_conflict(operation["operation_uuid"], "staged copy failed full hash/size verification")
                raise EvidenceError("staging verification failed")
            if not self._same_snapshot(source, expected, strict_stat=True):
                self.journal.mark_conflict(operation["operation_uuid"], "source changed while cross-volume copy was made")
                raise EvidenceError("source changed during copy; staged file retained")

        if _exists(destination):
            self.journal.mark_conflict(operation["operation_uuid"], "destination appeared before staged publish")
            raise DestinationConflictError("destination appeared before exclusive publish")
        try:
            _publish_stage_no_replace(staging, destination)
        except DestinationConflictError as exc:
            self.journal.mark_conflict(operation["operation_uuid"], "destination appeared before staged publish")
            raise DestinationConflictError("destination appeared before exclusive publish") from exc
        except FileExistsError as exc:
            self.journal.mark_conflict(operation["operation_uuid"], "destination appeared before staged publish")
            raise DestinationConflictError("destination appeared before exclusive publish") from exc
        _fsync_directory(destination.parent)
        if not self._same_snapshot(destination, expected, strict_stat=False):
            self.journal.mark_conflict(operation["operation_uuid"], "published destination failed complete hash/size verification")
            raise EvidenceError("published destination failed verification; source is retained")
        # The copied destination is now durable. Keep source until its original
        # identity and contents have been checked once more.
        if not self._same_snapshot(source, expected, strict_stat=True):
            self.journal.mark_conflict(operation["operation_uuid"], "source identity changed before unlink")
            raise EvidenceError("published copy retained; changed source was not deleted")
        source.unlink()
        _fsync_directory(source.parent)
        if _exists(staging):
            self._unlink_verified_stage(staging, expected)

    def _finish_metadata(self, operation_uuid: str, metadata_commit: MetadataCommit) -> dict[str, Any]:
        operation = self.journal.get(operation_uuid)
        if operation["state"] == "metadata_done":
            return self.journal.mark_complete(operation_uuid)
        if operation["state"] == "complete":
            return operation
        if operation["state"] != "disk_done":
            raise FileOperationError(f"operation is {operation['state']}; metadata cannot be committed")
        evidence = self._load_evidence(operation)
        _, destination = self._paths(operation)
        if not _exists(destination) or not self._same_snapshot(destination, evidence["source_snapshot"], strict_stat=False):
            self.journal.mark_conflict(operation_uuid, "destination changed before metadata commit")
            raise EvidenceError("destination no longer matches move evidence")
        # Deliberately do not mark error on callback failure: disk_done is resumable.
        metadata_commit(operation)
        self.journal.mark_metadata_done(operation_uuid)
        return self.journal.mark_complete(operation_uuid)

    def recover(self, metadata_commit: MetadataCommit, *, library_uuid: str | None = None) -> list[dict[str, Any]]:
        """Resume only operations whose persisted path/hash evidence verifies."""
        results: list[dict[str, Any]] = []
        for operation in self.journal.list_unfinished(library_uuid=library_uuid):
            op_id = operation["operation_uuid"]
            try:
                if operation["state"] == "planned":
                    evidence = self._load_evidence(operation)
                    self._finish_disk(operation, evidence)
                result = self._finish_metadata(op_id, metadata_commit)
                results.append({"operation_uuid": op_id, "state": result["state"]})
            except Exception as exc:
                # Preserve planned/disk_done for retry, or expose terminal conflict.
                try:
                    state = self.journal.get(op_id)["state"]
                except Exception:
                    state = "unknown"
                results.append({"operation_uuid": op_id, "state": state, "error": str(exc)})
        return results
