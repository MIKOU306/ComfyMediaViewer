"""Crash-resumable copy phase for offline data relocation jobs."""
from __future__ import annotations

import hashlib, json, os, shutil, sqlite3, uuid
from contextlib import closing
from pathlib import Path

from .data_relocation import relocation_preflight, _copy_targets, _compare_relocated_database
from .preflight import scan_manifest
from .sqlite_backup import backup_database
from .relocation_cutover import _write_new_json
from .guard import DataWriterGuard
from ..storage.runtime_state import load_state, _safe_path


def _atomic_json(path, value):
    path=Path(path)
    temporary=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    with temporary.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,sort_keys=True,ensure_ascii=False);stream.write('\n')
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def _settle_source_databases(source):
    """Recover SQLite-owned journals before freezing the first copy manifest."""
    with DataWriterGuard(source):
        manifest=scan_manifest(source)
        if not manifest['ready']:
            raise ValueError('Source preflight issues prevent SQLite recovery')
        for entry in manifest['files']:
            if Path(entry['path']).suffix.lower() not in ('.sqlite','.db'):
                continue
            database=_safe_path(source,entry['path'])
            with database.open('rb') as stream:
                if stream.read(16)!=b'SQLite format 3\0':
                    raise ValueError('Unrecognized database material: '+entry['path'])
            # mode=rw never creates a missing database. SQLite itself owns hot
            # journal recovery; do not remove journal/WAL files manually.
            with closing(sqlite3.connect(database.as_uri()+'?mode=rw',uri=True,timeout=5)) as connection:
                if connection.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                    raise ValueError('Source SQLite recovery integrity check failed')
                if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('Source SQLite recovery foreign key check failed')
                checkpoint=connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint and checkpoint[0]:
                    raise ValueError('Source database still has an active writer')


def _copy_file(source,target,entry,owned_part,checkpoint):
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_file() and target.stat().st_size==entry['bytes'] and _digest(target)==entry['sha256']:
        return
    if target.exists():
        raise ValueError('Unverified staging material will not be overwritten: '+str(target))
    # A part file is allowed only in the job's private staging tree and is rebuilt in place.
    if owned_part.exists():
        if not owned_part.is_file(): raise ValueError('Unexpected owned part material')
        owned_part.unlink()
    owned_part.parent.mkdir(parents=True,exist_ok=True)
    count=0;digest=hashlib.sha256()
    with source.open('rb') as reader,owned_part.open('xb') as writer:
        for chunk in iter(lambda:reader.read(1024*1024),b''):
            count+=len(chunk)
            if count>entry['bytes']: raise ValueError('Source grew while copying '+entry['path'])
            writer.write(chunk);digest.update(chunk);checkpoint('file-part:'+entry['path'])
        writer.flush();os.fsync(writer.fileno())
    if count!=entry['bytes'] or digest.hexdigest()!=entry['sha256']:
        raise ValueError('Copied material differs from source manifest: '+entry['path'])
    try: os.link(owned_part,target)
    except FileExistsError: raise ValueError('Copy target appeared during publication: '+str(target))
    owned_part.unlink()


