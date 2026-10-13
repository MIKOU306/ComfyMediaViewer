"""Classic UI handoff over verified migration workers; originals stay untouched."""
import json
import os
import shutil
import uuid
from pathlib import Path

from backend.storage.runtime_state import _safe_path, load_state, _uuid
from backend.migration.preparation_job import _check_absolute
from backend.migration.relocation_job import _birth, _wait_for_exit
from backend.migration.relocation_cutover import _write_new_json, _replace_json
from backend.migration.guard import DataWriterGuard

NATIVE = {
    'installation': ('.cmv-installation-jobs', 'backend.migration.installation_job', 'read_installation_job', 'run_installation_job'),
    'recovery': ('.cmv-recovery-jobs', 'backend.migration.recovery_job', 'read_recovery_job', 'run_recovery_job'),
    'recoverySelection': ('.cmv-recovery-selection-jobs', 'backend.migration.recovery_selection_job', 'read_selection_job', 'run_selection_job'),
    'relocation': ('.cmv-relocation-jobs', 'backend.migration.relocation_job', 'read_relocation_job', 'run_relocation_job'),
}


def selector_path(selector):
    selector = Path(selector).absolute()
    _check_absolute(selector)
    if selector.name != 'portable.flag' or (selector.exists() and not selector.is_file()):
        raise ValueError('资料选择路径无效')
    return selector


def bound_current(selector,current):
    from backend.migration.recovery_job import _selected
    current=Path(current).absolute()
    selected=_selected(selector_path(selector))
    if os.path.normcase(str(current))!=os.path.normcase(str(selected)):
        raise ValueError('当前资料库与便携选择不一致，请重新启动')
    return current


def import_path(selector, job_id):
    return _safe_path(selector.parent, '.cmv-classic-imports/' + _uuid(job_id) + '/job.json')


def read_import(selector, job_id):
    selector = selector_path(selector)
    path = import_path(selector, job_id)
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('导入任务记录过大')
    job = json.loads(raw)
    expected = {'version','jobId','selector','ownerDataDir','originalSource','source','destination',
                'parentPid','parentBirth','state','runUuid','error'}
    if not isinstance(job, dict) or set(job) != expected or job['version'] != 1 \
            or job['jobId'] != _uuid(job_id) or job['selector'] != str(selector):
        raise ValueError('导入任务不属于此程序')
    if job['state'] not in ('waiting-for-exit','copying','preparing','completed','error'):
        raise ValueError('导入任务状态无效')
    for name in ('ownerDataDir','originalSource','source','destination'):
        if not isinstance(job[name],str) or not Path(job[name]).is_absolute():
            raise ValueError('任务路径无效')
        _check_absolute(job[name])
    location=json.loads(_safe_path(path.parent,'location.json').read_text('utf-8'))
    if not isinstance(location,dict) or set(location)!={'directory'} or not isinstance(location['directory'],str) \
            or not Path(location['directory']).is_absolute():
        raise ValueError('导入材料位置无效')
    workspace=_check_absolute(location['directory'])
    marker=json.loads(_safe_path(workspace,'classic-import.json').read_text('utf-8'))
    if marker!={'version':1,'selector':str(selector),'jobId':_uuid(job_id)}:
        raise ValueError('导入材料不匹配此任务')
    if Path(job['source']) != workspace / 'legacy-data' or Path(job['destination']) != workspace / 'prepared':
        raise ValueError('导入目标不匹配冻结任务')
    if type(job['parentPid']) is not int or job['parentPid'] <= 0 or type(job['parentBirth']) not in (int,str):
        raise ValueError('导入进程身份无效')
    if job['runUuid'] is not None:
        _uuid(job['runUuid'])
    if job['error'] is not None and not isinstance(job['error'],str):
        raise ValueError('导入错误记录无效')
    original, owner = Path(job['originalSource']), Path(job['ownerDataDir'])
    if original == owner or original in owner.parents or owner in original.parents \
            or any(original == p or original in p.parents or p in original.parents for p in (selector.parent,workspace)):
        raise ValueError('旧数据必须与新程序数据完全分离')
    for other in (owner, selector.parent):
        if workspace == other or workspace in other.parents or other in workspace.parents:
            raise ValueError('导入材料必须与当前数据和程序目录完全分离')
    return job


