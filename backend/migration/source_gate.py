"""Recheck frozen legacy files immediately before activation; no source writes."""
from pathlib import Path

from backend.migration.backup import verify_snapshot
from backend.migration.import_legacy import _manifest_identity
from backend.migration.preflight import scan_manifest


def verify_live_source(snapshot, data_dir, *, installed_files=()):
    manifest = verify_snapshot(snapshot)
    source = Path(data_dir).absolute()
    blockers = []
    if source != Path(manifest['dataDir']).absolute():
        blockers.append({'code': 'source_directory_mismatch', 'path': str(source)})
    current = scan_manifest(source)
    expected = {row['path']: row for row in manifest['files']}
    actual = {row['path']: row for row in current['files']}
    installed = {row['path']: row for row in installed_files}
    for path in sorted(set(expected) | set(actual)):
        before, after = expected.get(path), actual.get(path)
        if before is None:
            owned = installed.get(path)
            if owned is None or any(owned[field] != after[field] for field in ('bytes', 'sha256')) or after['status'] != 'ok':
                blockers.append({'code': 'source_file_added', 'path': path})
        elif after is None:
            blockers.append({'code': 'source_file_missing', 'path': path})
        elif any(before[field] != after[field] for field in ('bytes', 'sha256', 'kind')) or after['status'] != 'ok':
            blockers.append({'code': 'source_file_changed', 'path': path})
    if not current['ready']:
        blockers.append({'code': 'source_preflight_failed', 'issues': current['issues']})
    if _manifest_identity(verify_snapshot(snapshot)) != _manifest_identity(manifest):
        blockers.append({'code': 'snapshot_changed_during_source_check'})
    # Persistent materials not converted to SQL remain in their original directory.
    migrated = {'favorites.json', 'favorites.seq.json', 'tags.json', 'marks.json'}
    retained = [{'path': row['path'], 'bytes': row['bytes'], 'sha256': row['sha256'],
                 'authority': 'retained_file', 'kind': row['kind']}
                for row in manifest['files'] if row['path'] not in migrated]
    return {'sourceManifestHash': _manifest_identity(manifest), 'sourceUnchanged': not blockers,
            'retainedFilesVerified': not blockers, 'retainedFiles': retained, 'blockers': blockers,
            'requiresWriterLockThroughPublication': True,
            'historicalExecutablesMustRemainStopped': True}
