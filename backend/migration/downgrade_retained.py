"""Build isolated legacy downgrade materials from a verified current recovery bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from .backup import _safe_relative, SnapshotError
from .recovery_restore import verify_recovery_bundle
from .recovery_export import _digest
from ..storage.runtime_state import _safe_path


_ORG_FILES = ('favorites.json', 'favorites.seq.json', 'tags.json', 'marks.json')
_OPTIONAL_RETAINED = ('recent.json', 'nsfw_manual.json', 'nsfw_index.json')


def _reject_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise SnapshotError('Downgrade paths must not contain links or reparse points')


def _read_json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapshotError(f'Invalid {label}: {error}') from error


def _verify_organization_export(bundle: Path, recovery_report: dict) -> tuple[Path | None, dict]:
    declared = recovery_report.get('organizationExport')
    if declared is None:
        return None, {'valid': False, 'reason': 'Current organization export is unavailable'}
    root = _safe_path(bundle, 'legacy-organization')
    manifest_path = _safe_path(root, 'export-manifest.json')
    manifest = _read_json(manifest_path, 'organization export manifest')
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or manifest.get('scope') != 'organization-only':
        raise SnapshotError('Invalid organization export manifest')
    expected = {}
    for entry in manifest.get('files', []):
        if not isinstance(entry, dict):
            raise SnapshotError('Invalid organization export entry')
        relative = _safe_relative(entry.get('path')).as_posix()
        if relative in expected:
            raise SnapshotError('Duplicate organization export path')
        item = _safe_path(root, relative)
        if not item.is_file() or item.stat().st_size != entry.get('bytes') or _digest(item) != entry.get('sha256'):
            raise SnapshotError('Organization export mismatch: ' + relative)
        expected[relative] = entry
    actual = {item.relative_to(root).as_posix() for item in root.rglob('*')
              if item.is_file() and item != manifest_path}
    for item in root.rglob('*'):
        _safe_path(root, item.relative_to(root).as_posix())
    if actual != set(expected):
        raise SnapshotError('Organization export file set differs from manifest')
    if not set(_ORG_FILES + ('settings-merge.json',)).issubset(expected):
        raise SnapshotError('Organization export is missing required legacy documents')
    if not isinstance(declared, dict) or declared.get('files') != manifest.get('files'):
        raise SnapshotError('Organization export differs from recovery report')
    return root, manifest


def _trash_blockers(preserved: Path, entries: dict) -> list[dict]:
    """Conservatively keep startup blocked while trash paths/state need reconciliation."""
    blockers = []
    for relative in entries:
        if relative == 'trash-state.sqlite' or relative.startswith('trash/'):
            blockers.append({'domain': 'trash', 'reason': 'Trash state or payload requires legacy path reconciliation'})
            break
    database = next((preserved / name for name in ('catalog.sqlite', 'library.sqlite') if (preserved / name).is_file()), None)
    if database is not None:
        try:
            import sqlite3
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'trash_item' in tables:
                    states = [row[0] for row in db.execute('SELECT DISTINCT state FROM trash_item')]
                    if states:
                        blockers.append({'domain': 'trash', 'reason': 'Trash records require state and payload reconciliation',
                                         'states': sorted(str(state) for state in states)})
        except (OSError, sqlite3.Error) as error:
            blockers.append({'domain': 'trash', 'reason': f'Trash state could not be inspected: {error}'})
    return blockers


def _project_current_recent(report: dict, preserved: Path) -> tuple[list[dict] | None, dict, list[dict]]:
    """Project every current SQLite recent row using the legacy loader's wire shape."""
    import sqlite3
    identity = report.get('storageIdentity')
    if not isinstance(identity, dict) or not isinstance(identity.get('database'), str):
        return None, {'exported': False, 'reason': 'Recovery report has no database identity'}, []
    database = _safe_path(preserved, identity['database'])
    if not database.is_file():
        return None, {'exported': False, 'reason': 'Preserved current catalog is missing'}, []
    try:
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            found = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recent_view'").fetchone()
            if not found:
                return None, {'exported': False, 'reason': 'Current recent_view authority is absent'}, []
            profile = db.execute('SELECT id FROM local_profile WHERE uuid=?',
                                 (identity.get('profileUuid'),)).fetchone()
            if profile is None:
                return None, {'exported': False, 'reason': 'Recovery profile identity is absent'}, []
            count = db.execute('SELECT COUNT(*) FROM recent_view WHERE profile_id=?', (profile['id'],)).fetchone()[0]
            from ..storage.recent import RecentRepository
            rows = RecentRepository(database).read(identity.get('profileUuid'), limit=count)
            lifecycle_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_lifecycle'").fetchone()
            lifecycle = ({row[0] for row in db.execute(
                "SELECT a.uuid FROM asset_lifecycle l JOIN media_asset a ON a.id=l.asset_id "
                "AND a.library_id=l.library_id")} if lifecycle_table else set())
    except Exception as error:
        return None, {'exported': False, 'reason': f'Current recent projection failed: {error}'}, []
    output = []
    losses = []
    seen_paths = set()
    deleted_identity_count = 0
    for row in rows:
        try:
            from .export_legacy import _legacy_seconds
            timestamp = _legacy_seconds(row['time'])
        except (TypeError, ValueError, OverflowError):
            losses.append({'domain': 'recent', 'path': row.get('path'),
                           'reason': 'Recent timestamp cannot be represented as a legacy integer'})
            continue
        path = str(row['path'])
        if path in seen_paths:
            losses.append({'domain': 'recent', 'path': path,
                           'reason': 'Multiple current media identities map to one legacy path'})
        seen_paths.add(path)
        deleted = row.get('mediaUuid') in lifecycle
        if deleted:
            deleted_identity_count += 1
        # The legacy loader keeps missing paths; its recent view later hides any
        # path for which _pack_media cannot find a file. Preserve that behavior.
        output.append({'path': path, 'action': str(row.get('action') or ''), 'time': timestamp})
    if len(rows) > 200:
        losses.append({'domain': 'recent', 'reason': 'Current recent rows exceed the legacy 200-entry write limit',
                       'count': len(rows), 'legacyWriteLimit': 200})
    if rows:
        losses.append({'domain': 'recent',
                       'reason': 'Legacy recent view filters by filesystem path; path existence was not inspected, so missing files stay in JSON and are hidden on view, while a replacement at the same path can resolve to a different identity',
                       'lifecycleDeletedIdentityCount': deleted_identity_count})
    return output, {'exported': True, 'count': len(output), 'sourceCount': count,
                    'order': 'sequence descending (newest first)',
                    'filesystemPresenceChecked': False,
                    'lifecycleDeletedIdentityCount': deleted_identity_count,
                    'duplicatePathCount': sum(1 for item in losses if item.get('reason', '').startswith('Multiple current')),
                    'legacyWriteLimit': 200}, losses


