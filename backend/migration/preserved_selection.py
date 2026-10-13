"""Preserve byte-readable unusable data before selecting a verified restore.

Raw preservation is evidence, not a repaired database or an old-version export.
The caller must stop all writers; historical processes do not obey our guard.
"""
from contextlib import ExitStack, closing
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .guard import DataWriterGuard
from .preflight import scan_manifest
from .recovery_export import _digest
from .recovery_job import _verify_completed
from .recovery_restore import verify_recovery_bundle
from .recovery_selection import _safe, _separate, _read_json, _selected_source, _selector_evidence, _MAX_RECORD
from .relocation_cutover import _replace_json, _write_new_json
from ..storage.runtime_state import load_state, _safe_path


JSON_DAMAGE = {'invalid_json', 'invalid_utf8', 'duplicate_json_key', 'json_too_large'}


def _validate_before(selector, before, source):
    if not isinstance(before, dict) or set(before) != {'exists', 'base64', 'sha256'} or type(before['exists']) is not bool:
        raise ValueError('Invalid original selector evidence')
    if before['exists']:
        try:
            raw = base64.b64decode(before['base64'], validate=True)
            if hashlib.sha256(raw).hexdigest() != before['sha256']:
                raise ValueError('Original selector evidence hash mismatch')
            value = json.loads(raw.decode('utf-8'))
        except (TypeError, UnicodeError, ValueError) as error:
            raise ValueError('Original selector bytes do not verify') from error
        if isinstance(value, dict) and isinstance(value.get('dir'), str):
            path = Path(value['dir'])
            selected = _safe((path if path.is_absolute() else selector.parent / path).absolute())
        else:
            selected = _safe((selector.parent / 'cmv-data').absolute())
    else:
        if before['base64'] is not None or before['sha256'] is not None:
            raise ValueError('Missing-selector evidence is inconsistent')
        selected = _safe((Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'ComfyMediaViewer').absolute())
    if selected != source:
        raise ValueError('Original selector evidence belongs to another source')


def _inventory(source):
    manifest = scan_manifest(source)
    if any(row['status'] != 'ok' for row in manifest['files']) or any(
            issue['code'] not in JSON_DAMAGE for issue in manifest['issues']):
        raise ValueError('Cannot preserve the complete readable source; inspect permissions, links or changing files')
    return manifest


def _verify_raw(raw, manifest):
    wanted = {entry['path']: entry for entry in manifest['files']}
    actual = _inventory(raw)
    if {entry['path'] for entry in actual['files']} != set(wanted):
        raise ValueError('Preserved raw file set changed')
    for entry in actual['files']:
        previous = wanted[entry['path']]
        if entry['bytes'] != previous['bytes'] or entry['sha256'] != previous['sha256']:
            raise ValueError('Preserved raw material changed: ' + entry['path'])


def select_preserved_source_restore(selector, bundle, target, evidence_directory):
    selector, bundle, target, evidence = map(lambda path: _safe(Path(path)),
                                           (selector, bundle, target, evidence_directory))
    if selector.name != 'portable.flag':
        raise ValueError('Only portable.flag is supported')
    report, _, _ = verify_recovery_bundle(bundle)
    source = _safe(Path(report['sourceDataDir']))
    _separate((selector, bundle, target, evidence, source))
    if not all(path.is_dir() for path in (source, target, selector.parent, evidence.parent)):
        raise ValueError('Source, restored target, program and evidence parent must exist')
    binding = {'action': 'restore', 'bundle': str(bundle), 'destination': str(target)}
    with ExitStack() as guards:
        for root in sorted((source, target, selector.parent), key=lambda path: str(path).casefold()):
            guards.enter_context(DataWriterGuard(root))
        _verify_completed(binding)
        target_identity = load_state(target)
        with closing(sqlite3.connect(_safe_path(target, target_identity['database']).as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Restored catalog fails integrity verification')
        if evidence.exists():
            intent = _read_json(evidence / 'preserved-selection-intent.json')
            if (intent.get('version') != 1 or intent.get('state') != 'prepared'
                    or intent.get('selector') != str(selector) or intent.get('source') != str(source)
                    or intent.get('bundle') != str(bundle) or intent.get('target') != str(target)
                    or intent.get('targetIdentity') != target_identity
                    or intent.get('preservationMode') != 'raw-files' or intent.get('readyForStartup') is not False):
                raise ValueError('Existing preservation intent does not match this selection')
            manifest = intent['sourceManifest']
            _verify_raw(evidence / 'raw-current-data', manifest)
            if _inventory(source) != manifest:
                raise ValueError('Original source changed after preservation')
        else:
            if _selected_source(selector) != source:
                raise ValueError('Selector differs from the original source')
            manifest = _inventory(source)
            intent = {'version': 1, 'state': 'prepared', 'selector': str(selector),
                      'source': str(source), 'bundle': str(bundle), 'target': str(target),
                      'targetIdentity': target_identity, 'sourceManifest': manifest,
                      'selectorBefore': _selector_evidence(selector), 'sourceHealth': 'not-validated',
                      'preservationMode': 'raw-files', 'readyForStartup': False,
                      'currentDataPreserved': True, 'selectorChanged': False,
                      'warning': 'Raw preserved source may require repair; changes after the bundle are not automatically merged'}
            if len(json.dumps(intent, ensure_ascii=False).encode('utf-8')) + 1 > _MAX_RECORD:
                raise ValueError('Preservation inventory exceeds supported record size')
            if shutil.disk_usage(evidence.parent).free < sum(entry['bytes'] for entry in manifest['files']) + 64 * 1024 * 1024:
                raise ValueError('Insufficient free space to preserve the current source')
            evidence.mkdir()
            raw = evidence / 'raw-current-data'; raw.mkdir()
            for entry in manifest['files']:
                origin = _safe_path(source, entry['path'])
                copy = _safe_path(raw, entry['path'])
                copy.parent.mkdir(parents=True, exist_ok=True)
                with origin.open('rb') as reader, copy.open('xb') as writer:
                    shutil.copyfileobj(reader, writer); writer.flush(); os.fsync(writer.fileno())
                if copy.stat().st_size != entry['bytes'] or _digest(copy) != entry['sha256']:
                    raise ValueError('Source changed while preserving: ' + entry['path'])
            _verify_raw(raw, manifest)
            if _inventory(source) != manifest or _selector_evidence(selector) != intent['selectorBefore']:
                raise ValueError('Source or selector changed during preservation')
            _write_new_json(evidence / 'preserved-selection-intent.json', intent)
        completed = dict(intent, state='completed', selectorChanged=True)
        _validate_before(selector, intent['selectorBefore'], source)
        receipt_path = evidence / 'preserved-selection-receipt.json'
        selected = _selected_source(selector)
        if receipt_path.exists():
            if _read_json(receipt_path) != completed or selected != target:
                raise ValueError('Completed preservation selection no longer verifies')
            return completed
        if selected == source and _selector_evidence(selector) == intent['selectorBefore']:
            if _inventory(source) != intent['sourceManifest']:
                raise ValueError('Source changed before selection publication')
            _verify_raw(evidence / 'raw-current-data', intent['sourceManifest'])
            _verify_completed(binding)
            if load_state(target) != intent['targetIdentity'] or _selector_evidence(selector) != intent['selectorBefore']:
                raise ValueError('Target or selector changed before selection publication')
            _replace_json(selector, {'dir': str(target)})
        elif selected != target:
            raise ValueError('Incomplete selection has an unknown selector outcome')
        _write_new_json(receipt_path, completed)
        return completed