def _activated_import(selector, preparation):
    """Allow post-cutover reads only with the exact completed activation proof."""
    from backend.migration.installation_job import read_installation_job
    tree = _safe_path(selector.parent, NATIVE['installation'][0])
    if not tree.exists():
        return False
    for folder in tree.iterdir():
        if not folder.is_dir():
            continue
        binding_path = _safe_path(folder, 'classic-import.json')
        if not binding_path.is_file():
            continue
        binding = json.loads(binding_path.read_text('utf-8'))
        expected = {'preparationJobId': preparation['jobId'], 'installationJobId': folder.name,
                    'source': preparation['source'], 'runUuid': preparation['runUuid'],
                    'ownerDataDir': preparation['ownerDataDir']}
        if binding != expected:
            continue
        installation = read_installation_job(selector, _uuid(folder.name))
        state = load_state(preparation['source'])
        if installation['state'] == 'completed' and installation['source'] == preparation['source'] \
                and installation['runUuid'] == preparation['runUuid'] and state['mode'] == 'sqlite' \
                and state.get('migrationRunUuid') == preparation['runUuid']:
            return True
    return False


def create_import(selector, current, source, destination):
    """destination is a new independent job container, not a live library."""
    selector = selector_path(selector)
    current, source, destination = (Path(p).absolute() for p in (current,source,destination))
    bound_current(selector,current)
    for p in (current,source,destination):
        _check_absolute(p)
    if not source.is_dir() or load_state(source)['mode'] != 'legacy-json':
        raise ValueError('请选择旧版 JSON 数据目录，数据库资料请使用完整恢复')
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('导入材料需要新的独立目录和已有父目录')
    for a,b in ((source,current),(source,selector.parent),(source,destination),(current,destination),(selector.parent,destination)):
        if a==b or a in b.parents or b in a.parents:
            raise ValueError('旧数据、当前资料库和导入材料必须完全分离')
    jobs = list_jobs(selector,current)
    if any(j['state'] in ('waiting-for-exit','copying','preparing') for j in jobs['preparation']):
        raise ValueError('已有未完成导入，请先检查原任务')
    birth=_birth(os.getpid())
    if birth is None:
        raise ValueError('未取得当前程序身份')
    job_id = str(uuid.uuid4())
    path = import_path(selector,job_id)
    path.parent.mkdir(parents=True)
    destination.mkdir()
    _write_new_json(destination / 'classic-import.json', {'version':1,'selector':str(selector),'jobId':job_id})
    record = {'version':1,'jobId':job_id,'selector':str(selector),'ownerDataDir':str(current),
              'originalSource':str(source),'source':str(destination/'legacy-data'),
              'destination':str(destination/'prepared'),'parentPid':os.getpid(),
              'parentBirth':birth,'state':'waiting-for-exit','runUuid':None,'error':None}
    _write_new_json(path.parent/'location.json', {'directory':str(destination)})
    _write_new_json(path,record)
    return read_import(selector,job_id)


def _import_sources(selector):
    folder = _safe_path(selector.parent,'.cmv-classic-imports')
    if not folder.exists():
        return []
    found = []
    for p in folder.iterdir():
        if not p.is_dir():
            continue
        try:
            job_id = _uuid(p.name)
        except (ValueError,RuntimeError):
            continue
        if not _safe_path(p,'job.json').exists():
            continue # An unpublished creation attempt remains recoverable evidence.
        found.append(read_import(selector,job_id))
        if len(found)>1000:
            raise ValueError('导入任务过多，请明确选择原任务')
    return found


def read_job(selector,current,kind,job_id):
    selector = selector_path(selector)
    current = str(bound_current(selector,current))
    if kind == 'preparation':
        job = read_import(selector,job_id)
        if job['ownerDataDir'] != current and not (job['source'] == current and _activated_import(selector, job)):
            raise ValueError('导入任务不属于当前资料库')
        if job.get('runUuid'):
            from backend.migration.status import preparation_status
            job = {**job,'status':preparation_status(job['destination'],job['runUuid'])}
        return job
    if kind not in NATIVE:
        raise ValueError('未知资料任务')
    import importlib
    _,module,reader,_ = NATIVE[kind]
    job = getattr(importlib.import_module(module),reader)(selector,_uuid(job_id))
    allowed = {current}
    allowed.update(j['source'] for j in _import_sources(selector)
                   if j['ownerDataDir']==current or (j['source']==current and _activated_import(selector,j)))
    if job['source'] not in allowed and job.get('target') != current and job.get('candidate') != current:
        raise ValueError('任务不属于当前资料库')
    return job


