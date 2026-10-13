"""Offline publication of a verified isolated recovery restore as the selected data directory."""
import base64
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .data_relocation import prepare_relocation_files
from .guard import DataWriterGuard
from .preflight import scan_manifest
from .recovery_job import _FIELDS, _STATES, _verify_completed, read_recovery_job
from .relocation_cutover import _replace_json, _selector_snapshot, _write_new_json
from ..storage.runtime_state import _safe_path, load_state

_MAX_RECORD = 16 * 1024 * 1024


def _safe(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Recovery selection paths must be absolute and cannot contain parent traversal')
    for item in (path,*path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.stat(),'st_file_attributes',0)&0x400):
            raise ValueError('Recovery selection paths cannot contain links')
    return path


def _separate(paths):
    for index,left in enumerate(paths):
        for right in paths[index+1:]:
            if left==right or left in right.parents or right in left.parents:
                raise ValueError('Recovery selection paths must be separate')


def _read_json(path):
    with _safe(path).open('rb') as stream:
        raw=stream.read(_MAX_RECORD+1)
    if len(raw)>_MAX_RECORD:
        raise ValueError('Recovery selection record exceeds supported size')
    value=json.loads(raw.decode('utf-8'))
    if not isinstance(value,dict): raise ValueError('Invalid recovery selection record')
    return value


def _selector_evidence(selector):
    snapshot=_selector_snapshot(selector)
    if snapshot['exists']:
        return {'exists':True,'base64':snapshot['base64'],'sha256':snapshot['sha256']}
    return {'exists':False,'base64':None,'sha256':None}


def _verify_backup(backup, intent):
    receipt=_read_json(backup/'previous-data'/'relocation-databases.json')
    if (receipt.get('version')!=1 or receipt.get('phase')!='files-without-authority' or
            receipt.get('source')!=intent['source'] or receipt.get('storage')!=intent['sourceIdentity'] or
            receipt.get('sourceManifest')!=intent['sourceManifest'] or receipt.get('requiresRemainingFiles') is not False):
        raise ValueError('Previous source backup evidence does not match selection intent')
    previous=backup/'previous-data'
    copy_targets=receipt.get('copyTargets')
    copied=receipt.get('copiedFiles')
    if not isinstance(copy_targets,dict) or not isinstance(copied,list):
        raise ValueError('Previous source backup manifest is invalid')
    entries={entry['path']:entry for entry in intent['sourceManifest'].get('files',[])}
    if len(entries)!=len(intent['sourceManifest'].get('files',[])):
        raise ValueError('Duplicate source manifest entries')
    databases={name for name in entries if Path(name).suffix.lower() in ('.sqlite','.db')}
    database_digests=intent.get('backupDatabases')
    if not isinstance(database_digests,dict) or set(database_digests)!=databases:
        raise ValueError('Previous database backup digests are incomplete')
    sidecars={name+suffix for name in databases for suffix in ('-wal','-shm','-journal')}
    if set(receipt.get('databases',[]))!=databases:
        raise ValueError('Previous source database set differs from manifest')
    expected_copied=set(entries)-databases-{'storage-state.json'}-sidecars
    if set(copied)!=expected_copied:
        raise ValueError('Previous source backup file set differs from manifest')
    if set(receipt.get('deferredFiles',[]))!=({'storage-state.json'} & set(entries) | (sidecars & set(entries))):
        raise ValueError('Previous source deferred-file evidence differs from manifest')
    if set(copy_targets)!={name for name in entries if name in {
            'relocation-databases.json','relocation-paths.json','relocation-cutover.json',
            'relocation-cutover-cancelled.json','relocation-ledger-history.json'}}:
        raise ValueError('Previous source backup relocation mapping differs')
    for relative in databases:
        target=_safe_path(previous,relative)
        if not target.is_file(): raise ValueError('Previous source database backup is missing: '+relative)
        with target.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if database_digests[relative]!={'bytes':target.stat().st_size,'sha256':digest}:
            raise ValueError('Previous database backup changed: '+relative)
        with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('Previous source database backup is corrupt: '+relative)
    for relative in copied:
        entry=entries[relative]
        target=_safe_path(previous,copy_targets.get(relative,relative).replace('\\','/'))
        _safe(target)
        if not target.is_file(): raise ValueError('Previous source backup file is missing: '+relative)
        with target.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if target.stat().st_size!=entry['bytes'] or digest!=entry['sha256']:
            raise ValueError('Previous source backup differs from manifest: '+relative)
    return receipt


