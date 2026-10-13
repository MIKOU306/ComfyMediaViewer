"""Build standalone installation evidence outside the live source directory."""
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from backend.migration.guard import DataWriterGuard
from backend.migration.run_state import MigrationRuns
from backend.migration.reconcile import reconcile_organization
from backend.migration.reconcile_trash import reconcile_trash
from backend.migration.sqlite_backup import backup_database


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def build_install_bundle(prepared, destination):
    prepared, destination = Path(prepared).absolute(), Path(destination).absolute()
    for candidate in (prepared, destination, *prepared.parents, *destination.parents):
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Installation preparation cannot contain links')
    if destination.exists():
        raise ValueError('Installation bundle must use a new directory')
    with DataWriterGuard(prepared):
        report = json.loads((prepared / 'preparation-report.json').read_text('utf-8'))
        registry = MigrationRuns(prepared / 'migration-runs.sqlite')
        run = registry.read(report['runUuid'])
        source = Path(run['identity']['sourceDataDir']).absolute()
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError('Bundle must remain separate from live data')
        if run['state'] != 'reconciled':
            raise ValueError('Migration has not passed reconciliation')
        snapshot = Path(run['identity']['snapshot'])
        catalog = Path(run['identity']['shadowDatabase'])
        organization = reconcile_organization(snapshot, catalog, report['organizationImport'])
        trash = reconcile_trash(snapshot, catalog, prepared / 'trash-state.sqlite', prepared / 'trash',
                                 report['trashImport'], library_uuid=report['libraryUuid'],
                                 profile_uuid=report['profileUuid'])
        if not organization['organizationReady'] or not trash['trashReady']:
            raise ValueError('Shadow contents no longer pass reconciliation')
        destination.mkdir(parents=True)
        payload = destination / 'files'
        payload.mkdir()
        targets = [('catalog.sqlite', 'libraries/' + report['libraryUuid'] + '/catalog.sqlite'),
                   ('trash-state.sqlite', 'trash-state.sqlite'),
                   ('operation-journal.sqlite', 'operation-journal.sqlite')]
        for name, relative in targets:
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            backup_database(prepared / name, target)
        # Only the standalone destination copy receives final runtime locators.
        trash_copy = payload / 'trash-state.sqlite'
        db = sqlite3.connect(str(trash_copy))
        try:
            rows = db.execute('SELECT key,payload_path FROM trash_item').fetchall()
            for key, old_path in rows:
                old_path = Path(old_path).absolute()
                if old_path.parent != prepared / 'trash' or old_path.is_symlink():
                    raise ValueError('Shadow trash payload is outside its owned directory')
                relative = 'trash/' + old_path.name
                target = payload / relative
                target.parent.mkdir(exist_ok=True)
                with old_path.open('rb') as reader, target.open('xb') as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                if file_hash(old_path) != file_hash(target):
                    raise ValueError('Trash payload changed while staging')
                db.execute('UPDATE trash_item SET payload_path=? WHERE key=?', (str(source / relative), key))
            db.commit()
        finally:
            db.close()
        files = []
        for path in sorted(payload.rglob('*')):
            if path.is_file():
                with path.open('r+b') as stream:
                    os.fsync(stream.fileno())
                files.append({'path': path.relative_to(payload).as_posix(), 'bytes': path.stat().st_size,
                              'sha256': file_hash(path)})
        bundle = {'version': 1, 'runUuid': report['runUuid'], 'sourceDataDir': str(source),
                  'sourceManifestHash': run['identity']['sourceManifestHash'], 'files': files,
                  'pointer': {'version': 1, 'mode': 'sqlite', 'libraryUuid': report['libraryUuid'],
                              'profileUuid': report['profileUuid'], 'database': targets[0][1]},
                  'authorityPublished': False}
        with (destination / 'bundle.json').open('x', encoding='utf-8') as stream:
            json.dump(bundle, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        return bundle
