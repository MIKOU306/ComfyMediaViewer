"""Activate only installed evidence; recover pointer publication without reinstalling."""
import json
from pathlib import Path
from backend.migration.guard import DataWriterGuard
from backend.migration.run_state import MigrationRuns
from backend.migration.install import _safe
from backend.migration.install_bundle import file_hash
from backend.migration.source_gate import verify_live_source
from backend.storage.runtime_state import load_state, publish_state


def activate_migration(prepared, bundle_directory, *, after_publication=None):
    prepared, bundle_directory = Path(prepared).absolute(), Path(bundle_directory).absolute()
    manifest = _safe(bundle_directory, 'bundle.json')
    bundle = json.loads(manifest.read_text('utf-8'))
    registry = MigrationRuns(_safe(prepared, 'migration-runs.sqlite'))
    run = registry.read(bundle['runUuid'])
    source = Path(run['identity']['sourceDataDir']).absolute()
    expected = {**bundle['pointer'], 'migrationRunUuid': bundle['runUuid']}
    evidence = {'bundleHash': file_hash(manifest), 'bundleDirectory': str(bundle_directory)}
    with DataWriterGuard(prepared), DataWriterGuard(source):
        run = registry.read(bundle['runUuid'])
        current = load_state(source)
        if run['state'] == 'active':
            if current != expected:
                raise ValueError('Active migration authority differs from its pointer')
            return {'state': 'active', 'recovered': False, 'pointer': current}
        if run['state'] != 'switching' or registry.latest_evidence(bundle['runUuid']) != evidence:
            raise ValueError('Installed migration evidence does not match activation')
        recovered = current['mode'] == 'sqlite'
        if recovered:
            if current != expected:
                raise ValueError('Existing authority belongs to another migration')
        else:
            if bundle['sourceManifestHash'] != run['identity']['sourceManifestHash'] or bundle['sourceDataDir'] != str(source):
                raise ValueError('Activation source differs')
            for entry in bundle['files']:
                target = _safe(source, entry['path'])
                if not target.is_file() or target.stat().st_size != entry['bytes'] or file_hash(target) != entry['sha256']:
                    raise ValueError('Installed file changed before activation')
            checked = verify_live_source(run['identity']['snapshot'], source, installed_files=bundle['files'])
            if not checked['sourceUnchanged']:
                raise ValueError('Legacy source changed before activation')
            publish_state(source, expected)
            if after_publication:
                after_publication()
        registry.transition(bundle['runUuid'], 'switching', 'active', {'pointer': expected, **evidence})
        return {'state': 'active', 'recovered': recovered, 'pointer': load_state(source)}
