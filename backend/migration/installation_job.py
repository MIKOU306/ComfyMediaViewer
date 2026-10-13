"""Durable handoff for offline installation and activation."""
import json
import os
import uuid
from itertools import islice
from pathlib import Path

from .guard import DataWriterGuard
from .installation_flow import complete_installation
from .relocation_cutover import _replace_json, _write_new_json
from .relocation_job import _birth, _wait_for_exit
from .status import preparation_status
from ..storage.runtime_state import _safe_path, load_state

_STATES = {'waiting-for-exit', 'installing', 'completed', 'error'}
_FIELDS = {'version', 'jobId', 'prepared', 'source', 'bundleDirectory', 'selector',
           'parentPid', 'parentBirth', 'state', 'runUuid', 'authorityPublished',
           'restartRequired', 'error'}
_READY = {'reconciled', 'switching', 'active'}


def _check_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Installation job paths must be absolute')
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Installation job paths cannot contain links')
    return path


def _job_path(selector, job_id):
    return _safe_path(selector.parent, '.cmv-installation-jobs/' + job_id + '/job.json')


def _separate(paths):
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError('Source, prepared, bundle, and selector paths must be separate')


def _status(prepared, run_uuid):
    result = preparation_status(prepared, run_uuid)
    if result['state'] not in _READY:
        raise ValueError('Prepared run is not reconciled, switching, or active')
    return result


def _read(path):
    path = Path(path).absolute()
    _check_path(path)
    if path.name != 'job.json' or path.parent.parent.name != '.cmv-installation-jobs':
        raise ValueError('Invalid installation job path')
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Installation job record exceeds supported size')
    record = json.loads(raw.decode('utf-8'))
    if not isinstance(record, dict) or set(record) != _FIELDS or \
            type(record.get('version')) is not int or record['version'] != 1:
        raise ValueError('Invalid installation job record')
    job_id = record.get('jobId')
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id or path.parent.name != job_id:
        raise ValueError('Invalid installation job identity')
    if not isinstance(record.get('state'), str) or record['state'] not in _STATES:
        raise ValueError('Invalid installation job state')
    for key in ('prepared', 'source', 'bundleDirectory', 'selector'):
        value = record.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value)) != value:
            raise ValueError('Invalid installation job paths')
        _check_path(Path(value))
    prepared, source, bundle, selector = (Path(record[key]) for key in
                                          ('prepared', 'source', 'bundleDirectory', 'selector'))
    _separate((prepared, source, bundle, selector))
    if selector.name != 'portable.flag' or _job_path(selector, job_id) != path:
        raise ValueError('Installation job does not match this application')
    if type(record.get('parentPid')) is not int or record['parentPid'] <= 0 or \
            type(record.get('parentBirth')) not in (int, str):
        raise ValueError('Invalid installation owner process identity')
    run_uuid = record.get('runUuid')
    if not isinstance(run_uuid, str) or str(uuid.UUID(run_uuid)) != run_uuid:
        raise ValueError('Invalid prepared run identity')
    if record.get('authorityPublished') not in (None, True, False) or \
            type(record.get('restartRequired')) not in (bool, type(None)):
        raise ValueError('Invalid installation outcome flags')
    if record['state'] == 'completed' and (record['authorityPublished'] is not True or
                                            record['restartRequired'] is not True):
        raise ValueError('Completed installation must require restart and have published authority')
    if record.get('error') is not None and not isinstance(record['error'], str):
        raise ValueError('Invalid installation error')
    status = _status(prepared, run_uuid)
    if status['sourceDataDir'] != str(source):
        raise ValueError('Installation job is bound to another source')
    return record


def create_installation_job(prepared, bundle_directory, selector, *, run_uuid=None, parent_pid=None):
    prepared, bundle, selector = (Path(p).absolute() for p in (prepared, bundle_directory, selector))
    for path in (prepared, bundle, selector):
        _check_path(path)
    if selector.name != 'portable.flag' or not prepared.is_dir() or bundle.exists() or \
            not bundle.parent.is_dir():
        raise ValueError('Installation requires a prepared run, new bundle directory, and portable selector')
    report_path = _safe_path(prepared, 'preparation-report.json')
    _check_path(report_path)
    with report_path.open('rb') as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Preparation report exceeds supported size')
    report = json.loads(raw.decode('utf-8'))
    if not isinstance(report, dict) or not isinstance(report.get('runUuid'), str):
        raise ValueError('Prepared run report is invalid')
    report_uuid = str(uuid.UUID(report['runUuid']))
    if run_uuid is not None and str(uuid.UUID(run_uuid)) != report_uuid:
        raise ValueError('Requested run identity differs from prepared report')
    status = _status(prepared, report_uuid)
    source = Path(status['sourceDataDir'])
    _check_path(source)
    _separate((prepared, source, bundle, selector))
    if pending_installation_job(selector, prepared):
        raise ValueError('An unfinished installation job must be resumed before creating another')
    parent_pid = os.getpid() if parent_pid is None else parent_pid
    if type(parent_pid) is not int or parent_pid <= 0:
        raise ValueError('Invalid application PID')
    birth = _birth(parent_pid)
    if birth is None:
        raise ValueError('Application process is not running')
    job_id = str(uuid.uuid4())
    folder = _safe_path(selector.parent, '.cmv-installation-jobs/' + job_id)
    folder.mkdir(parents=True)
    record = {'version': 1, 'jobId': job_id, 'prepared': str(prepared), 'source': str(source),
              'bundleDirectory': str(bundle), 'selector': str(selector), 'parentPid': parent_pid,
              'parentBirth': birth, 'state': 'waiting-for-exit', 'runUuid': report_uuid,
              'authorityPublished': None, 'restartRequired': None, 'error': None}
    _write_new_json(folder / 'job.json', record)
    return folder / 'job.json'


