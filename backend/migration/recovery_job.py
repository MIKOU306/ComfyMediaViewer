"""Durable offline recovery handoff. It never changes the selected data directory."""
import json
import os
import uuid
from itertools import islice
from pathlib import Path

from .guard import DataWriterGuard
from .recovery_export import export_recovery_bundle
from .recovery_restore import restore_recovery_bundle, verify_recovery_bundle
from .relocation_cutover import _replace_json, _write_new_json
from .relocation_job import _birth, _wait_for_exit
from ..storage.runtime_state import _safe_path, load_state

_STATES = {'waiting-for-exit', 'running', 'completed', 'error'}
_FIELDS = {'version','jobId','source','selector','action','destination','bundle','parentPid',
           'parentBirth','state','selectorChanged','authorityPublished','scope','result','error'}


def _safe(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Recovery job paths must be absolute')
    if '..' in path.parts:
        raise ValueError('Recovery job paths cannot contain parent traversal')
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Recovery job paths cannot contain links')
    return path


def _separate(paths):
    paths = [p for p in paths if p is not None]
    for i, left in enumerate(paths):
        for right in paths[i+1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError('Recovery paths must be separate')


def _job_path(selector, job_id):
    return _safe_path(selector.parent, '.cmv-recovery-jobs/' + job_id + '/job.json')


def _selected(selector):
    selector = _safe(selector)
    if not selector.is_file():
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'ComfyMediaViewer'
        return _safe(base.absolute())
    try:
        with selector.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('Portable selector exceeds supported size')
        value = json.loads(raw.decode('utf-8'))
        raw_dir = value.get('dir') if isinstance(value, dict) else None
        if isinstance(raw_dir, str) and raw_dir.strip():
            selected = Path(raw_dir.strip())
            if '..' in selected.parts:
                raise ValueError('Portable selector path cannot contain parent traversal')
            if not selected.is_absolute():
                selected = selector.parent / selected
            return _safe(selected.absolute())
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return _safe((selector.parent / 'cmv-data').absolute())


def _read(path):
    path = _safe(Path(path).absolute())
    if path.name != 'job.json' or path.parent.parent.name != '.cmv-recovery-jobs':
        raise ValueError('Invalid recovery job path')
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Recovery job record exceeds supported size')
    record = json.loads(raw.decode('utf-8'))
    if not isinstance(record, dict) or set(record) != _FIELDS or type(record['version']) is not int or record['version'] != 1:
        raise ValueError('Invalid recovery job record')
    jid = record['jobId']
    if not isinstance(jid, str) or str(uuid.UUID(jid)) != jid or path.parent.name != jid:
        raise ValueError('Invalid recovery job identity')
    if not isinstance(record['state'],str) or not isinstance(record['action'],str) \
            or record['state'] not in _STATES or record['action'] not in ('export','restore'):
        raise ValueError('Invalid recovery job state or action')
    for key in ('source','selector','destination'):
        value = record[key]
        if not isinstance(value,str) or not Path(value).is_absolute() or str(Path(value)) != value:
            raise ValueError('Invalid recovery job paths')
        _safe(Path(value))
    if record['bundle'] is not None:
        value=record['bundle']
        if not isinstance(value,str) or not Path(value).is_absolute() or str(Path(value)) != value: raise ValueError('Invalid bundle path')
        _safe(Path(value))
    source,selector,destination=map(Path,(record['source'],record['selector'],record['destination']))
    bundle=Path(record['bundle']) if record['bundle'] else None
    if selector.name!='portable.flag' or _job_path(selector,jid)!=path or _selected(selector)!=source:
        raise ValueError('Recovery job does not match selected data directory')
    _separate((source,selector,destination,bundle))
    if record['action']=='export' and bundle is not None or record['action']=='restore' and bundle is None:
        raise ValueError('Recovery bundle does not match action')
    if type(record['parentPid']) is not int or record['parentPid']<=0 or type(record['parentBirth']) not in (int,str):
        raise ValueError('Invalid recovery owner process identity')
    if record['selectorChanged'] is not False or record['authorityPublished'] is not False or record['scope']!='recovery':
        raise ValueError('Invalid recovery publication claims')
    if record['error'] is not None and (not isinstance(record['error'],str) or len(record['error'])>1000):
        raise ValueError('Invalid recovery error')
    return record


def create_recovery_job(source, selector, action, destination, *, bundle=None, parent_pid=None):
    raw_paths=[source,selector,destination]+([] if bundle is None else [bundle])
    if any(not Path(p).is_absolute() for p in raw_paths):
        raise ValueError('Recovery job paths must be absolute')
    source,selector,destination=(_safe(Path(p)) for p in (source,selector,destination))
    bundle=None if bundle is None else _safe(Path(bundle))
    if selector.name!='portable.flag' or not source.is_dir() or (selector.exists() and not selector.is_file()) or _selected(selector)!=source:
        raise ValueError('Recovery requires selected existing source and portable selector')
    if action not in ('export','restore') or (action=='restore') != (bundle is not None):
        raise ValueError('Recovery action requires a matching bundle')
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('Recovery destination must be new and its parent must exist')
    if bundle is not None and not bundle.is_dir(): raise ValueError('Recovery bundle must exist')
    _separate((source,selector,destination,bundle))
    parent_pid=os.getpid() if parent_pid is None else parent_pid
    if type(parent_pid) is not int or parent_pid<=0: raise ValueError('Invalid application PID')
    birth=_birth(parent_pid)
    if birth is None: raise ValueError('Application process is not running')
    jid=str(uuid.uuid4()); folder=_safe_path(selector.parent,'.cmv-recovery-jobs/'+jid)
    folder.mkdir(parents=True)
    record={'version':1,'jobId':jid,'source':str(source),'selector':str(selector),'action':action,
            'destination':str(destination),'bundle':str(bundle) if bundle else None,'parentPid':parent_pid,
            'parentBirth':birth,'state':'waiting-for-exit','selectorChanged':False,
            'authorityPublished':False,'scope':'recovery','result':None,'error':None}
    _write_new_json(folder/'job.json',record)
    return folder/'job.json'


def read_recovery_job(selector, job_id):
    selector=_safe(Path(selector).absolute())
    if selector.name!='portable.flag' or not isinstance(job_id,str) or str(uuid.UUID(job_id))!=job_id:
        raise ValueError('Invalid recovery job identity')
    record=_read(_job_path(selector,job_id))
    if record['selector']!=str(selector): raise ValueError('Recovery job record does not match this application')
    return dict(record)


def discover_recovery_jobs(selector, source):
    """List at most 1000 noncompleted jobs bound to this selected source."""
    selector = _safe(Path(selector).absolute())
    source = _safe(Path(source).absolute())
    if selector.name != 'portable.flag':
        raise ValueError('Invalid portable selector')
    directory = _safe_path(selector.parent, '.cmv-recovery-jobs')
    _safe(directory)
    if not directory.exists():
        return []
    with os.scandir(directory) as entries:
        folders = list(islice(entries, 1001))
    if len(folders) > 1000:
        raise ValueError('Too many recovery jobs; explicit selection is required')
    found = []
    for folder in folders:
        try:
            if str(uuid.UUID(folder.name)) != folder.name:
                continue
        except ValueError:
            continue
        job_path = _job_path(selector, folder.name)
        # Peek only at the source binding so records for a prior selected directory
        # do not prevent discovery of jobs for the current one.
        _safe(job_path)
        try:
            with job_path.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                continue
            peek = json.loads(raw.decode('utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(peek, dict) or peek.get('source') != str(source):
            continue
        record = read_recovery_job(selector, folder.name)
        if record['state'] != 'completed':
            found.append({key:record[key] for key in ('jobId','state','action','destination','bundle','error')})
    return sorted(found, key=lambda item:item['jobId'])


def _verify_completed(record):
    dest=Path(record['destination'])
    if record['action']=='export':
        report, _, _ = verify_recovery_bundle(dest)
        if report.get('sourceDataDir') != record['source']:
            raise ValueError('Completed recovery export belongs to another source')
    else:
        report, _, _ = verify_recovery_bundle(Path(record['bundle']))
        with _safe(dest/'recovery-restore.json').open('rb') as stream: raw=stream.read(65537)
        if len(raw)>65536: raise ValueError('Restore receipt exceeds supported size')
        receipt=json.loads(raw.decode('utf-8'))
        if (receipt.get('bundle') != record['bundle'] or receipt.get('destination') != str(dest) or
                receipt.get('isolatedRestore') is not True or receipt.get('selectorChanged') is not False or
                load_state(dest) != report.get('storageIdentity')):
            raise ValueError('Completed recovery restore no longer verifies')


def run_recovery_job(job_path, timeout=30):
    path=_safe(Path(job_path).absolute())
    if path.name!='job.json': raise ValueError('Invalid recovery job path')
    with DataWriterGuard(path.parent):
        record=_read(path)
        if record['parentPid']==os.getpid(): raise ValueError('Invalid recovery job owner')
        def save(state,**changes):
            record.update(state=state,**changes);_replace_json(path,record)
        try:
            _wait_for_exit(record['parentPid'],record['parentBirth'],timeout)
            if record['state']=='completed':
                _verify_completed(record)
                return dict(record)
            if Path(record['destination']).exists():
                raise ValueError('Partial recovery destination retained; refusing to overwrite it')
            save('running',error=None)
            if record['action']=='export':
                result=export_recovery_bundle(record['source'],record['destination'])
            else:
                result=restore_recovery_bundle(record['bundle'],record['destination'])
            if result.get('authorityPublished') is True or result.get('selectorChanged') is True:
                raise ValueError('Recovery operation attempted authority publication')
            save('completed',result={'action':record['action'],'destination':record['destination'],
                                     'reportPath':str(Path(record['destination'])/
                                         ('recovery-export.json' if record['action']=='export' else 'recovery-restore.json')),
                                     'selectorChanged':False,'authorityPublished':False,'scope':'recovery'})
            _verify_completed(record)
            return dict(record)
        except Exception as error:
            # Keep all partial output for explicit inspection and refuse implicit overwrite on retry.
            save('error',error=str(error)[:1000])
            raise
