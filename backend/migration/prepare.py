"""Prepare and reconcile an isolated migration without publishing live authority."""
import json
import os
import sqlite3
import uuid
import tempfile
from pathlib import Path

from backend.migration.backup import create_snapshot, verify_snapshot
from backend.migration.guard import DataWriterGuard
from backend.migration.import_legacy import import_snapshot, _manifest_identity
from backend.migration.import_trash import import_trash
from backend.migration.reconcile import reconcile_organization
from backend.migration.reconcile_trash import reconcile_trash
from backend.migration.source_gate import verify_live_source
from backend.migration.run_state import MigrationRuns
from backend.storage.runtime_state import load_state
from backend.operations.journal import initialize_journal
from backend.operations.trash import CatalogTrash


def prepare_migration(data_dir, destination):
    source, destination = Path(data_dir).absolute(), Path(destination).absolute()
    for path in (source, destination, *source.parents, *destination.parents):
        if path.is_symlink() or (path.exists() and getattr(path.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Preparation paths cannot contain links')
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Preparation requires a separate isolated destination')
    if not source.is_dir() or destination.exists():
        raise ValueError('Existing source and new destination required')
    destination.mkdir(parents=True)
    run_uuid = str(uuid.uuid4())
    registry = MigrationRuns(destination / 'migration-runs.sqlite')
    registry.initialize()
    result = {'runUuid': run_uuid, 'readyForActivation': False, 'authorityPublished': False,
              'sourceDataDir': str(source), 'destination': str(destination)}
    created = False
    try:
        with DataWriterGuard(source):
            if load_state(source)['mode'] != 'legacy-json':
                raise ValueError('This preparation applies only to legacy JSON authority')
            snapshot = destination / 'snapshot'
            manifest = create_snapshot(source, snapshot)
            database = destination / 'catalog.sqlite'
            registry.create(run_uuid, {'sourceManifestHash': _manifest_identity(manifest),
                                      'sourceDataDir': str(source), 'snapshot': str(snapshot),
                                      'shadowDatabase': str(database)})
            created = True
            registry.transition(run_uuid, 'preflight', 'backed_up', {'snapshotVerified': True,
                                                                   'sourceManifestHash': _manifest_identity(manifest)})
            registry.transition(run_uuid, 'backed_up', 'importing', {'shadowDatabase': str(database)})
            organization = import_snapshot(snapshot, database)
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                roots = connection.execute('SELECT path FROM library_root ORDER BY id').fetchall()
            finally:
                connection.close()
            if not roots:
                raise ValueError('No registered root for trash ownership')
            journal = destination / 'operation-journal.sqlite'
            initialize_journal(journal)
            service = CatalogTrash(database, organization['libraryUuid'], roots[0][0],
                                   destination / 'trash', journal, destination / 'operation-evidence')
            service.initialize()
            trash = import_trash(snapshot, database, service.store, destination / 'trash',
                                 library_uuid=organization['libraryUuid'], profile_uuid=organization['profileUuid'])
            organization_check = reconcile_organization(snapshot, database, organization)
            trash_check = reconcile_trash(snapshot, database, service.store, destination / 'trash', trash,
                                          library_uuid=organization['libraryUuid'], profile_uuid=organization['profileUuid'])
            source_check = verify_live_source(snapshot, source)
            ready = organization_check['organizationReady'] and trash_check['trashReady'] and source_check['sourceUnchanged']
            result.update({'libraryUuid': organization['libraryUuid'], 'profileUuid': organization['profileUuid'],
                           'organizationImport': organization, 'trashImport': trash,
                           'organizationCheck': organization_check, 'trashCheck': trash_check,
                           'sourceCheck': source_check, 'readyForActivation': ready,
                           'requiresFinalRecheckAndWriterLock': True})
            registry.transition(run_uuid, 'importing', 'reconciled' if ready else 'failed',
                                {'organizationReady': organization_check['organizationReady'],
                                 'trashReady': trash_check['trashReady'], 'sourceUnchanged': source_check['sourceUnchanged']})
            result['state'] = registry.read(run_uuid)['state']
    except Exception as error:
        result.update({'state': 'failed', 'error': str(error)})
        if created:
            checkpoint = registry.read(run_uuid)['state']
            if checkpoint not in ('failed', 'active', 'rollback_exported'):
                registry.transition(run_uuid, checkpoint, 'failed', {'error': str(error)})
    with (destination / 'preparation-report.json').open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return result


def resume_preparation(destination, run_uuid):
    """Resume frozen preparation only; never adopt new source or publish authority."""
    destination = Path(destination).absolute()
    registry = MigrationRuns(destination / 'migration-runs.sqlite')
    run = registry.read(run_uuid)
    identity = run['identity']
    source = Path(identity['sourceDataDir']).absolute()
    snapshot = destination / 'snapshot'
    database = destination / 'catalog.sqlite'
    if identity['snapshot'] != str(snapshot) or identity['shadowDatabase'] != str(database):
        raise ValueError('Preparation target differs from persisted identity')
    result = {'runUuid': run_uuid, 'sourceDataDir': str(source), 'destination': str(destination),
              'readyForActivation': False, 'authorityPublished': False}
    with DataWriterGuard(destination), DataWriterGuard(source):
        run = registry.read(run_uuid)
        checkpoint = run['checkpoint'] if run['state'] == 'failed' else run['state']
        if checkpoint not in ('preflight', 'backed_up', 'importing', 'reconciled'):
            raise ValueError('Preparation cannot resume after installation begins')
        manifest = verify_snapshot(snapshot)
        if _manifest_identity(manifest) != identity['sourceManifestHash']:
            raise ValueError('Frozen snapshot identity changed')
        if load_state(source)['mode'] != 'legacy-json' or not verify_live_source(snapshot, source)['sourceUnchanged']:
            raise ValueError('Source changed; create a new preparation instead')
        if run['state'] == 'failed':
            registry.transition(run_uuid, 'failed', checkpoint, {'resumeFrozenPreparation': True})
        if checkpoint == 'preflight':
            registry.transition(run_uuid, 'preflight', 'backed_up', {'snapshotVerified': True,
                                'sourceManifestHash': identity['sourceManifestHash']})
            checkpoint = 'backed_up'
        if checkpoint == 'backed_up':
            registry.transition(run_uuid, 'backed_up', 'importing', {'shadowDatabase': str(database)})
        try:
            organization = import_snapshot(snapshot, database)
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                root = connection.execute('SELECT path FROM library_root ORDER BY id LIMIT 1').fetchone()
            finally:
                connection.close()
            if root is None:
                raise ValueError('No registered root for trash ownership')
            journal = destination / 'operation-journal.sqlite'
            initialize_journal(journal)
            service = CatalogTrash(database, organization['libraryUuid'], root[0],
                                   destination / 'trash', journal, destination / 'operation-evidence')
            service.initialize()
            trash = import_trash(snapshot, database, service.store, destination / 'trash',
                                 library_uuid=organization['libraryUuid'], profile_uuid=organization['profileUuid'])
            org_check = reconcile_organization(snapshot, database, organization)
            trash_check = reconcile_trash(snapshot, database, service.store, destination / 'trash', trash,
                                          library_uuid=organization['libraryUuid'], profile_uuid=organization['profileUuid'])
            source_check = verify_live_source(snapshot, source)
            ready = org_check['organizationReady'] and trash_check['trashReady'] and source_check['sourceUnchanged']
            result.update(libraryUuid=organization['libraryUuid'], profileUuid=organization['profileUuid'],
                          organizationImport=organization, trashImport=trash, organizationCheck=org_check,
                          trashCheck=trash_check, sourceCheck=source_check, readyForActivation=ready,
                          requiresFinalRecheckAndWriterLock=True)
            current = registry.read(run_uuid)['state']
            target = 'reconciled' if ready else 'failed'
            if current != target:
                registry.transition(run_uuid, current, target, {'organizationReady': org_check['organizationReady'],
                                    'trashReady': trash_check['trashReady'], 'sourceUnchanged': source_check['sourceUnchanged']})
            result['state'] = target
        except Exception as error:
            current = registry.read(run_uuid)['state']
            if current != 'failed':
                registry.transition(run_uuid, current, 'failed', {'error': str(error)})
            result.update(state='failed', error=str(error))
        descriptor, name = tempfile.mkstemp(prefix='.preparation-report-', dir=destination)
        staging = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, destination / 'preparation-report.json')
        finally:
            staging.unlink(missing_ok=True)
    return result