def build_downgrade_materials(bundle, destination) -> dict:
    """Copy verified preserved-current-data and merge current organization categories.

    The output is a reviewable isolated candidate. It never declares legacy startup
    ready because SQLite identities/relations are not represented by legacy JSON.
    """
    bundle = Path(bundle).absolute()
    destination = Path(destination).absolute()
    _reject_links(destination)
    if destination.exists():
        raise SnapshotError('Downgrade destination must be new and empty')
    report, preserved, entries = verify_recovery_bundle(bundle)
    org_root, org_manifest = _verify_organization_export(bundle, report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.downgrade-retained-', dir=destination.parent))
    try:
        blockers = []
        losses = []
        output_names = (['settings.json'] if 'settings.json' in entries else []) + [
            name for name in ('nsfw_manual.json', 'nsfw_index.json') if name in entries]
        for relative in output_names:
            entry = entries[relative]
            source = _safe_path(preserved, relative)
            target = _safe_path(stage, relative)
            if not source.is_file() or source.stat().st_size != entry['bytes'] or _digest(source) != entry['sha256']:
                raise SnapshotError('Recovery material changed during downgrade: ' + relative)
            with source.open('rb') as reader, target.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            if target.stat().st_size != entry['bytes'] or _digest(target) != entry['sha256']:
                raise SnapshotError('Downgrade copy hash mismatch: ' + relative)
        settings_path = _safe_path(stage, 'settings.json')
        if 'settings.json' not in entries:
            blockers.append({'domain': 'settings', 'reason': 'Current preserved settings.json is missing'})
        else:
            settings = _read_json(settings_path, 'preserved settings.json')
            if not isinstance(settings, dict):
                raise SnapshotError('Preserved settings.json must contain an object')
            if org_root is not None:
                fragment = _read_json(_safe_path(org_root, 'settings-merge.json'), 'settings merge fragment')
                if not isinstance(fragment, dict) or not isinstance(fragment.get('fav_cats'), list):
                    raise SnapshotError('settings-merge.json fav_cats must be an array')
                settings['fav_cats'] = fragment['fav_cats']
                payload = (json.dumps(settings, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                with settings_path.open('wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                blockers.append({'domain': 'organization', 'reason': 'Latest categories unavailable; settings left unchanged'})

        missing_optional = [name for name in ('nsfw_manual.json', 'nsfw_index.json') if name not in entries]
        if missing_optional:
            losses.extend({'domain': name.removesuffix('.json'), 'reason': 'Optional preserved file is missing',
                           'file': name} for name in missing_optional)

        recent_rows, recent_projection, recent_losses = _project_current_recent(report, preserved)
        recent_projection['recentCurrentProjectionExported'] = recent_projection.pop('exported')
        if recent_rows is None:
            blockers.append({'domain': 'recent', 'reason': recent_projection['reason']})
        else:
            recent_path = _safe_path(stage, 'recent.json')
            payload = (json.dumps(recent_rows, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            with recent_path.open('wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            losses.extend(recent_losses)
            # Conversion and identity/path edge cases are explicit report entries.
            # RecentRepository has already enforced newest-first sequence order.
            if recent_projection.get('count', 0) > 200:
                losses.append({'domain': 'recent', 'count': recent_projection['count'],
                               'reason': 'Next legacy write can truncate entries beyond index 199'})
            if recent_projection.get('duplicatePathCount', 0):
                losses.append({'domain': 'recent', 'count': recent_projection['duplicatePathCount'],
                               'reason': 'Current media identities share legacy paths'})

        # Trash is exported by the integrating assembly; this component records
        # its separate reconciliation requirement without vetoing that export.
        requires_separate_projection = _trash_blockers(preserved, entries)
        # Advanced SQLite identities and relations have no legacy JSON equivalent,
        # but remain preserved in the verified source bundle.
        losses.append({'domain': 'advanced-identities',
                       'reason': 'Media UUIDs, revisions, metadata archives, and relationships remain SQLite-only; complete recovery bundle retained'})
        material_hashes = {}
        for path in sorted(item for item in stage.rglob('*') if item.is_file()):
            relative = path.relative_to(stage).as_posix()
            material_hashes[relative] = {'bytes': path.stat().st_size, 'sha256': _digest(path)}
        result = {
            'version': 1, 'sourceBundle': str(bundle), 'destination': str(destination),
            'sourceRecoveryVerified': True, 'sourcePreservedFileCount': len(entries),
            'optionalRetainedFilesMissing': missing_optional,
            'recentCurrentProjectionExported': recent_projection['recentCurrentProjectionExported'],
            'recentProjection': recent_projection,
            'readyForLegacyStartup': False, 'blockers': blockers,
            'losses': losses, 'requiresSeparateProjection': requires_separate_projection,
            'retainedDomains': ['settings', 'recent', 'nsfw_manual', 'nsfw_index'],
            'files': material_hashes,
        }
        _safe_path(stage, 'downgrade-materials.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if destination.exists():
            raise SnapshotError('Downgrade destination was created concurrently')
        stage.rename(destination)
        result['destination'] = str(destination)
        return result
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
