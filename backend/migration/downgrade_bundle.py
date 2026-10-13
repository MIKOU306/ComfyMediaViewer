"""Assemble current legacy projections without changing storage authority."""
import hashlib
import json
import os
from pathlib import Path
import shutil

from .export_legacy import export_organization, organization_export_preview
from .recovery_restore import verify_recovery_bundle
from .relocation_cutover import _write_new_json
from ..storage.runtime_state import _safe_path
from .backup import _safe_relative


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_downgrade_bundle(directory):
    """Validate only material completeness, never authorize lossy startup."""
    directory = Path(directory).absolute()
    report_path = _safe_path(directory, 'downgrade-report.json')
    with report_path.open('rb') as stream:
        payload = stream.read(32 * 1024 * 1024 + 1)
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError('Downgrade report exceeds supported size')
    report = json.loads(payload)
    if not isinstance(report, dict) or report.get('version') != 1 or \
            report.get('scope') != 'current-data-legacy-projections' or \
            report.get('selectorChanged') is not False or report.get('readyForLegacyStartup') is not False:
        raise ValueError('Invalid downgrade report')
    legacy = _safe_path(directory, 'legacy-data')
    expected = set()
    for entry in report['files']:
        relative = _safe_relative(entry['path']).as_posix()
        if relative in expected or relative in ('storage-state.json', 'portable.flag'):
            raise ValueError('Duplicate or activated downgrade material')
        path = _safe_path(legacy, relative)
        if not path.is_file() or path.stat().st_size != entry['bytes'] or _digest(path) != entry['sha256']:
            raise ValueError('Downgrade material mismatch: ' + relative)
        expected.add(relative)
    actual = set()
    for path in legacy.rglob('*'):
        _safe_path(legacy, path.relative_to(legacy).as_posix())
        if path.is_file():
            actual.add(path.relative_to(legacy).as_posix())
    if actual != expected:
        raise ValueError('Downgrade file set differs from report')
    if report.get('projectionComplete') is not (not report['blockers']):
        raise ValueError('Downgrade readiness disagrees with blockers')
    if report['projectionComplete'] and not {'settings.json', 'recent.json', 'favorites.json',
            'favorites.seq.json', 'tags.json', 'marks.json', 'trash/index.json'} <= expected:
        raise ValueError('Complete projection lacks required legacy domains')
    verify_recovery_bundle(report['sourceBundle'])
    return report


def build_downgrade_bundle(bundle, destination):
    """Keep incomplete work for inspection; never overwrite or activate it."""
    report, preserved, before = verify_recovery_bundle(bundle)
    destination = Path(destination).absolute()
    bundle = Path(bundle).absolute()
    _safe_path(destination.parent, destination.name)
    if destination.exists() or destination in bundle.parents or bundle in destination.parents:
        raise ValueError('Downgrade requires a new separate directory')
    if not destination.parent.is_dir():
        raise ValueError('Downgrade destination parent must exist')
    destination.mkdir()
    # Component directories are retained as review evidence on any failure.
    from .downgrade_retained import build_downgrade_materials
    from .downgrade_trash import export_trash
    identity = report['storageIdentity']
    database = _safe_path(preserved, identity['database'])
    review = organization_export_preview(database, identity['profileUuid'])
    organization = export_organization(database, identity['profileUuid'], destination / 'organization') \
        if review['organizationExportable'] else {'files': [], 'downgradeReview': review}
    retained = build_downgrade_materials(bundle, destination / 'retained')
    trash = export_trash(bundle, destination / 'trash-projection')
    legacy = destination / 'legacy-data'
    legacy.mkdir()
    for directory in ('organization', 'retained', 'trash-projection'):
        source = destination / directory
        for entry in sorted(source.rglob('*')):
            relative = entry.relative_to(source).as_posix()
            _safe_path(source, relative)
            if not entry.is_file() or relative in ('export-manifest.json', 'settings-merge.json'):
                continue
            # Only explicitly enumerated component payload files are assembled;
            # component reports stay outside the legacy data directory.
            component = {'organization': organization, 'retained': retained,
                         'trash-projection': trash}[directory]
            file_list = component['files']
            declared = file_list if isinstance(file_list, dict) else {row['path']: row for row in file_list}
            if relative not in declared:
                continue
            metadata = declared[relative]
            if entry.stat().st_size != metadata['bytes'] or _digest(entry) != metadata['sha256']:
                raise ValueError('Downgrade component changed: ' + relative)
            target = _safe_path(legacy, relative)
            if target.exists():
                if target.stat().st_size == metadata['bytes'] and _digest(target) == metadata['sha256']:
                    continue
                raise ValueError('Conflicting downgrade component file: ' + relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with entry.open('rb') as incoming, target.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if target.stat().st_size != metadata['bytes'] or _digest(target) != metadata['sha256']:
                raise ValueError('Assembled downgrade file mismatch: ' + relative)
    after_report, _, after = verify_recovery_bundle(bundle)
    if before != after or report != after_report:
        raise ValueError('Recovery materials changed during downgrade assembly')
    files = [{'path': path.relative_to(legacy).as_posix(), 'bytes': path.stat().st_size,
              'sha256': _digest(path)}
             for path in sorted(legacy.rglob('*')) if path.is_file()]
    blockers = review['blockers'] + retained.get('blockers', []) + trash.get('blockers', [])
    losses = retained.get('losses', []) + trash.get('losses', [])
    losses += [{'domain': 'catalog', 'reason': reason}
               for reason in organization['downgradeReview']['notRepresentedInLegacy']]
    result = {'version': 1, 'scope': 'current-data-legacy-projections',
              'sourceBundle': str(bundle), 'legacyData': str(legacy),
              'files': files, 'blockers': blockers, 'losses': losses,
              'components': {'organization': organization, 'retained': retained, 'trash': trash},
              'projectionComplete': not blockers, 'readyForLegacyStartup': False,
              'selectorChanged': False,
              'requiredReview': 'Validate legacy loading and explicitly accept listed losses; keep the complete recovery bundle.'}
    _write_new_json(destination / 'downgrade-report.json', result)
    verify_downgrade_bundle(destination)
    return result