def read_installation_job(selector, job_id):
    selector = Path(selector).absolute()
    _check_path(selector)
    if selector.name != 'portable.flag' or not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
        raise ValueError('Invalid installation job identity')
    record = _read(_job_path(selector, job_id))
    if record['selector'] != str(selector):
        raise ValueError('Installation job record does not match this application')
    return dict(record)


def pending_installation_job(selector, prepared):
    selector, prepared = Path(selector).absolute(), Path(prepared).absolute()
    directory = _safe_path(selector.parent, '.cmv-installation-jobs')
    _check_path(directory)
    if not directory.exists():
        return None
    with os.scandir(directory) as entries:
        folders = list(islice(entries, 1001))
    if len(folders) > 1000:
        raise ValueError('Too many installation jobs; explicit selection is required')
    pending = []
    for folder in folders:
        try:
            if str(uuid.UUID(folder.name)) != folder.name:
                continue
        except ValueError:
            continue
        record = read_installation_job(selector, folder.name)
        if record['prepared'] == str(prepared) and record['state'] != 'completed':
            pending.append(record)
    if len(pending) > 1:
        raise ValueError('Multiple unfinished installation jobs require explicit recovery')
    return pending[0] if pending else None


def discover_installation_jobs(selector, source):
    selector, source = Path(selector).absolute(), Path(source).absolute()
    _check_path(selector)
    _check_path(source)
    directory = _safe_path(selector.parent, '.cmv-installation-jobs')
    _check_path(directory)
    if not directory.exists():
        return []
    with os.scandir(directory) as entries:
        folders = list(islice(entries, 1001))
    if len(folders) > 1000:
        raise ValueError('Too many installation jobs; explicit selection is required')
    found = []
    for folder in folders:
        try:
            if str(uuid.UUID(folder.name)) != folder.name:
                continue
        except ValueError:
            continue
        record = read_installation_job(selector, folder.name)
        if record['source'] == str(source) and record['state'] != 'completed':
            found.append({key:record[key] for key in
                          ('jobId','state','prepared','bundleDirectory','error')})
    return sorted(found, key=lambda item:item['jobId'])


def run_installation_job(job_path, timeout=30):
    path = Path(job_path).absolute()
    _check_path(path)
    if path.name != 'job.json':
        raise ValueError('Invalid installation job path')
    with DataWriterGuard(path.parent):
        record = _read(path)
        if record['parentPid'] == os.getpid():
            raise ValueError('Invalid installation job owner')

        def save(state, **changes):
            record.update(state=state, **changes)
            _replace_json(path, record)

        try:
            _wait_for_exit(record['parentPid'], record['parentBirth'], timeout)
            prepared, bundle = Path(record['prepared']), Path(record['bundleDirectory'])
            status = _status(prepared, record['runUuid'])
            if status['sourceDataDir'] != record['source']:
                raise ValueError('Prepared run source differs from installation job')
            if record['state'] == 'completed':
                result = complete_installation(prepared, bundle)
                if result.get('runUuid') != record['runUuid'] or result.get('authorityPublished') is not True:
                    raise ValueError('Completed installation no longer matches its published authority')
                return dict(record)
            if bundle.exists() and not (bundle / 'bundle.json').is_file():
                raise ValueError('Incomplete bundle retained; refusing to overwrite it')
            save('installing', error=None, authorityPublished=None, restartRequired=None)
            result = complete_installation(prepared, bundle)
            if result.get('runUuid') != record['runUuid'] or result.get('authorityPublished') is not True:
                raise ValueError('Installation flow did not confirm the bound run and published authority')
            save('completed', authorityPublished=True, restartRequired=True, error=None)
            return dict(record)
        except Exception as error:
            published = None
            restart = None
            try:
                current = load_state(record['source'])
                if current['mode'] == 'sqlite' and current.get('migrationRunUuid') == record['runUuid']:
                    published, restart = True, True
            except Exception:
                pass
            save('error', authorityPublished=published, restartRequired=restart, error=str(error))
            raise
