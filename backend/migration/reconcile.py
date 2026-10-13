"""Read-only reconciliation of frozen organization records and shadow catalog."""
import json
import sqlite3
from collections import Counter
from pathlib import Path

from backend.migration.backup import verify_snapshot
from backend.migration.import_legacy import _manifest_identity, _pointer
from backend.storage.database import initialize_schema
from backend.storage.organization import OrganizationRepository


def reconcile_organization(snapshot, database, imported):
    snapshot, database = Path(snapshot).absolute(), Path(database).absolute()
    manifest = verify_snapshot(snapshot)
    source_hash = _manifest_identity(manifest)
    blockers = []
    def block(code, detail):
        blockers.append({'code': code, 'detail': detail})
    if imported.get('sourceManifestHash') != source_hash:
        block('source_manifest_mismatch', 'Import report does not identify this snapshot')
    if database.is_symlink() or not database.is_file():
        raise ValueError('Existing shadow catalog required')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        initialize_schema(connection)
        connection.execute('BEGIN')
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            block('foreign_key_violation', 'Catalog foreign key check failed')
        owner = connection.execute('SELECT l.id AS library_id,p.id AS profile_id FROM library l '
                                   'JOIN local_profile p ON p.library_id=l.id WHERE l.uuid=? AND p.uuid=?',
                                   (imported.get('libraryUuid'), imported.get('profileUuid'))).fetchone()
        if owner is None:
            block('owner_mismatch', 'Import Library/Profile ownership is absent')
        rows = connection.execute('SELECT * FROM migration_mapping WHERE source_hash=?', (source_hash,)).fetchall()
        actual = {(row['source_kind'], row['source_key']): row for row in rows}
        mappings = imported.get('mappings', [])
        counts = Counter()
        seen = set()
        tables = {'asset': 'media_asset', 'root': 'library_root', 'collection': 'collection',
                  'tag_assignment': 'tag'}
        for entry in mappings:
            key = (entry.get('sourceKind'), entry.get('sourceKey'))
            if key in seen:
                block('duplicate_report_mapping', str(key))
            seen.add(key)
            counts[entry.get('outcome')] += 1
            row = actual.get(key)
            if row is None or row['target_uuid'] != entry.get('targetUuid') or row['target_kind'] != entry.get('targetKind'):
                block('mapping_mismatch', str(key))
                continue
            if (entry.get('outcome') == 'quarantined') != (row['outcome'] == 'quarantined'):
                block('mapping_outcome_mismatch', str(key))
            table = tables.get(row['target_kind'])
            if table and not connection.execute(f'SELECT 1 FROM {table} WHERE uuid=?', (row['target_uuid'],)).fetchone():
                block('mapped_target_missing', str(key))
            if row['outcome'] == 'quarantined':
                block('unresolved_quarantine', str(key))
        for key, row in actual.items():
            if row['source_kind'] not in ('catalog', 'legacy-trash') and key not in seen:
                block('report_omitted_mapping', str(key))
        if {name: counts[name] for name in ('mapped', 'duplicate', 'quarantined')} != imported.get('counts'):
            block('report_count_mismatch', 'Mapping counts do not match report totals')
        job = connection.execute('SELECT state FROM migration_job WHERE uuid=? AND source_hash=?',
                                 (imported.get('jobUuid'), source_hash)).fetchone()
        if job is None or job['state'] != 'reconciled':
            block('import_not_reconciled', 'Persisted import job is not reconciled')
    finally:
        connection.close()
    try:
        projection = OrganizationRepository(database).read_projection(imported['profileUuid'])
    except Exception as error:
        block('projection_unavailable', str(error))
        projection = None
    compared = 0
    settings_path = snapshot / 'files' / 'settings.json'
    if projection is not None and settings_path.exists():
        settings = json.loads(settings_path.read_text('utf-8'))
        if not isinstance(settings, dict):
            block('source_domain_invalid', 'settings.json')
        else:
            source_categories = settings.get('fav_cats', [])
            if not isinstance(source_categories, list):
                block('source_domain_invalid', 'settings.json#/fav_cats')
            else:
                target_categories = {category['uuid']: category['name']
                                     for category in projection['cats']}
                for index, name in enumerate(source_categories):
                    source_key = f'settings.json#/fav_cats/{index}'
                    compared += 1
                    mapping = actual.get(('settings.json', source_key))
                    if mapping is None or mapping['target_kind'] != 'collection' or \
                            mapping['outcome'] == 'quarantined':
                        block('source_record_unmapped', source_key)
                        continue
                    target_name = target_categories.get(mapping['target_uuid'])
                    if not isinstance(name, str) or target_name != name:
                        block('source_projection_mismatch', source_key)
    if projection is not None:
        for filename, domain in [('favorites.json', 'favorites'), ('favorites.seq.json', 'seq'),
                                 ('tags.json', 'tags'), ('marks.json', 'marks')]:
            path = snapshot / 'files' / filename
            if not path.exists():
                continue
            document = json.loads(path.read_text('utf-8'))
            if not isinstance(document, dict):
                block('source_domain_invalid', filename)
                continue
            for locator, value in document.items():
                source_key = filename + '#/' + _pointer(locator)
                mapping = actual.get((filename, source_key))
                compared += 1
                if mapping is None:
                    block('source_record_unmapped', source_key)
                    continue
                if mapping['outcome'] == 'quarantined':
                    continue
                target = projection[domain].get(locator)
                try:
                    if domain == 'favorites':
                        time = value.get('t') if isinstance(value, dict) else value
                        category = str(value.get('cat') or '默认') if isinstance(value, dict) else '默认'
                        equal = target is not None and int(target['t']) == int(time) and target.get('cat', '默认') == category
                    elif domain == 'seq':
                        equal = target == value and type(value) is int
                    elif domain == 'tags':
                        equal = isinstance(value, list) and target is not None and set(target) == set(value)
                    else:
                        equal = target is not None and target['state'] == value['state'] and int(target['updated']) == int(value.get('updated') or 0)
                except (ValueError, TypeError, KeyError):
                    equal = False
                if not equal:
                    block('source_projection_mismatch', source_key)
    if _manifest_identity(verify_snapshot(snapshot)) != source_hash:
        block('snapshot_changed', 'Snapshot changed during reconciliation')
    # Domain acceptance is not a substitute for trash/retained-domain cutover.
    return {'sourceManifestHash': source_hash, 'comparedSourceRecords': compared,
            'organizationReady': not blockers, 'readyToSwitch': False, 'blockers': blockers,
            'pendingDomains': ['trash', 'retained_compatibility', 'source_freeze'],
            'compatibleRetained': imported.get('compatibleRetained', [])}
