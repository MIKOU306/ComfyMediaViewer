"""Durable parent-exit handoff for selecting a completed isolated recovery restore."""
import json
import os
import uuid
from itertools import islice
from pathlib import Path

from .guard import DataWriterGuard
from .recovery_job import read_recovery_job, _verify_completed
from .recovery_selection import (_MAX_RECORD, _read_job_for_retry, _safe,
                                 _selected_source, select_restored_data)
from .relocation_cutover import _replace_json, _write_new_json
from .relocation_job import _birth, _wait_for_exit
from ..storage.runtime_state import _safe_path

_STATES={'waiting-for-exit','selecting','error','completed'}
_FIELDS={'version','jobId','source','target','bundle','selector','restoreJobId','backupDirectory',
         'parentPid','parentBirth','state','selectorChanged','result','error'}


def _job_folder(selector,job_id):
    return _safe_path(selector.parent,'.cmv-recovery-selection-jobs/'+job_id)


def _restore(selector,restore_job_id):
    selected=_selected_source(selector)
    path=_safe_path(selector.parent,'.cmv-recovery-jobs/'+restore_job_id+'/job.json')
    if not path.is_file(): raise ValueError('Completed restore job is missing')
    # The ordinary reader enforces current-source binding before publication. Once
    # selected, use the same completed-job evidence while allowing that bound target.
    try:
        record=read_recovery_job(selector,restore_job_id)
    except ValueError:
        record=_read_job_for_retry(selector,restore_job_id)
    if record['state']!='completed' or record['action']!='restore':
        raise ValueError('Only a completed restore job can be selected')
    if selected not in (Path(record['source']),Path(record['destination'])):
        raise ValueError('Current selector points to a foreign data directory')
    _verify_completed(record)
    return record


def _read(path,selector=None):
    path=_safe(Path(path))
    if path.name!='job.json' or path.parent.parent.name!='.cmv-recovery-selection-jobs':
        raise ValueError('Invalid recovery selection job path')
    with path.open('rb') as stream: raw=stream.read(65537)
    if len(raw)>65536: raise ValueError('Recovery selection job exceeds supported size')
    record=json.loads(raw.decode('utf-8'))
    if not isinstance(record,dict) or set(record)!=_FIELDS or type(record.get('version')) is not int or record['version']!=1:
        raise ValueError('Invalid recovery selection job record')
    jid=record.get('jobId')
    if not isinstance(jid,str) or str(uuid.UUID(jid))!=jid or path.parent.name!=jid:
        raise ValueError('Invalid recovery selection job identity')
    if not isinstance(record.get('state'),str) or record.get('state') not in _STATES:
        raise ValueError('Invalid recovery selection job state')
    for key in ('source','target','bundle','selector','backupDirectory'):
        value=record.get(key)
        if not isinstance(value,str) or not Path(value).is_absolute() or str(Path(value))!=value:
            raise ValueError('Recovery selection job paths must be absolute')
        _safe(Path(value))
    selector_path=Path(record['selector']);source=Path(record['source']);target=Path(record['target'])
    bundle=Path(record['bundle']);backup=Path(record['backupDirectory'])
    if selector_path.name!='portable.flag' or _job_folder(selector_path,jid)/'job.json'!=path:
        raise ValueError('Recovery selection job does not match this application')
    if not isinstance(record.get('restoreJobId'),str) or str(uuid.UUID(record['restoreJobId']))!=record['restoreJobId']:
        raise ValueError('Invalid restore job identity')
    if type(record.get('parentPid')) is not int or record['parentPid']<=0 or type(record.get('parentBirth')) not in (int,str):
        raise ValueError('Invalid selection job owner process')
    if record.get('selectorChanged') is not None and type(record.get('selectorChanged')) is not bool:
        raise ValueError('Invalid selector outcome')
    if record.get('result') is not None and not isinstance(record['result'],dict):
        raise ValueError('Invalid selection job result')
    if record.get('error') is not None and (not isinstance(record['error'],str) or len(record['error'])>1000):
        raise ValueError('Invalid selection job error')
    _separate((source,target,bundle,selector_path,backup))
    if selector is not None:
        selector=_safe(Path(selector))
        if selector!=selector_path: raise ValueError('Selection job belongs to another selector')
        selected=_selected_source(selector)
        if selected not in (source,target): raise ValueError('Current selector points to a foreign data directory')
        restore=_restore(selector,record['restoreJobId'])
        if (restore['source']!=str(source) or restore['destination']!=str(target) or
                restore['bundle']!=str(bundle)):
            raise ValueError('Selection job differs from its completed restore binding')
    if record['state']=='completed' and (record['selectorChanged'] is not True or record['result'] is None):
        raise ValueError('Completed selection job lacks its publication result')
    return record


def _separate(paths):
    for index,left in enumerate(paths):
        for right in paths[index+1:]:
            if left==right or left in right.parents or right in left.parents:
                raise ValueError('Recovery selection job paths must be separate')


