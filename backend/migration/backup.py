"""Verified file snapshots and isolated restore. Never overwrites live user data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from .preflight import scan_manifest


class SnapshotError(RuntimeError):
    pass


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _entries(manifest):
    return [(f['path'], f['bytes'], f['sha256'], f['status']) for f in manifest['files']]


def _separate(source, destination):
    if destination == source or source in destination.parents or destination in source.parents:
        raise SnapshotError('Source and destination must be separate directories')


def _safe_relative(value):
    path = PurePosixPath(value)
    if not isinstance(value, str) or not value or path.is_absolute() or any(
            part in ('', '.', '..') or ':' in part or '\\' in part for part in path.parts):
        raise SnapshotError('Unsafe snapshot path')
    return Path(*path.parts)


def verify_snapshot(snapshot):
    snapshot = Path(snapshot).resolve()
    manifest = json.loads((snapshot / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or not manifest.get('ready'):
        raise SnapshotError('Snapshot manifest is not ready')
    payload = snapshot / 'files'
    if payload.is_symlink() or not payload.is_dir():
        raise SnapshotError('Invalid snapshot payload')
    expected = set()
    for entry in manifest['files']:
        rel = _safe_relative(entry['path'])
        if entry['path'] in expected:
            raise SnapshotError('Duplicate manifest path')
        expected.add(entry['path'])
        item = payload / rel
        if any(p.is_symlink() for p in (item, *item.parents) if p != snapshot.parent):
            raise SnapshotError('Snapshot contains a symbolic link')
        if not item.is_file() or item.stat().st_size != entry['bytes'] or _hash(item) != entry['sha256']:
            raise SnapshotError('Snapshot content mismatch: ' + entry['path'])
    actual = set()
    for item in payload.rglob('*'):
        if item.is_symlink():
            raise SnapshotError('Snapshot contains a symbolic link')
        if item.is_file(): actual.add(item.relative_to(payload).as_posix())
    if actual != expected:
        raise SnapshotError('Snapshot file set mismatch')
    return manifest


def create_snapshot(data_dir, destination):
    """Caller must stop cooperative writers; detects changed sources as another guard.

    This file snapshot is for legacy JSON/trash migration input, not a live SQLite
    backup. A database snapshot will use SQLite backup once that store is enabled.
    """
    source = Path(data_dir).absolute()
    dest = Path(destination).absolute()
    _separate(source, dest.resolve())
    if dest.exists(): raise SnapshotError('Destination already exists')
    before = scan_manifest(source)
    if not before['ready']: raise SnapshotError('Preflight issues block backup')
    source = source.resolve(strict=True)
    if any(f['path'].endswith(('.sqlite', '.sqlite-wal', '.db', '.db-wal')) for f in before['files']):
        raise SnapshotError('Use SQLite consistent backup for database files')
    required = sum(f['bytes'] for f in before['files']) + 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(dest.parent).free < required:
        raise SnapshotError('Insufficient snapshot space')
    stage = Path(tempfile.mkdtemp(prefix='cmv-snapshot-', dir=dest.parent))
    published = False
    try:
        payload = stage / 'files'
        payload.mkdir()
        for entry in before['files']:
            relative = _safe_relative(entry['path'])
            origin = source / relative
            if origin.is_symlink() or not origin.is_file():
                raise SnapshotError('Source changed during backup')
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
        (stage / 'manifest.json').write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf-8')
        verify_snapshot(stage)
        after = scan_manifest(source)
        if not after['ready'] or _entries(before) != _entries(after):
            raise SnapshotError('Source changed during backup; retry after stopping writers')
        # On Windows rename refuses an existing destination. No replacement is allowed.
        os.rename(stage, dest)
        published = True
        return verify_snapshot(dest)
    finally:
        if not published: shutil.rmtree(stage)


def restore_isolated(snapshot, destination):
    """Restore to an exclusively created directory, outside source and snapshot."""
    snapshot = Path(snapshot).resolve(strict=True)
    manifest = verify_snapshot(snapshot)
    dest = Path(destination).absolute()
    _separate(snapshot, dest.resolve())
    original = Path(manifest['dataDir']).resolve()
    _separate(original, dest.resolve())
    if dest.exists(): raise SnapshotError('Restore requires a new directory')
    dest.mkdir(parents=True, exist_ok=False)
    try:
        for entry in manifest['files']:
            relative = _safe_relative(entry['path'])
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot / 'files' / relative, target)
            if target.stat().st_size != entry['bytes'] or _hash(target) != entry['sha256']:
                raise SnapshotError('Restored file mismatch')
        restored = scan_manifest(dest)
        if not restored['ready'] or _entries(restored) != _entries(manifest):
            raise SnapshotError('Restore manifest mismatch')
        return restored
    except Exception:
        # Leave an incomplete isolated directory for inspection, never overwrite.
        raise


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('create')
    backup.add_argument('--data-dir', required=True)
    backup.add_argument('--destination', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--snapshot', required=True)
    restore.add_argument('--destination', required=True)
    args = parser.parse_args()
    result = create_snapshot(args.data_dir, args.destination) if args.command == 'create' else restore_isolated(args.snapshot, args.destination)
    print(json.dumps({'ok': True, 'files': len(result['files'])}))

if __name__ == '__main__': main()
