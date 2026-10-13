"""Export current organizational state for an explicit, isolated downgrade."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from decimal import Decimal, InvalidOperation

from ..storage.organization import OrganizationRepository
from .backup import SnapshotError


def _legacy_seconds(value, *, nonzero=False):
    if value is None or isinstance(value, bool):
        raise ValueError('Unknown historical favorite time' if nonzero else 'Unknown mark time')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError('Time is not a finite integer')
        result = int(number)
    except (InvalidOperation, OverflowError) as error:
        raise ValueError('Time is not a finite integer') from error
    if nonzero and result == 0:
        raise ValueError('Zero favorite time')
    return result


def organization_export_preview(database, profile_uuid):
    """Read-only report; never claims a complete downgrade is ready."""
    projection = OrganizationRepository(Path(database).absolute()).read_projection(profile_uuid)
    return _projection_export_preview(projection)


def _projection_export_preview(projection):
    blockers = []
    for domain, field, nonzero in [('favorites','t',True),('marks','updated',False)]:
        for path, record in projection[domain].items():
            try:
                _legacy_seconds(record[field],nonzero=nonzero)
            except (ValueError, KeyError) as error:
                blockers.append({'domain':domain,'path':path,'reason':str(error)})
    return {'scope':'organization-only','organizationExportable':not blockers,
            'readyForLegacyStartup':False,'blockers':blockers,
            'counts':{name:len(projection[name]) for name in ['favorites','tags','marks','cats']},
            'requiresSeparateRecovery':['settings','recent','nsfw_manual','trash'],
            'notRepresentedInLegacy':['media UUID and revisions','raw metadata archives',
                                      'generation projections','workflow and model identities','creation relations'],
            'requiredPreservation':'Keep the complete SQLite database and archived materials; this export cannot replace them.'}


def export_organization(database, profile_uuid, destination):
    """Never overwrite legacy authority or copy a pre-cutover stale backup.

    Only organization files are exported. Settings here is a merge fragment;
    callers must reconcile retained settings/NSFW/recent/trash separately.
    """
    database = Path(database).absolute()
    destination = Path(destination).absolute()
    for candidate in (destination, *destination.parents):
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400):
            raise SnapshotError('Export destination must not contain links')
    if destination.exists() or destination == database or destination in database.parents:
        raise SnapshotError('Export requires a new isolated directory')
    projection = OrganizationRepository(database).read_projection(profile_uuid)
    preview = _projection_export_preview(projection)
    if preview['blockers']:
        raise SnapshotError(preview['blockers'][0]['reason'] + ' cannot be represented by the legacy loader')
    # Legacy loaders use integer Unix seconds. Reject incompatible values
    # rather than quietly lose state on the first old-program startup.
    for record in projection['favorites'].values():
        record['t'] = _legacy_seconds(record['t'],nonzero=True)
    for record in projection['marks'].values():
        record['updated'] = _legacy_seconds(record['updated'])
    documents = {
        'favorites.json': projection['favorites'],
        'favorites.seq.json': projection['seq'],
        'tags.json': projection['tags'],
        'marks.json': projection['marks'],
        'settings-merge.json': {'fav_cats': [category['name'] for category in projection['cats']]},
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.organization-export-', dir=destination.parent))
    try:
        files = []
        for name, value in documents.items():
            payload = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            with (staging / name).open('wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            files.append({'path': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
        report = {'version': 1, 'profileUuid': profile_uuid, 'database': str(database),
                  'files': files, 'scope': 'organization-only', 'readyForLegacyStartup': False,
                  'downgradeReview': preview,
                  'retainedDomains': ['settings', 'recent', 'nsfw_manual', 'trash'],
                  'limitations': ['UUID and revision identities remain in SQLite',
                                  'settings-merge.json must be merged into retained settings']}
        (staging / 'export-manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows rename refuses an existing directory. Recheck for other hosts.
        if destination.exists():
            raise SnapshotError('Export destination was created concurrently')
        staging.rename(destination)
        return report
    finally:
        if staging.exists():
            shutil.rmtree(staging)