def _completed_native_job_is_foreign(selector, item, job_id, current, kind):
    """Skip a well-formed completed record bound to another selected library.

    Native readers deliberately require the selector to match the job's source
    while validating a job. Discovery may span several libraries over time, so
    peek only enough binding data to omit completed history for another library
    before invoking that strict reader. Malformed, oversized, or noncompleted
    records still go through the strict reader and retain its failure behavior.
    """
    path = _safe_path(item, 'job.json')
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return False
        record = json.loads(raw.decode('utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict) or record.get('state') != 'completed' \
            or record.get('jobId') != job_id or record.get('selector') != str(selector):
        return False
    fields = {
        'installation': ('source',),
        'recovery': ('source',),
        'recoverySelection': ('source', 'target'),
        'relocation': ('source', 'candidate'),
    }.get(kind, ())
    if not fields:
        return False
    current = os.path.normcase(os.path.abspath(current))
    roots = [record.get(field) for field in fields]
    # Only discard records with a valid path binding and a definite mismatch.
    if not roots or any(not isinstance(value, str) or not Path(value).is_absolute() for value in roots):
        return False
    return all(os.path.normcase(os.path.abspath(value)) != current for value in roots)


def bind_installation(selector,current,preparation_job_id,installation):
    preparation=read_job(selector,current,'preparation',preparation_job_id)
    if preparation['source']!=installation['source'] or preparation['runUuid']!=installation['runUuid']:
        raise ValueError('安装任务与冻结准备不匹配')
    path=_safe_path(Path(selector).parent,NATIVE['installation'][0]+'/'+installation['jobId']+'/classic-import.json')
    _write_new_json(path,{'preparationJobId':preparation_job_id,'installationJobId':installation['jobId'],
        'source':installation['source'],'runUuid':installation['runUuid'],'ownerDataDir':str(Path(current).absolute())})


def list_jobs(selector,current):
    selector = selector_path(selector)
    output = {'preparation':[],'installation':[],'recovery':[],'recoverySelection':[],'relocation':[]}
    for job in _import_sources(selector):
        if str(Path(current).absolute()) in (job['ownerDataDir'],job['source']):
            output['preparation'].append(read_job(selector,current,'preparation',job['jobId']))
    for kind,(folder,*_) in NATIVE.items():
        tree = _safe_path(selector.parent,folder)
        if not tree.exists():
            continue
        for item in tree.iterdir():
            if not item.is_dir():
                continue
            try:
                job_id = _uuid(item.name)
            except (ValueError,RuntimeError):
                continue
            if _completed_native_job_is_foreign(selector, item, job_id, current, kind):
                continue
            try:
                output[kind].append(read_job(selector,current,kind,job_id))
            except ValueError as error:
                if '不属于当前' not in str(error):
                    raise
            if len(output[kind])>1000:
                raise ValueError('任务过多')
    return output


def run_import(selector,job_id):
    from backend.migration.backup import create_snapshot, verify_snapshot
    from backend.migration.source_gate import verify_live_source
    from backend.migration.prepare import prepare_migration, resume_preparation
    selector = selector_path(selector)
    path = import_path(selector,job_id)
    with DataWriterGuard(path.parent):
        job = read_import(selector,job_id)
        def save(state,**values):
            job.update(state=state,**values)
            _replace_json(path,job)
        try:
            _wait_for_exit(job['parentPid'],job['parentBirth'],3600)
            from backend.migration.recovery_job import _selected
            selected = str(_selected(selector))
            if selected != job['ownerDataDir'] and not (selected == job['source'] and _activated_import(selector,job)):
                raise ValueError('导入所属的当前资料库已经改变，拒绝继续')
            if job['state']=='completed':
                from backend.migration.status import preparation_status
                status=preparation_status(job['destination'],job['runUuid'])
                if status['sourceDataDir']!=job['source'] or not (status['preparationReady'] or _activated_import(selector,job)):
                    raise ValueError('已完成的导入材料不再通过核对')
                return job
            copy=Path(job['source'])
            snapshot=copy.parent/'frozen'
            save('copying',error=None)
            if not snapshot.exists():
                create_snapshot(job['originalSource'],snapshot)
            verify_snapshot(snapshot)
            if not verify_live_source(snapshot,job['originalSource'])['sourceUnchanged']:
                raise ValueError('旧资料已变化，请保留此任务并重新创建导入')
            if not copy.exists():
                stage=_safe_path(copy.parent,'legacy-data.partial')
                stage.mkdir(exist_ok=True)
                manifest=verify_snapshot(snapshot)
                from backend.migration.backup import _hash
                for entry in manifest['files']:
                    target=_safe_path(stage,entry['path'])
                    target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists():
                        if target.stat().st_size!=entry['bytes'] or _hash(target)!=entry['sha256']:
                            raise ValueError('暂存副本已损坏，请保留材料重新准备')
                        continue
                    temporary=_safe_path(stage,entry['path']+'.cmv-copy-tmp')
                    with (snapshot/'files'/entry['path']).open('rb') as reader, temporary.open('wb') as writer:
                        shutil.copyfileobj(reader,writer)
                        writer.flush(); os.fsync(writer.fileno())
                    if _hash(temporary)!=entry['sha256']:
                        raise ValueError('暂存副本校验失败')
                    os.replace(temporary,target)
                if {p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file()}!={e['path'] for e in manifest['files']}:
                    raise ValueError('暂存副本包含未知材料')
                os.replace(stage,copy)
            # Verify every cloned byte before accepting it as migration input.
            manifest=verify_snapshot(snapshot)
            from backend.migration.backup import _hash
            for entry in manifest['files']:
                p=copy/entry['path']
                if p.stat().st_size!=entry['bytes'] or _hash(p)!=entry['sha256']:
                    raise ValueError('旧资料副本不完整，拒绝继续')
            save('preparing')
            prepared=Path(job['destination'])
            if job['runUuid']:
                result=resume_preparation(prepared,job['runUuid'])
            elif prepared.exists():
                from backend.migration.preparation_job import _existing_run
                result=resume_preparation(prepared,_existing_run(prepared,copy))
            else:
                result=prepare_migration(copy,prepared)
            save('completed' if result['readyForActivation'] else 'error',runUuid=result['runUuid'],error=result.get('error'))
        except Exception as error:
            save('error',error=str(error))
            raise
        return job


def run_worker(selector,current,kind,job_id):
    job=read_job(selector,current,kind,job_id)
    if kind=='preparation':
        return run_import(selector,job_id)
    import importlib
    folder,module,_,runner=NATIVE[kind]
    path=_safe_path(Path(selector).parent,folder+'/'+_uuid(job_id)+'/job.json')
    result=getattr(importlib.import_module(module),runner)(path)
    if kind=='installation' and result.get('state')=='completed':
        binding=_safe_path(path.parent,'classic-import.json')
        if binding.exists():
            binding=json.loads(binding.read_text('utf-8'))
            preparation=read_import(selector,binding['preparationJobId'])
            if binding!={'preparationJobId':preparation['jobId'],'installationJobId':job_id,
                         'source':result['source'],'runUuid':result['runUuid'],'ownerDataDir':preparation['ownerDataDir']} \
                    or preparation['source']!=result['source'] or preparation['runUuid']!=result['runUuid']:
                raise ValueError('安装选择与准备任务不匹配')
            # Select only the verified converted COPY. Never activate the original
            # legacy source or replace the existing user's catalog.
            state=load_state(result['source'])
            if state['mode']!='sqlite' or state.get('migrationRunUuid')!=result['runUuid']:
                raise ValueError('新资料副本尚未通过数据库验证')
            with DataWriterGuard(Path(selector).parent):
                bound_current(selector,current)
                if str(Path(current).absolute()) not in (preparation['ownerDataDir'], preparation['source']):
                    raise ValueError('安装任务不属于当前资料库')
                selected=json.loads(Path(selector).read_text('utf-8'))
                selected.update(dir=result['source'])
                _replace_json(Path(selector),selected)
    return result