def _read_job_for_retry(selector, job_id):
    """Read the immutable job identity after selector publication changed its source binding."""
    job_path=_safe_path(selector.parent,'.cmv-recovery-jobs/'+job_id+'/job.json')
    record=_read_json(job_path)
    if set(record)!=_FIELDS or type(record.get('version')) is not int or record['version']!=1:
        raise ValueError('Recovery job record is invalid')
    if record.get('selector')!=str(selector) or record.get('jobId')!=job_id or job_path.parent.name!=job_id:
        raise ValueError('Recovery job does not match selection receipt')
    if not isinstance(record.get('state'),str) or not isinstance(record.get('action'),str) \
            or record.get('state') not in _STATES or record.get('action') not in ('export','restore'):
        raise ValueError('Recovery job is not in a recognized completed state')
    for key in ('source','selector','destination'):
        value=record.get(key)
        if not isinstance(value,str) or not Path(value).is_absolute() or str(Path(value))!=value:
            raise ValueError('Recovery job path is invalid')
        _safe(Path(value))
    if record.get('bundle') is not None:
        value=record['bundle']
        if not isinstance(value,str) or not Path(value).is_absolute() or str(Path(value))!=value:
            raise ValueError('Recovery job bundle path is invalid')
        _safe(Path(value))
    return record


def _completed_receipt(selector, job_id, backup):
    intent=_read_json(backup/'selection-intent.json')
    receipt=_read_json(backup/'selection-receipt.json')
    if (intent.get('version')!=1 or intent.get('state')!='prepared' or
            receipt.get('version')!=1 or receipt.get('state')!='completed' or
            receipt.get('jobId')!=job_id or intent.get('jobId')!=job_id or
            receipt.get('source')!=intent.get('source') or receipt.get('target')!=intent.get('target') or
            receipt.get('sourceIdentity')!=intent.get('sourceIdentity') or
            receipt.get('targetIdentity')!=intent.get('targetIdentity') or
            receipt.get('sourceManifest')!=intent.get('sourceManifest') or
            receipt.get('backupDatabases')!=intent.get('backupDatabases') or
            intent.get('selector')!=str(selector) or receipt.get('selector')!=str(selector) or
            receipt.get('selectorBefore')!=intent.get('selectorBefore')):
        raise ValueError('Completed recovery selection receipt does not match its intent')
    evidence=intent.get('selectorBefore')
    if not isinstance(evidence,dict) or type(evidence.get('exists')) is not bool:
        raise ValueError('Original selector evidence is invalid')
    if evidence['exists']:
        try: prior=base64.b64decode(evidence['base64'],validate=True)
        except Exception as error: raise ValueError('Original selector bytes are invalid') from error
        if hashlib.sha256(prior).hexdigest()!=evidence.get('sha256'):
            raise ValueError('Original selector digest differs from its evidence')
    elif evidence.get('base64') is not None or evidence.get('sha256') is not None:
        raise ValueError('Missing-selector evidence is inconsistent')
    record=_read_job_for_retry(selector,job_id)
    if record['state']!='completed' or record['action']!='restore' or record['destination']!=intent['target'] \
            or record['source']!=intent['source'] or record['bundle']!=intent['bundle']:
        raise ValueError('Recovery selection job is no longer the completed restore in the receipt')
    _verify_completed(record)
    selected=json.loads(selector.read_text('utf-8'))
    if selected!={'dir':intent['target']}:
        raise ValueError('Portable selector differs from completed recovery selection')
    if load_state(intent['target'])!=intent['targetIdentity']:
        raise ValueError('Selected recovery target identity changed')
    _verify_backup(backup,intent)
    source=Path(intent['source'])
    if scan_manifest(source)!=intent['sourceManifest'] or load_state(source)!=intent['sourceIdentity']:
        raise ValueError('Original source no longer matches its preserved backup evidence')
    return {'state':'completed','jobId':job_id,'source':intent['source'],'target':intent['target'],
            'backupDirectory':str(backup),'receiptPath':str(backup/'selection-receipt.json'),
            'selectorChanged':True}


