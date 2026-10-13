"""Persistent handoff from a live application to an offline relocation worker."""
import ctypes
import json
import os
import time
import uuid
from itertools import islice
from pathlib import Path

from .relocation_cutover import _write_new_json,_replace_json,prepare_relocation_cutover
from .relocation_cutover import execute_prepared_relocation,recover_relocation_cutover
from .data_relocation import rewrite_relocation_paths
from .relocation_copy import prepare_job_relocation_files
from .guard import DataWriterGuard
from ..storage.runtime_state import _safe_path

_STATES={'waiting-for-exit','copying','reconciling','publishing','recovering','completed','error'}


def _read_job_record(path):
    path=Path(path).absolute();_safe_path(path.parent,path.name)
    with path.open('rb') as stream:
        raw=stream.read(65537)
    if len(raw)>65536:
        raise ValueError('Relocation job record exceeds supported size')
    record=json.loads(raw)
    if not isinstance(record,dict) or record.get('version')!=1 or \
            path.name!='job.json' or record.get('jobId')!=path.parent.name or \
            record.get('state') not in _STATES:
        raise ValueError('Invalid relocation job record')
    if str(uuid.UUID(record['jobId']))!=record['jobId']:
        raise ValueError('Invalid relocation job identity')
    for key in ('source','candidate','selector'):
        value=record.get(key)
        if not isinstance(value,str) or not Path(value).is_absolute():
            raise ValueError('Relocation job paths must be absolute')
        _safe_path(Path(value).parent,Path(value).name)
    source=Path(record['source']);candidate=Path(record['candidate']);selector=Path(record['selector'])
    expected=_safe_path(selector.parent,'.cmv-relocation-jobs/'+record['jobId']+'/job.json')
    if expected!=path or selector.name!='portable.flag' or \
            source==candidate or source in candidate.parents or candidate in source.parents or \
            any(selector.is_relative_to(folder) for folder in (source,candidate)):
        raise ValueError('Relocation job paths do not match this application')
    if type(record.get('parentPid')) is not int or record['parentPid']<=0 or \
            record.get('parentBirth') is None:
        raise ValueError('Invalid relocation job process identity')
    return record


def read_relocation_job(selector,job_id):
    selector=Path(selector).absolute()
    if not isinstance(job_id,str) or str(uuid.UUID(job_id))!=job_id:
        raise ValueError('Invalid relocation job identity')
    path=_safe_path(selector.parent,'.cmv-relocation-jobs/'+job_id+'/job.json')
    record=_read_job_record(path)
    if not isinstance(record,dict) or record.get('version')!=1 or record.get('jobId')!=job_id or \
            record.get('selector')!=str(selector) or record.get('state') not in _STATES:
        raise ValueError('Relocation job record does not match this application')
    if not all(isinstance(record.get(key),str) and Path(record[key]).is_absolute()
               for key in ('source','candidate')):
        raise ValueError('Relocation job directories are invalid')
    return {key:record[key] for key in ('version','jobId','state','source','candidate',
            'authorityChanged','restartRequired','error','recoveryRequired') if key in record}


def latest_relocation_job(selector,data_dir):
    selector=Path(selector).absolute();data_dir=str(Path(data_dir).absolute())
    directory=_safe_path(selector.parent,'.cmv-relocation-jobs')
    if not directory.exists():
        return None
    with os.scandir(directory) as entries:
        folders=list(islice(entries,1001))
    if len(folders)>1000:
        raise ValueError('Too many relocation records; explicit job selection is required')
    latest=None
    for folder in folders:
        try:
            if str(uuid.UUID(folder.name))!=folder.name:
                continue
        except ValueError:
            continue
        path=_safe_path(directory,folder.name+'/job.json')
        if not path.exists():
            continue
        record=read_relocation_job(selector,folder.name)
        if data_dir not in (record.get('source'),record.get('candidate')):
            continue
        key=(path.stat().st_mtime_ns,folder.name)
        if latest is None or key>latest[0]:
            latest=(key,record)
    return latest[1] if latest else None