def create_selection_job(selector,restore_job_id,backup_directory,*,parent_pid=None):
    raw_selector=Path(selector);raw_backup=Path(backup_directory)
    if not raw_selector.is_absolute() or not raw_backup.is_absolute():
        raise ValueError('Selector and backup directory must be absolute')
    if '..' in raw_selector.parts or '..' in raw_backup.parts:
        raise ValueError('Selection job paths cannot contain parent traversal')
    selector=_safe(raw_selector);backup=_safe(raw_backup)
    if selector.name!='portable.flag' or not isinstance(restore_job_id,str) or str(uuid.UUID(restore_job_id))!=restore_job_id:
        raise ValueError('Invalid selector or restore job identity')
    if backup.exists() or not backup.parent.is_dir():
        raise ValueError('Selection backup must be new and its parent must exist')
    restore=_restore(selector,restore_job_id)
    source,target,bundle=Path(restore['source']),Path(restore['destination']),Path(restore['bundle'])
    if _selected_source(selector)!=source:
        raise ValueError('Restore target is already selected; inspect the original selection task')
    _separate((source,target,bundle,selector,backup))
    parent_pid=os.getpid() if parent_pid is None else parent_pid
    if type(parent_pid) is not int or parent_pid<=0: raise ValueError('Invalid application PID')
    birth=_birth(parent_pid)
    if birth is None: raise ValueError('Application process is not running')
    job_id=str(uuid.uuid4());folder=_job_folder(selector,job_id)
    folder.mkdir(parents=True)
    record={'version':1,'jobId':job_id,'source':str(source),'target':str(target),'bundle':str(bundle),
            'selector':str(selector),'restoreJobId':restore_job_id,'backupDirectory':str(backup),
            'parentPid':parent_pid,'parentBirth':birth,'state':'waiting-for-exit',
            'selectorChanged':None,'result':None,'error':None}
    _write_new_json(folder/'job.json',record)
    return folder/'job.json'


def read_selection_job(selector,job_id):
    selector=_safe(Path(selector).absolute())
    if selector.name!='portable.flag' or not isinstance(job_id,str) or str(uuid.UUID(job_id))!=job_id:
        raise ValueError('Invalid selection job identity')
    return dict(_read(_job_folder(selector,job_id)/'job.json',selector))


def discover_selection_jobs(selector,data_dir):
    selector=_safe(Path(selector).absolute());data_dir=_safe(Path(data_dir).absolute())
    directory=_safe_path(selector.parent,'.cmv-recovery-selection-jobs')
    _safe(directory)
    if not directory.exists(): return []
    with os.scandir(directory) as entries: folders=list(islice(entries,1001))
    if len(folders)>1000: raise ValueError('Too many selection jobs; explicit selection is required')
    found=[]
    for folder in folders:
        try:
            if str(uuid.UUID(folder.name))!=folder.name: continue
        except ValueError: continue
        path=_job_folder(selector,folder.name)/'job.json'
        _safe(path)
        try:
            with path.open('rb') as stream: raw=stream.read(65537)
            if len(raw)>65536: continue
            peek=json.loads(raw.decode('utf-8'))
        except (OSError,UnicodeError,json.JSONDecodeError): continue
        if not isinstance(peek,dict) or str(data_dir) not in (peek.get('source'),peek.get('target')):
            continue
        record=_read(path,selector)
        if record['state']!='completed':
            found.append({key:record[key] for key in ('jobId','state','restoreJobId','source','target',
                                                       'backupDirectory','selectorChanged','error')})
    return sorted(found,key=lambda item:item['jobId'])


def _outcome(selector,source,target):
    try:
        selected=_selected_source(selector)
        if selected==Path(source): return False
        if selected==Path(target): return True
    except Exception:
        pass
    return None


def run_selection_job(job_path,timeout=30):
    path=_safe(Path(job_path).absolute())
    if path.name!='job.json': raise ValueError('Invalid selection job path')
    with DataWriterGuard(path.parent):
        # Parse the owner/path binding first; selector state is checked separately so
        # a foreign current selector can be recorded as an unknown outcome safely.
        record=_read(path)
        if record['parentPid']==os.getpid(): raise ValueError('Invalid selection job owner')
        selector=Path(record['selector'])
        def save(state,**changes):
            record.update(state=state,**changes);_replace_json(path,record)
        try:
            _wait_for_exit(record['parentPid'],record['parentBirth'],timeout)
            current=_read(path,selector)
            if current['state']=='completed':
                result=select_restored_data(selector,record['restoreJobId'],record['backupDirectory'])
                return dict(current)
            if current['state']=='selecting' and Path(record['backupDirectory']).exists():
                raise ValueError('Selection may be incomplete; retained backup requires explicit inspection')
            save('selecting',selectorChanged=None,error=None)
            result=select_restored_data(selector,record['restoreJobId'],record['backupDirectory'])
            summary={key:result[key] for key in ('state','jobId','source','target','backupDirectory',
                                                 'receiptPath','selectorChanged') if key in result}
            save('completed',selectorChanged=True,result=summary,error=None)
            return dict(record)
        except Exception as error:
            save('error',selectorChanged=_outcome(selector,record['source'],record['target']),
                 error=str(error)[:1000])
            raise