def select_restored_data(selector,job_id,backup_directory):
    """Back up the current source, then atomically select a completed isolated restore."""
    if not isinstance(job_id,str) or str(uuid.UUID(job_id))!=job_id:
        raise ValueError('Invalid recovery job identity')
    if not Path(selector).is_absolute() or not Path(backup_directory).is_absolute():
        raise ValueError('Selector and backup directory must be absolute')
    selector=_safe(Path(selector));backup=_safe(Path(backup_directory))
    if selector.name!='portable.flag': raise ValueError('Only portable.flag selection is supported')

    # On an idempotent retry, the selected source is already the target. Validate all
    # durable evidence without attempting to re-run preparation or rewrite the selector.
    if backup.exists() and (backup/'selection-receipt.json').is_file():
        return _completed_receipt(selector,job_id,backup)
    if backup.exists():
        raise ValueError('Recovery selection state is incomplete or unknown; refusing to reuse backup directory')

    record=read_recovery_job(selector,job_id)
    if record['state']!='completed' or record['action']!='restore':
        raise ValueError('Only a completed restore recovery job can be selected')
    _verify_completed(record)
    source,target,bundle=(_safe(Path(record[key])) for key in ('source','destination','bundle'))
    if _selected_source(selector)!=source:
        raise ValueError('Recovery job source differs from current selector')
    if not backup.parent.is_dir(): raise ValueError('Backup directory parent must exist')
    _separate((source,target,bundle,selector,backup))
    if backup.exists(): raise ValueError('Backup directory already exists')

    # prepare_relocation_files owns the source guard itself. It records a complete
    # SQLite backup plus persistent files without publishing data authority.
    backup.mkdir()
    source_backup=backup/'previous-data'
    try:
        prepared=prepare_relocation_files(source,source_backup)
        database_digests={}
        for relative in prepared['databases']:
            copied_database=_safe_path(source_backup,relative)
            with copied_database.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            database_digests[relative]={'bytes':copied_database.stat().st_size,'sha256':digest}
        selector_before=_selector_evidence(selector)
        source_identity=load_state(source)
        target_identity=load_state(target)
        if source_identity!=prepared['storage']:
            raise ValueError('Current source authority differs from its backup receipt')
        if target_identity!=verify_recovery_bundle_identity(bundle=record['bundle']):
            raise ValueError('Restore target identity differs from the recovery bundle')
        intent={'version':1,'state':'prepared','jobId':job_id,'selector':str(selector),
                'source':str(source),'target':str(target),'bundle':str(bundle),
                'sourceIdentity':source_identity,'targetIdentity':target_identity,
                'sourceManifest':prepared['sourceManifest'],'backupDatabases':database_digests,
                'selectorBefore':selector_before,
                'selectorChanged':False}
        _write_new_json(backup/'selection-intent.json',intent)

        guards=sorted((source,target,selector.parent),key=lambda path:str(path).casefold())
        with DataWriterGuard(guards[0]),DataWriterGuard(guards[1]),DataWriterGuard(guards[2]):
            latest=read_recovery_job(selector,job_id)
            if latest['state']!='completed' or latest['action']!='restore':
                raise ValueError('Completed restore job changed before selector publication')
            _verify_completed(latest)
            if scan_manifest(source)!=prepared['sourceManifest'] or load_state(source)!=source_identity:
                raise ValueError('Current source changed after its backup was prepared')
            if load_state(target)!=target_identity:
                raise ValueError('Recovery target changed before selector publication')
            if _selector_evidence(selector)!=selector_before:
                raise ValueError('Portable selector changed after selection was prepared')
            _verify_backup(backup,intent)
            _replace_json(selector,{'dir':str(target)})
            completed=dict(intent,state='completed',selectorChanged=True)
            _write_new_json(backup/'selection-receipt.json',completed)
        return {'state':'completed','jobId':job_id,'source':str(source),'target':str(target),
                'backupDirectory':str(backup),'receiptPath':str(backup/'selection-receipt.json'),
                'selectorChanged':True}
    except Exception:
        # Artifacts are intentionally retained. If publication may have happened,
        # only explicit inspection can resolve the unknown boundary safely.
        raise


def _selected_source(selector):
    if selector.is_file():
        value=json.loads(selector.read_text('utf-8'))
        if isinstance(value,dict) and isinstance(value.get('dir'),str):
            selected=Path(value['dir'])
            return _safe((selected if selected.is_absolute() else selector.parent/selected).absolute())
        return _safe((selector.parent/'cmv-data').absolute())
    import os
    return _safe((Path(os.environ.get('LOCALAPPDATA') or Path.home())/'ComfyMediaViewer').absolute())


def verify_recovery_bundle_identity(bundle):
    from .recovery_restore import verify_recovery_bundle
    report,_,_=verify_recovery_bundle(bundle)
    return report['storageIdentity']
