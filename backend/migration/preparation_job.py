"""Persistent offline handoff for legacy data preparation."""
import json
import os
import sqlite3
import uuid
from pathlib import Path
from itertools import islice

from .guard import DataWriterGuard
from .prepare import prepare_migration, resume_preparation
from .relocation_cutover import _write_new_json, _replace_json
from .relocation_job import _birth, _wait_for_exit
from .run_state import MigrationRuns
from .status import preparation_status
from ..storage.runtime_state import _safe_path

_STATES = {'waiting-for-exit', 'preparing', 'completed', 'error'}
_FIELDS = {'version', 'jobId', 'source', 'destination', 'selector', 'parentPid',
           'parentBirth', 'state', 'runUuid', 'error'}


def _check_absolute(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Preparation job paths must be absolute')
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Preparation job paths cannot contain links')
    return path


def _job_path(selector, job_id):
    return _safe_path(selector.parent, '.cmv-preparation-jobs/' + job_id + '/job.json')


def _read(path):
    path = Path(path).absolute()
    _check_absolute(path)
    if path.name != 'job.json' or path.parent.parent.name != '.cmv-preparation-jobs':
        raise ValueError('Invalid preparation job path')
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Preparation job record exceeds supported size')
    record = json.loads(raw)
    if not isinstance(record, dict) or set(record) != _FIELDS or \
            type(record.get('version')) is not int or record['version'] != 1:
        raise ValueError('Invalid preparation job record')
    job_id = record.get('jobId')
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id or path.parent.name != job_id:
        raise ValueError('Invalid preparation job identity')
    if not isinstance(record.get('state'), str) or record['state'] not in _STATES:
        raise ValueError('Invalid preparation job state')
    for key in ('source', 'destination', 'selector'):
        value = record.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value)) != value:
            raise ValueError('Invalid preparation job paths')
        _check_absolute(Path(value))
    source, destination, selector = (Path(record[k]) for k in ('source', 'destination', 'selector'))
    if selector.name != 'portable.flag' or _job_path(selector, job_id) != path:
        raise ValueError('Preparation job does not match this application')
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Preparation directories must be separate')
    if any(selector.is_relative_to(folder) for folder in (source, destination)):
        raise ValueError('Application selector must be outside the data directories')
    if type(record.get('parentPid')) is not int or record['parentPid'] <= 0 or \
            type(record.get('parentBirth')) not in (int, str):
        raise ValueError('Invalid preparation owner process identity')
    if record.get('runUuid') is not None and (not isinstance(record['runUuid'], str) or
            str(uuid.UUID(record['runUuid'])) != record['runUuid']):
        raise ValueError('Invalid migration run identity')
    if record.get('error') is not None and not isinstance(record['error'], str):
        raise ValueError('Invalid preparation error')
    return record


def create_preparation_job(source, destination, selector):
    source, destination, selector = (Path(p).absolute() for p in (source, destination, selector))
    for path in (source, destination, selector):
        _check_absolute(path)
    if selector.name != 'portable.flag' or not source.is_dir() or destination.exists() or not destination.parent.is_dir():
        raise ValueError('Preparation requires an existing source, new destination, and portable selector')
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Preparation directories must be separate')
    if any(selector.is_relative_to(folder) for folder in (source, destination)):
        raise ValueError('Application selector must be outside the data directories')
    if pending_preparation_job(selector, source):
        raise ValueError('An unfinished preparation job must be resumed before creating another')
    parent_pid = os.getpid()
    birth = _birth(parent_pid)
    if birth is None:
        raise ValueError('Application process is not running')
    job_id = str(uuid.uuid4())
    folder = _safe_path(selector.parent, '.cmv-preparation-jobs/' + job_id)
    folder.mkdir(parents=True)
    record = {'version': 1, 'jobId': job_id, 'source': str(source), 'destination': str(destination),
              'selector': str(selector), 'parentPid': parent_pid, 'parentBirth': birth,
              'state': 'waiting-for-exit', 'runUuid': None, 'error': None}
    _write_new_json(folder / 'job.json', record)
    return folder / 'job.json'


def read_preparation_job(selector, job_id):
    selector = Path(selector).absolute()
    _check_absolute(selector)
    if selector.name != 'portable.flag' or not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
        raise ValueError('Invalid preparation job identity')
    path = _job_path(selector, job_id)
    record = _read(path)
    if record['selector'] != str(selector):
        raise ValueError('Preparation job record does not match this application')
    return dict(record)