def prepare_job_relocation_files(source,candidate,job_path,*,_checkpoint=lambda phase:None):
    """Prepare or resume one job's copy while the caller holds the jobdir guard.

    Source writer guard remains held from manifest revalidation through receipt publication.
    Candidate is published by a same-volume exclusive directory rename after full verification.
    """
    source=Path(source).absolute();candidate=Path(candidate).absolute();job_path=Path(job_path).absolute()
    jobdir=job_path.parent
    _safe_path(source.parent,source.name)
    _safe_path(candidate.parent,candidate.name)
    if job_path.name!='job.json' or not jobdir.is_dir(): raise ValueError('Invalid relocation job path')
    _safe_path(jobdir,'copy-plan.json')
    plan_path=jobdir/'copy-plan.json'
    job=json.loads(job_path.read_text('utf-8'))
    job_id=job.get('jobId')
    if not isinstance(job_id,str) or str(uuid.UUID(job_id))!=job_id or job.get('source')!=str(source) or job.get('candidate')!=str(candidate):
        raise ValueError('Job identity or relocation paths differ')
    staging=candidate.parent/('.cmv-relocation-staging-'+job_id)
    owner=candidate.parent/('.cmv-relocation-staging-'+job_id+'.owner.json')
    parts_dir=candidate.parent/('.cmv-relocation-parts-'+job_id)
    parts_owner=candidate.parent/('.cmv-relocation-parts-'+job_id+'.owner.json')
    dbtmp=candidate.parent/('.cmv-relocation-sqlite-tmp-'+job_id)
    dbtmp_owner=candidate.parent/('.cmv-relocation-sqlite-tmp-'+job_id+'.owner.json')
    for path in (plan_path,job_path,staging,owner,parts_dir,parts_owner,dbtmp,dbtmp_owner):
        _safe_path(path.parent,path.name)
    if plan_path.exists():
        plan=json.loads(plan_path.read_text('utf-8'))
        if plan.get('version')!=1 or plan.get('jobId')!=job_id or plan.get('source')!=str(source) or plan.get('candidate')!=str(candidate) or plan.get('staging')!=str(staging):
            raise ValueError('Persisted copy plan differs from current source/job')
    else:
        if candidate.exists() or staging.exists() or owner.exists(): raise ValueError('Unknown candidate or staging material exists')
        _settle_source_databases(source)
        preflight=relocation_preflight(source,candidate)
        if preflight['blockers']: raise ValueError('Relocation preflight has blockers')
        manifest=preflight['sourceManifest'];storage=preflight['storage']
        expected={'version':1,'jobId':job_id,'source':str(source),'candidate':str(candidate),
                  'sourceManifest':manifest,'storage':storage,'staging':str(staging)}
        _write_new_json(plan_path,expected)
        plan=expected
        _checkpoint('plan-published')
    manifest=plan['sourceManifest'];storage=plan['storage']
    if candidate.exists():
        # Published candidate is reusable only with matching durable receipt.
        receipt_path=_safe_path(candidate,'relocation-databases.json')
        receipt=json.loads(receipt_path.read_text('utf-8'))
        expected_receipt=(receipt.get('sourceManifest')==manifest and receipt.get('storage')==storage and receipt.get('source')==str(source))
        marker=json.loads(owner.read_text('utf-8')) if owner.exists() else None
        if not expected_receipt or marker!={'version':1,'jobId':job_id,'source':str(source),'candidate':str(candidate)}:
            raise ValueError('Published candidate receipt differs from copy plan')
        with DataWriterGuard(candidate), DataWriterGuard(source):
            if scan_manifest(source)!=manifest or load_state(source)!=storage:
                raise ValueError('Relocation source changed since copy plan')
            entries={entry['path']:entry for entry in manifest['files']}
            targets=_copy_targets(manifest)
            dbs=[n for n in entries if Path(n).suffix.lower() in ('.sqlite','.db')]
            for relative,entry in entries.items():
                if relative in dbs or relative=='storage-state.json' or any(relative==db+s for db in dbs for s in ('-wal','-shm','-journal')): continue
                target=_safe_path(candidate,targets.get(relative,relative))
                if not target.is_file() or target.stat().st_size!=entry['bytes'] or _digest(target)!=entry['sha256']:
                    raise ValueError('Published candidate file differs from plan: '+relative)
            for relative in dbs:
                target=_safe_path(candidate,relative)
                with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as c:
                    if c.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or c.execute('PRAGMA foreign_key_check').fetchone() is not None:
                        raise ValueError('Published database integrity check failed: '+relative)
                _compare_relocated_database(_safe_path(source,relative),target,source,candidate)
            expected_paths={targets.get(n,n) for n in entries if n not in dbs and n!='storage-state.json' and not any(n==db+s for db in dbs for s in ('-wal','-shm','-journal'))}
            expected_paths.update(dbs);expected_paths.update({'relocation-databases.json','.cmv-writer.lock'})
            allowed_dirs=set()
            for name in expected_paths:
                parent=Path(name).parent
                while str(parent) not in ('','.'): allowed_dirs.add(parent.as_posix());parent=parent.parent
            for item in candidate.rglob('*'):
                if item.is_symlink() or getattr(item.stat(),'st_file_attributes',0)&0x400:
                    raise ValueError('Link found in published candidate')
                rel=item.relative_to(candidate).as_posix()
                if item.is_file() and rel not in expected_paths: raise ValueError('Unknown published candidate material: '+rel)
                if item.is_dir() and rel not in allowed_dirs: raise ValueError('Unknown published candidate directory: '+rel)
        return receipt
    if not owner.exists():
        if staging.exists(): raise ValueError('Unknown staging directory will not be adopted')
        marker={'version':1,'jobId':job_id,'source':str(source),'candidate':str(candidate)}
        _write_new_json(owner,marker)
        _checkpoint('staging-owner-published')
    elif json.loads(owner.read_text('utf-8'))!={'version':1,'jobId':job_id,'source':str(source),'candidate':str(candidate)}:
        raise ValueError('Staging owner identity differs')
    if not staging.exists():
        staging.mkdir()
        _checkpoint('staging-created')
    if not staging.is_dir(): raise ValueError('Owned staging path is not a directory')
    owned_identity={'version':1,'jobId':job_id,'source':str(source),'candidate':str(candidate)}
    if parts_dir.exists() and (not parts_owner.exists() or json.loads(parts_owner.read_text('utf-8'))!=owned_identity):
        raise ValueError('Unknown copy parts will not be adopted')
    if not parts_owner.exists(): _write_new_json(parts_owner,owned_identity)
    parts_dir.mkdir(exist_ok=True)
    with DataWriterGuard(source), DataWriterGuard(staging) as stage_guard:
        if scan_manifest(source)!=manifest or load_state(source)!=storage:
            raise ValueError('Relocation source changed since copy plan')
        _checkpoint('source-verified')
        if shutil.disk_usage(candidate.parent).free < sum(entry['bytes'] for entry in manifest['files'])+64*1024*1024:
            raise ValueError('Insufficient relocation staging space')
        entries={entry['path']:entry for entry in manifest['files']}
        dbs=[name for name in entries if Path(name).suffix.lower() in ('.sqlite','.db')]
        deferred=[name for name in entries if name=='storage-state.json' or any(name==db+s for db in dbs for s in ('-wal','-shm','-journal'))]
        copied=[name for name in entries if name not in dbs and name not in deferred]
        targets=_copy_targets(manifest)
        for relative in copied:
            target=_safe_path(staging,targets.get(relative,relative))
            part=_safe_path(parts_dir,hashlib.sha256(relative.encode()).hexdigest()+'.part')
            part_owner=part.with_suffix('.owner.json')
            identity={'jobId':job_id,'relative':relative,'sourceManifestSha256':hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
            if part.exists() and (not part_owner.exists() or json.loads(part_owner.read_text('utf-8'))!=identity):
                raise ValueError('Unknown part file will not be overwritten: '+str(part))
            if not part_owner.exists(): _write_new_json(part_owner,identity)
            _copy_file(_safe_path(source,relative),target,entries[relative],part,_checkpoint)
            part_owner.unlink(missing_ok=True)
            _checkpoint('file:'+relative)
        db_identity={'jobId':job_id,'source':str(source),'candidate':str(candidate),
                     'sourceManifestSha256':hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
        if dbtmp.exists() and (not dbtmp_owner.exists() or json.loads(dbtmp_owner.read_text('utf-8'))!=db_identity):
            raise ValueError('Unknown SQLite temporary directory will not be adopted')
        if not dbtmp_owner.exists(): _write_new_json(dbtmp_owner,db_identity)
        dbtmp.mkdir(exist_ok=True)
        for relative in dbs:
            origin=_safe_path(source,relative);target=_safe_path(staging,relative)
            if target.exists():
                try:
                    with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as c:
                        valid=c.execute('PRAGMA integrity_check').fetchall()==[('ok',)] and c.execute('PRAGMA foreign_key_check').fetchone() is None
                    if valid: _compare_relocated_database(origin,target,source,staging); continue
                except (sqlite3.Error,ValueError): pass
                raise ValueError('Unverified database material will not be overwritten: '+relative)
            temp=dbtmp/(hashlib.sha256(relative.encode()).hexdigest()+'.sqlite')
            _safe_path(temp.parent,temp.name)
            if not temp.exists():
                backup_database(origin,temp)
            with closing(sqlite3.connect(temp.as_uri()+'?mode=ro',uri=True)) as c:
                if c.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or c.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('Database backup failed integrity checks: '+relative)
            _compare_relocated_database(origin,temp,source,staging)
            target.parent.mkdir(parents=True,exist_ok=True);os.replace(temp,target)
            _checkpoint('database:'+relative)
        # Reject every unaccounted staging entry before publishing it.
        allowed_files=set(targets.get(n,n) for n in copied)|set(dbs)|{'relocation-databases.json','.cmv-writer.lock'}
        for path in staging.rglob('*'):
            if path.is_symlink() or getattr(path.stat(),'st_file_attributes',0)&0x400:
                raise ValueError('Link found in owned staging tree')
            if path.is_file() and path.relative_to(staging).as_posix() not in allowed_files:
                raise ValueError('Unknown staging material will not be published: '+str(path))
            if path.is_dir():
                rel=path.relative_to(staging).as_posix()
                required_dirs=set()
                for name in allowed_files:
                    parent=Path(name).parent
                    while str(parent) not in ('','.'): required_dirs.add(parent.as_posix());parent=parent.parent
                if rel not in required_dirs:
                    raise ValueError('Unknown staging directory will not be published: '+str(path))
        # SQLite crash leftovers stay in this job-owned directory; never sweep unknown names.
        if scan_manifest(source)!=manifest: raise ValueError('Relocation source changed while copying')
        receipt={'version':1,'phase':'files-without-authority','source':str(source),'databases':dbs,
                 'storage':storage,'copiedFiles':copied,'deferredFiles':deferred,'sourceManifest':manifest,
                 'copyTargets':targets,'authorityChanged':False,'requiresRemainingFiles':False,'requiresPathRewrites':True}
        receipt_path=staging/'relocation-databases.json'
        if receipt_path.exists():
            if json.loads(receipt_path.read_text('utf-8'))!=receipt:
                raise ValueError('Staging receipt differs from verified copy plan')
        else:
            # Keep interrupted serialization in the identity-bound sibling,
            # never among candidate files that must all be accounted for.
            temporary=_safe_path(parts_dir,'receipt.json.part')
            raw=(json.dumps(receipt,sort_keys=True,ensure_ascii=False)+'\n').encode('utf-8')
            temporary_owner=_safe_path(parts_dir,'receipt-part.owner.json')
            identity={'jobId':job_id,'receiptSha256':hashlib.sha256(raw).hexdigest()}
            if temporary.exists() and not temporary_owner.exists():
                raise ValueError('Unclaimed receipt temporary material')
            if temporary_owner.exists():
                if json.loads(temporary_owner.read_text('utf-8'))!=identity:
                    raise ValueError('Receipt temporary ownership differs')
            else:
                _write_new_json(temporary_owner,identity)
            if temporary.exists():
                if not temporary.is_file():
                    raise ValueError('Unexpected receipt temporary material')
                temporary.unlink()
            with temporary.open('xb') as stream:
                for offset in range(0,len(raw),128):
                    stream.write(raw[offset:offset+128])
                    _checkpoint('receipt-writing')
                stream.flush();os.fsync(stream.fileno())
            os.link(temporary,receipt_path)
            temporary.unlink()
        _checkpoint('receipt-published')
        # Exclusive publication: destination must still be absent.
        if candidate.exists(): raise ValueError('Candidate appeared before exclusive publication')
        stage_guard.release()
        with DataWriterGuard(candidate.parent):
            if candidate.exists(): raise ValueError('Candidate appeared before exclusive publication')
            os.rename(staging,candidate)
        _checkpoint('directory-published')
        return receipt
