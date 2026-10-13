"""Read-only evidence check for a legacy-trash shadow before activation."""
import hashlib
import json
import sqlite3
from pathlib import Path

from backend.migration.backup import verify_snapshot
from backend.migration.import_legacy import _manifest_identity
from backend.migration.import_trash import (_payload_source, _EXTENSION_VERSION, _EXTENSION_CHECKSUM,
                                          _TRASH_VERSION, _TRASH_CHECKSUM)


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _open(path):
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Reconciliation paths cannot contain links')
    if not path.is_file():
        raise ValueError('Existing database required')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')
    return connection


def reconcile_trash(snapshot, database, trash_database, trash_dir, report, *, library_uuid, profile_uuid=None):
    snapshot, trash_dir = Path(snapshot).absolute(), Path(trash_dir).absolute()
    manifest = verify_snapshot(snapshot)
    source_hash = _manifest_identity(manifest)
    blockers = []
    def block(code, key):
        blockers.append({'code': code, 'key': key})
    if report.get('sourceHash') != source_hash:
        block('trash_manifest_mismatch', '')
    index_path = snapshot / 'files' / 'trash' / 'index.json'
    entries = json.loads(index_path.read_text('utf-8')) if index_path.exists() else {}
    if not isinstance(entries, dict):
        return {'trashReady': False, 'blockers': [{'code': 'invalid_trash_index', 'key': ''}]}
    catalog, trash = _open(database), _open(trash_database)
    try:
        for table, version, checksum in [('trash_schema', _TRASH_VERSION, _TRASH_CHECKSUM),
                                         ('legacy_trash_extension', _EXTENSION_VERSION, _EXTENSION_CHECKSUM)]:
            rows = trash.execute(f'SELECT version,checksum FROM {table}').fetchall()
            if len(rows) != 1 or rows[0]['version'] != version or rows[0]['checksum'] != checksum:
                block('trash_schema_mismatch', table)
        reported = report.get('mapped', []) + report.get('duplicates', [])
        report_keys = [row.get('key') for row in reported]
        if len(report_keys) != len(set(report_keys)) or set(report_keys) != set(entries):
            block('trash_report_not_closed', '')
        if report.get('blockers') or report.get('quarantined') or report.get('projectionConflicts'):
            block('trash_import_unresolved', '')
        for key, original in entries.items():
            entry_key = 'trash-index:' + key
            item = trash.execute('SELECT * FROM trash_item WHERE key=?', (key,)).fetchone()
            raw = trash.execute('SELECT * FROM legacy_trash_snapshot WHERE source_hash=? AND source_key=?',
                                (source_hash, entry_key)).fetchone()
            if item is None or raw is None:
                block('trash_entry_missing', key)
                continue
            if json.loads(raw['raw_entry_json']) != original:
                block('trash_original_snapshot_changed', key)
            if item['library_uuid'] != library_uuid or item['state'] != 'trashed':
                block('trash_scope_or_state_changed', key)
            if any(item[target] != original.get(source) for target, source in
                   [('name', 'name'), ('orig_path', 'orig'), ('size', 'size'), ('trashed_at', 't')]):
                block('trash_index_field_changed', key)
            payload = Path(item['payload_path']).absolute()
            try:
                payload.relative_to(trash_dir)
                if payload.parent != trash_dir or payload.is_symlink() or not payload.is_file():
                    raise ValueError('Invalid payload location')
                for candidate in (payload, *payload.parents):
                    if candidate.is_symlink() or getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400:
                        raise ValueError('Linked payload')
                source_payload = _payload_source(snapshot, manifest, key)
                if source_payload is None or _hash(payload) != _hash(source_payload):
                    raise ValueError('Payload hash differs')
            except (ValueError, OSError):
                block('trash_payload_mismatch', key)
                continue
            asset = catalog.execute('SELECT a.id,r.uuid,r.content_hash FROM media_asset a '
                                    'JOIN library l ON l.id=a.library_id '
                                    'JOIN media_revision r ON r.asset_id=a.id AND r.id=a.current_revision_id '
                                    'WHERE a.uuid=? AND l.uuid=?', (item['media_uuid'], library_uuid)).fetchone()
            if asset is None or asset['uuid'] != item['revision_uuid'] or asset['content_hash'] != _hash(payload):
                block('trash_catalog_identity_mismatch', key)
            elif original.get('fav') or original.get('tags') or original.get('mark'):
                profile = catalog.execute('SELECT p.id FROM local_profile p JOIN library l ON l.id=p.library_id '
                                          'WHERE p.uuid=? AND l.uuid=?', (profile_uuid, library_uuid)).fetchone()
                if profile is None:
                    block('trash_organization_profile_missing', key)
                else:
                    state = catalog.execute('SELECT * FROM user_media_state WHERE asset_id=? AND profile_id=?',
                                            (asset['id'], profile['id'])).fetchone()
                    favorite = original.get('fav')
                    if favorite and (state is None or state['favorite'] != 1 or
                                     state['favorite_category'] != favorite or state['favorite_time'] is not None):
                        block('trash_favorite_projection_mismatch', key)
                    if original.get('mark') and (state is None or state['mark_state'] != original['mark'] or
                                                 state['mark_updated'] is not None):
                        block('trash_mark_projection_mismatch', key)
                    tags = catalog.execute('SELECT t.name FROM tag_assignment ta JOIN tag t ON t.id=ta.tag_id '
                                           'WHERE ta.asset_id=? AND t.profile_id=?', (asset['id'], profile['id'])).fetchall()
                    if {row['name'] for row in tags} != {tag.strip() for tag in original.get('tags', [])}:
                        block('trash_tag_projection_mismatch', key)
                    if favorite:
                        membership = catalog.execute('SELECT 1 FROM collection_item ci JOIN collection c ON c.id=ci.collection_id '
                                                     'WHERE ci.asset_id=? AND c.profile_id=? AND c.name=?',
                                                     (asset['id'], profile['id'], favorite)).fetchone()
                        if membership is None:
                            block('trash_collection_membership_missing', key)
            for suffix, target in [('#asset', item['media_uuid']), ('#revision', item['revision_uuid'])]:
                mapped = catalog.execute("SELECT target_uuid FROM migration_mapping WHERE source_kind='legacy-trash' "
                                         'AND source_hash=? AND source_key=?', (source_hash, entry_key + suffix)).fetchone()
                if mapped is None or mapped['target_uuid'] != target:
                    block('trash_identity_mapping_missing', key)
    finally:
        catalog.close()
        trash.close()
    if _manifest_identity(verify_snapshot(snapshot)) != source_hash:
        block('snapshot_changed', '')
    return {'sourceManifestHash': source_hash, 'trashReady': not blockers, 'blockers': blockers,
            'sourceEntries': len(entries), 'legacyLoss': report.get('legacyLoss', [])}