def pending_preparation_job(selector, source):
    selector=Path(selector).absolute()
    directory=_safe_path(selector.parent,'.cmv-preparation-jobs')
    if not directory.exists():
        return None
    with os.scandir(directory) as entries:
        folders=list(islice(entries,1001))
    if len(folders)>1000:
        raise ValueError('Too many preparation jobs; explicit selection is required')
    pending=[]
    for folder in folders:
        try:
            if str(uuid.UUID(folder.name))!=folder.name:
                continue
        except ValueError:
            continue
        record=read_preparation_job(selector,folder.name)
        if record['source']==str(Path(source).absolute()) and record['state'] in ('waiting-for-exit','preparing'):
            pending.append(record)
    if len(pending)>1:
        raise ValueError('Multiple unfinished preparation jobs require explicit recovery')
    return pending[0] if pending else None


def _bound_run(destination, source, report_uuid=None):
    registry = MigrationRuns(destination / 'migration-runs.sqlite')
    if report_uuid is not None:
        run = registry.read(report_uuid)
        matches = [run]
    else:
        # Read the registry without changing it; only identities recorded there can resume.
        _check_absolute(registry.database)
        if not registry.database.is_file():
            raise ValueError('Migration registry is missing')
        with sqlite3.connect(registry.database.as_uri() + '?mode=ro&nofollow=1', uri=True) as db:
            registry._validate(db)
            rows = db.execute('SELECT uuid FROM migration_run LIMIT 1001').fetchall()
        _check_absolute(registry.database)
        if len(rows) > 1000:
            raise ValueError('Too many migration runs to identify a preparation safely')
        matches = [registry.read(row[0]) for row in rows]
    matches = [run for run in matches if run['identity'].get('sourceDataDir') == str(source.absolute()) and
               run['identity'].get('snapshot') == str(destination / 'snapshot') and
               run['identity'].get('shadowDatabase') == str(destination / 'catalog.sqlite')]
    if len(matches) != 1:
        raise ValueError('Cannot uniquely identify a preparation run bound to this source and destination')
    return matches[0]['uuid']


def _existing_run(destination, source):
    report_path = _safe_path(destination, 'preparation-report.json')
    report_uuid = None
    if report_path.exists():
        with report_path.open('rb') as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Preparation report exceeds supported size')
        # Recheck after opening to reject a replaced link/reparse point.
        _check_absolute(report_path)
        report = json.loads(raw.decode('utf-8'))
        if not isinstance(report, dict):
            raise ValueError('Invalid preparation report')
        if report.get('runUuid'):
            report_uuid = str(uuid.UUID(report['runUuid']))
            if report.get('sourceDataDir') != str(source):
                raise ValueError('Preparation report belongs to another source')
    return _bound_run(destination, source, report_uuid)


def run_preparation_job(job_path, timeout=30):
    path = Path(job_path).absolute()
    _check_absolute(path)
    if path.name != 'job.json':
        raise ValueError('Invalid preparation job path')
    with DataWriterGuard(path.parent):
        record = _read(path)
        if record['parentPid'] == os.getpid():
            raise ValueError('Invalid preparation job owner')

        def save(state, **changes):
            record.update(state=state, **changes)
            _replace_json(path, record)

        destination, source = Path(record['destination']), Path(record['source'])
        try:
            _wait_for_exit(record['parentPid'], record['parentBirth'], timeout)
            if record['state'] == 'completed':
                status = preparation_status(destination, record['runUuid'])
                if not status['preparationReady'] or status['sourceDataDir'] != str(source):
                    raise ValueError('Completed preparation no longer matches its source')
                return record
            if record['runUuid'] and not destination.exists():
                raise ValueError('Frozen preparation destination is missing; recovery cannot recreate its identity')
            if destination.exists():
                run_uuid = _bound_run(destination, source, record['runUuid']) if record['runUuid'] \
                    else _existing_run(destination, source)
                record['runUuid'] = run_uuid
                save('preparing', error=None)
                result = resume_preparation(destination, run_uuid)
            else:
                save('preparing', error=None)
                result = prepare_migration(source, destination)
                record['runUuid'] = result['runUuid']
            status = preparation_status(destination, record['runUuid'])
            if status['sourceDataDir'] != str(source):
                raise ValueError('Preparation result belongs to another source')
            if result.get('readyForActivation') and status['preparationReady']:
                save('completed', error=None)
            else:
                save('error', error=result.get('error') or 'Preparation did not reach a ready checkpoint')
            return dict(record)
        except Exception as error:
            save('error', error=str(error))
            raise
