"""Preserve current post-cutover data before assessing a legacy downgrade."""
from pathlib import Path
import json
import hashlib

from .data_relocation import prepare_relocation_files
from .export_legacy import export_organization, organization_export_preview
from .relocation_cutover import _write_new_json
from ..storage.runtime_state import _safe_path


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def export_recovery_bundle(data_dir, destination):
    """Offline full material preservation; no activation or legacy startup claim."""
    source=Path(data_dir).absolute()
    destination=Path(destination).absolute()
    _safe_path(destination.parent,destination.name)
    if destination.exists() or source==destination or source in destination.parents or destination in source.parents:
        raise ValueError('Recovery export requires a new separate directory')
    if not destination.parent.is_dir():
        raise ValueError('Recovery export parent directory must exist')
    destination.mkdir()
    # This uses SQLite backup and checked file copies under the source writer guard.
    # The preserved material deliberately has no storage-state.json authority pointer.
    preserved=destination/'preserved-current-data'
    receipt=prepare_relocation_files(source,preserved)
    state=receipt['storage']
    if state['mode']!='sqlite':
        raise ValueError('Post-cutover recovery export requires SQLite authority')
    database=_safe_path(preserved,state['database'])
    review=organization_export_preview(database,state['profileUuid'])
    organization=None
    if review['organizationExportable']:
        organization=export_organization(database,state['profileUuid'],destination/'legacy-organization')
    report={'version':1,'sourceDataDir':str(source),'preservedData':str(preserved),
            'storageIdentity':state,'sourceManifest':receipt['sourceManifest'],
            'organizationExport':organization,'downgradeReview':review,
            'authorityPublished':False,'readyForLegacyStartup':False,
            'preservationScope':'all persistent files accepted by source manifest, plus consistent SQLite backups',
            'restoreRequirements':['Verify relocation-databases.json and database contents before restoring',
                'Restore the matching authority identity only into a verified isolated directory',
                'Reconcile absolute trash payload paths before opening a restored copy',
                'Do not replace current data with pre-cutover JSON backups'],
            'preservedFiles':[{'path':path.relative_to(preserved).as_posix(),'bytes':path.stat().st_size,
                               'sha256':_digest(path)}
                              for path in sorted(preserved.rglob('*')) if path.is_file() and path.name!='.cmv-writer.lock'],
            'retainedDomains':['settings','recent','manual safety decisions','trash payloads and state',
                               'operation recovery journals','media identities and metadata archives']}
    _write_new_json(destination/'recovery-export.json',report)
    return report
