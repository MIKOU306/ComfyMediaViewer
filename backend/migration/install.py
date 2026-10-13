"""Exclusive installation with a persisted bundle identity; no pointer publication."""
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from backend.migration.guard import DataWriterGuard
from backend.migration.run_state import MigrationRuns
from backend.migration.source_gate import verify_live_source
from backend.migration.install_bundle import file_hash
from backend.migration.reconcile import reconcile_organization
from backend.migration.reconcile_trash import reconcile_trash
from backend.storage.runtime_state import load_state, validate_state


def _safe(directory, relative):
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or '\\' in relative or ':' in relative or any(
            part in ('', '.', '..') for part in relative.split('/')):
        raise ValueError('Invalid install relative path')
    path = directory.joinpath(*rel.parts)
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Install paths cannot contain links')
    return path


def install_migration(prepared, bundle_directory, *, after_install=None):
    prepared, bundle_directory = Path(prepared).absolute(), Path(bundle_directory).absolute()
    _safe(prepared, 'migration-runs.sqlite')
    manifest_path = _safe(bundle_directory, 'bundle.json')
    bundle_hash = file_hash(manifest_path)
    bundle = json.loads(manifest_path.read_text('utf-8'))
    registry = MigrationRuns(prepared / 'migration-runs.sqlite')
    run = registry.read(bundle['runUuid'])
    source = Path(run['identity']['sourceDataDir']).absolute()
    _safe(source, 'storage-state.json')
    if bundle.get('version') != 1 or bundle.get('sourceDataDir') != str(source) or \
            bundle.get('sourceManifestHash') != run['identity']['sourceManifestHash']:
        raise ValueError('Bundle does not belong to this migration source')
    evidence = {'bundleHash': bundle_hash, 'bundleDirectory': str(bundle_directory)}
    if run['state'] not in ('reconciled', 'switching'):
        raise ValueError('Migration is not ready for installation')
    if run['state'] == 'switching' and registry.latest_evidence(bundle['runUuid']) != evidence:
        raise ValueError('Installation retry differs from persisted bundle')
    files = bundle['files']
    if not isinstance(files, list) or len({entry['path'] for entry in files}) != len(files):
        raise ValueError('Invalid installation file list')
    for entry in files:
        origin = _safe(bundle_directory / 'files', entry['path'])
        _safe(source, entry['path'])
        if not origin.is_file() or origin.stat().st_size != entry['bytes'] or file_hash(origin) != entry['sha256']:
            raise ValueError('Installation payload changed')
    snapshot = Path(run['identity']['snapshot'])
    with DataWriterGuard(prepared), DataWriterGuard(source):
        # The state observed before acquiring the guards may already be stale.
        # Only the locked state may authorize source allowances or publication.
        run = registry.read(bundle['runUuid'])
        if run['state'] not in ('reconciled', 'switching'):
            raise ValueError('Migration is no longer ready for installation')
        if run['state'] == 'switching' and registry.latest_evidence(bundle['runUuid']) != evidence:
            raise ValueError('Installation retry differs from persisted bundle')
        if load_state(source)['mode'] != 'legacy-json':
            raise ValueError('Authority is already active; do not reinstall')
        source_check = verify_live_source(snapshot, source,
                                         installed_files=files if run['state'] == 'switching' else ())
        if not source_check['sourceUnchanged']:
            raise ValueError('Legacy source changed; refuse installation')
        preparation = json.loads(_safe(prepared, 'preparation-report.json').read_text('utf-8'))
        pointer = bundle['pointer']
        if preparation['runUuid'] != bundle['runUuid'] or pointer['libraryUuid'] != preparation['libraryUuid'] or \
                pointer['profileUuid'] != preparation['profileUuid']:
            raise ValueError('Bundle owner differs from reconciled preparation')
        catalog_copy = _safe(bundle_directory / 'files', pointer['database'])
        checked_org = reconcile_organization(snapshot, catalog_copy, preparation['organizationImport'])
        checked_trash = reconcile_trash(snapshot, catalog_copy, _safe(bundle_directory / 'files', 'trash-state.sqlite'),
                                        source / 'trash', preparation['trashImport'],
                                        library_uuid=pointer['libraryUuid'], profile_uuid=pointer['profileUuid'])
        if not checked_org['organizationReady'] or not checked_trash['trashReady']:
            raise ValueError('Install copies no longer reconcile with frozen records')
        if run['state'] == 'reconciled':
            # Record ownership before writing any live-directory artifacts.
            registry.transition(bundle['runUuid'], 'reconciled', 'switching', evidence)
        installed = []
        for entry in files:
            target = _safe(source, entry['path'])
            if target.exists():
                if not target.is_file() or target.stat().st_size != entry['bytes'] or file_hash(target) != entry['sha256']:
                    raise ValueError('Existing install target conflicts; never overwrite')
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Same-volume staging outside live data means a killed copy
                # leaves evidence but cannot contaminate the legacy inventory.
                if source.parent == source:
                    raise ValueError('A volume root cannot serve as the application data directory')
                descriptor, temporary = tempfile.mkstemp(prefix='.cmv-install-' + bundle['runUuid'] + '-',
                                                          dir=source.parent)
                try:
                    with os.fdopen(descriptor, 'wb') as writer, _safe(bundle_directory / 'files', entry['path']).open('rb') as reader:
                        shutil.copyfileobj(reader, writer)
                        writer.flush()
                        os.fsync(writer.fileno())
                    if file_hash(temporary) != entry['sha256']:
                        raise ValueError('Installation source changed during copy')
                    os.link(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            installed.append(entry['path'])
            if after_install:
                after_install(entry['path'])
        if file_hash(manifest_path) != bundle_hash:
            raise ValueError('Bundle changed during installation')
        final = verify_live_source(snapshot, source, installed_files=files)
        if not final['sourceUnchanged']:
            raise ValueError('Source changed during installation')
        validate_state(source, bundle['pointer'])
        return {'runUuid': bundle['runUuid'], 'state': 'switching', 'installedFiles': installed,
                'sourceVerified': True, 'authorityPublished': False, 'pointer': bundle['pointer']}