def _process_handle(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
    handle=kernel.OpenProcess(0x100000|0x1000,False,pid)
    if not handle:
        error=ctypes.get_last_error()
        if error==87:
            return kernel,None,None
        raise OSError(error,'Cannot inspect application process')
    class FileTime(ctypes.Structure):
        _fields_=[('low',ctypes.c_uint32),('high',ctypes.c_uint32)]
    times=[FileTime() for _ in range(4)]
    kernel.GetProcessTimes.argtypes=[ctypes.c_void_p]+[ctypes.POINTER(FileTime)]*4
    if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in times]):
        kernel.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(),'Cannot inspect application start time')
    birth=(times[0].high<<32)|times[0].low
    return kernel,handle,birth


def _birth(pid):
    if os.name=='nt':
        kernel,handle,birth=_process_handle(pid)
        if handle:
            kernel.CloseHandle(handle)
        return birth
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    except FileNotFoundError:
        return None


def _wait_for_exit(pid,birth,timeout):
    deadline=time.monotonic()+timeout
    if os.name=='nt':
        kernel,handle,current=_process_handle(pid)
        if not handle:
            return
        try:
            if current!=birth:
                return  # Original process exited; PID was reused.
            while time.monotonic()<deadline:
                result=kernel.WaitForSingleObject(handle,200)
                if result==0:
                    return
                if result!=258:
                    raise OSError('Application process wait failed')
        finally:
            kernel.CloseHandle(handle)
    else:
        while time.monotonic()<deadline:
            if _birth(pid)!=birth:
                return
            time.sleep(.2)
    raise TimeoutError('Application is still running; relocation was not started')


def create_relocation_job(source,candidate,selector,*,parent_pid=None):
    source=Path(source).absolute();candidate=Path(candidate).absolute();selector=Path(selector).absolute()
    for path in (source,candidate,selector):
        _safe_path(path.parent,path.name)
    if not source.is_dir() or candidate.exists() or not candidate.parent.is_dir():
        raise ValueError('Relocation requires an existing source and a new candidate')
    if source==candidate or source in candidate.parents or candidate in source.parents:
        raise ValueError('Relocation directories must be separate')
    if selector.name!='portable.flag' or any(selector.is_relative_to(path) for path in (source,candidate)):
        raise ValueError('Application selector must be outside the data directories')
    parent_pid=os.getpid() if parent_pid is None else parent_pid
    if type(parent_pid) is not int or parent_pid<=0:
        raise ValueError('Invalid application PID')
    birth=_birth(parent_pid)
    if birth is None:
        raise ValueError('Application process is not running')
    job_id=str(uuid.uuid4())
    folder=_safe_path(selector.parent,'.cmv-relocation-jobs/'+job_id)
    folder.mkdir(parents=True)
    record={'version':1,'jobId':job_id,'state':'waiting-for-exit','source':str(source),
            'candidate':str(candidate),'selector':str(selector),'parentPid':parent_pid,
            'parentBirth':birth,'authorityChanged':False,'restartRequired':False}
    _write_new_json(folder/'job.json',record)
    return folder/'job.json'


def run_relocation_job(path,*,wait_timeout=60):
    path=Path(path).absolute();_safe_path(path.parent,path.name)
    with DataWriterGuard(path.parent):
        return _run_relocation_job(path,wait_timeout=wait_timeout)


def _run_relocation_job(path,*,wait_timeout):
    path=Path(path).absolute();_safe_path(path.parent,path.name)
    record=_read_job_record(path)
    if record['parentPid']==os.getpid():
        raise ValueError('Invalid relocation job owner')
    def status(state,**values):
        record.update(state=state,**values);_replace_json(path,record)
    try:
        _wait_for_exit(record['parentPid'],record['parentBirth'],wait_timeout)
        candidate=Path(record['candidate'])
        cutover=_safe_path(candidate,'relocation-cutover.json')
        if cutover.exists():
            status('recovering')
            result=recover_relocation_cutover(candidate)
        else:
            status('copying')
            if not _safe_path(candidate,'relocation-databases.json').exists():
                prepare_job_relocation_files(record['source'],candidate,path)
            status('reconciling')
            rewrite_relocation_paths(candidate)
            prepare_relocation_cutover(candidate,record['selector'])
            status('publishing')
            result=execute_prepared_relocation(candidate)
        status('completed',authorityChanged=result['authorityChanged'],restartRequired=True,error=None)
        return record
    except Exception as error:
        recovery_required=_safe_path(Path(record['source']),'relocation-retired.json').exists()
        status('error',error=str(error),restartRequired=False,recoveryRequired=recovery_required,
               authorityChanged=recovery_required)
        raise
