"""Inspect an offline relocation without copying or changing authority."""
from pathlib import Path

from .guard import DataWriterGuard
from .preflight import scan_manifest
from ..storage.runtime_state import load_state,validate_state
from ..operations.journal import OperationJournal
from .sqlite_backup import backup_database
import json
import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from itertools import zip_longest
from .backup import _safe_relative
from ..storage.runtime_state import _safe_path

_RELOCATION_PATH_SCHEMA = "CREATE TABLE data_relocation_paths (version INTEGER PRIMARY KEY CHECK(version=1), source TEXT NOT NULL, destination TEXT NOT NULL, result_json TEXT NOT NULL)"
_RELOCATION_FILES={'relocation-databases.json','relocation-paths.json','relocation-cutover.json',
                   'relocation-cutover-cancelled.json','relocation-ledger-history.json'}


def _copy_targets(manifest):
    generation=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
    return {entry['path']:'relocation-history/'+generation+'/'+entry['path']
            for entry in manifest['files'] if entry['path'] in _RELOCATION_FILES}


def backup_relocation_databases(source, destination, *, include_files=False):
    """Prepare database-only material, never a runnable/activated data directory."""
    report=relocation_preflight(source,destination)
    if report['blockers']:
        raise ValueError('Relocation preflight has blockers')
    source=Path(report['source'])
    destination=Path(report['destination'])
    if destination.exists():
        raise ValueError('Database snapshot destination must be new')
    with DataWriterGuard(source):
        before=scan_manifest(source)
        if before!=report['sourceManifest'] or load_state(source)!=report['storage']:
            raise ValueError('Source changed since relocation preflight')
        databases=[entry['path'] for entry in report['files']
                   if Path(entry['path']).suffix.lower() in ('.sqlite','.db')]
        required=sum(entry['bytes'] for entry in report['files'])+64*1024*1024
        if shutil.disk_usage(destination.parent).free<required:
            raise ValueError('Insufficient relocation staging space')
        destination.mkdir()
        outputs=[]
        copied=[]
        skipped=[]
        copy_targets=_copy_targets(before)
        try:
            with DataWriterGuard(destination):
                for relative in databases:
                    origin=source/relative
                    with origin.open('rb') as reader:
                        if reader.read(16)!=b'SQLite format 3\0':
                            raise ValueError('Unrecognized database material: '+relative)
                    target=destination/relative
                    backup_database(origin,target)
                    outputs.append(relative)
                if include_files:
                    for entry in report['files']:
                        relative=entry['path']
                        if relative in databases:
                            continue
                        if relative=='storage-state.json' or any(relative==name+suffix
                                for name in databases for suffix in ('-wal','-shm','-journal')):
                            skipped.append(relative)
                            continue
                        origin=source/_safe_relative(relative)
                        target=destination/_safe_relative(copy_targets.get(relative,relative))
                        target.parent.mkdir(parents=True,exist_ok=True)
                        digest=hashlib.sha256()
                        count=0
                        with origin.open('rb') as reader,target.open('xb') as writer:
                            for chunk in iter(lambda:reader.read(1024*1024),b''):
                                count+=len(chunk)
                                if count>entry['bytes']:
                                    raise ValueError('Source grew while copying '+relative)
                                writer.write(chunk);digest.update(chunk)
                            writer.flush();os.fsync(writer.fileno())
                        if count!=entry['bytes'] or digest.hexdigest()!=entry['sha256']:
                            raise ValueError('Copied material differs from source manifest: '+relative)
                        with target.open('rb') as reader:
                            checked=hashlib.file_digest(reader,'sha256').hexdigest()
                        if checked!=entry['sha256']:
                            raise ValueError('Destination material verification failed: '+relative)
                        copied.append(relative)
                after=scan_manifest(source)
                if after!=before:
                    raise ValueError('Source changed while preparing database snapshots')
                receipt={'version':1,'phase':'files-without-authority' if include_files else 'database-snapshots-only','source':str(source),
                         'databases':outputs,'storage':report['storage'],
                         'copiedFiles':copied,'deferredFiles':skipped,'sourceManifest':before,
                         'copyTargets':copy_targets,
                         'authorityChanged':False,'requiresRemainingFiles':not include_files,
                         'requiresPathRewrites':True}
                # The final name is a completion boundary used by the worker.
                # Publish only after a complete, flushed JSON document exists;
                # a crash during serialization must not masquerade as a receipt.
                from .relocation_cutover import _write_new_json
                _write_new_json(destination/'relocation-databases.json',receipt)
                return receipt
        except BaseException:
            # Retain generated evidence; never recursively remove a failed target.
            raise


def prepare_relocation_files(source,destination):
    return backup_relocation_databases(source,destination,include_files=True)


def _compare_relocated_database(source_database,candidate_database,source,candidate,*,paths_rewritten=False):
    """Compare copied rows, optionally accounting for committed path rewrites.

    Database snapshots are verified before rewriting and must match the source
    byte-for-byte at the row level. The final candidate reconciliation runs
    after ``rewrite_relocation_paths`` and expects live trash payload paths to
    point into the candidate directory.
    """
    quote=lambda name:'"'+name.replace('"','""')+'"'
    with closing(sqlite3.connect(source_database.as_uri()+'?mode=ro',uri=True)) as before, \
         closing(sqlite3.connect(candidate_database.as_uri()+'?mode=ro',uri=True)) as after:
        schema_sql="SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name!='data_relocation_paths' ORDER BY type,name"
        schema=before.execute(schema_sql).fetchall()
        if schema!=after.execute(schema_sql).fetchall():
            raise ValueError('Candidate database schema differs: '+candidate_database.name)
        prior_schema=before.execute("SELECT sql FROM sqlite_master WHERE name='data_relocation_paths'").fetchone()
        candidate_schema=after.execute("SELECT sql FROM sqlite_master WHERE name='data_relocation_paths'").fetchone()
        for connection,ledger_schema in ((before,prior_schema),(after,candidate_schema)):
            if ledger_schema and (ledger_schema[0]!=_RELOCATION_PATH_SCHEMA or
                    connection.execute('SELECT COUNT(*) FROM data_relocation_paths').fetchone()[0]>1):
                raise ValueError('Unrecognized relocation ledger structure or rows')
        if prior_schema and not candidate_schema:
            raise ValueError('Candidate lost previous relocation ledger')
        if not prior_schema and candidate_schema:
            added=after.execute('SELECT source,destination FROM data_relocation_paths WHERE version=1').fetchone()
            if added!=(str(source),str(candidate)):
                raise ValueError('Unexpected candidate relocation ledger')
        if prior_schema:
            prior=before.execute('SELECT source,destination,result_json FROM data_relocation_paths WHERE version=1').fetchone()
            retained=after.execute('SELECT source,destination,result_json FROM data_relocation_paths WHERE version=1').fetchone()
            if prior and retained!=prior:
                expected={'version':1,'database':candidate_database.relative_to(candidate).as_posix(),
                          'schema':prior_schema[0],'rows':[list(prior)]}
                history=json.loads(_safe_path(candidate,'relocation-ledger-history.json').read_text('utf-8'))
                if history!=expected:
                    raise ValueError('Previous relocation ledger history differs')
        for kind,name,_,_ in schema:
            if kind!='table':
                continue
            columns=[row[1] for row in before.execute('PRAGMA table_info('+quote(name)+')')]
            payload=columns.index('payload_path') if name=='trash_item' else None
            order=[column for column in columns if column!='payload_path' or name!='trash_item']
            query='SELECT '+','.join(map(quote,columns))+' FROM '+quote(name)+' ORDER BY '+','.join(map(quote,order))
            sentinel=object()
            for left,right in zip_longest(before.execute(query),after.execute(query),fillvalue=sentinel):
                if left is sentinel or right is sentinel:
                    raise ValueError('Candidate database row count differs: '+name)
                if payload is not None and paths_rewritten:
                    original=Path(left[payload])
                    if original.is_relative_to(source):
                        relative=original.relative_to(source)
                        if not relative.parts or relative.parts[0]!='trash':
                            raise ValueError('Unexpected source trash payload location')
                        values=list(left);values[payload]=str(_safe_path(candidate,relative.as_posix()));left=tuple(values)
                if left!=right:
                    raise ValueError('Candidate database row differs: '+name)


def verify_relocation_candidate(candidate):
    """Final read-only reconciliation under both guards; never activate a copy."""
    return with_verified_relocation(candidate,lambda report,receipt:report)


def with_verified_relocation(candidate,action,*,_recovering=False):
    """Run a cutover action only after reconciliation, with both guards held.

    The caller owns durable intent, publication and recovery. Exceptions release
    guards but never undo an action's published state implicitly.
    """
    if not callable(action):
        raise TypeError('Relocation action must be callable')
    candidate=Path(candidate).absolute()
    receipt_path=_safe_path(candidate,'relocation-databases.json')
    with DataWriterGuard(candidate):
        receipt=json.loads(receipt_path.read_text('utf-8'))
        if receipt.get('phase')!='files-without-authority' or receipt.get('authorityChanged') is not False:
            raise ValueError('Relocation candidate is not awaiting reconciliation')
        pointer=_safe_path(candidate,'storage-state.json')
        if pointer.exists() and not _recovering:
            raise ValueError('Relocation candidate already has an authority pointer')
        source=Path(receipt['source']).absolute()
        if source==candidate or source in candidate.parents or candidate in source.parents:
            raise ValueError('Relocation requires separate directory trees')
        with DataWriterGuard(source):
            manifest=scan_manifest(source)
            if _recovering:
                intent=json.loads(_safe_path(candidate,'relocation-cutover.json').read_text('utf-8'))
                digest=hashlib.sha256(json.dumps(receipt['sourceManifest'],sort_keys=True,
                                                ensure_ascii=False).encode('utf-8')).hexdigest()
                if intent.get('version')!=1 or intent.get('phase') not in (
                        'prepared','source-retired','authority-published','selector-published') or \
                        intent.get('source')!=str(source) or intent.get('candidate')!=str(candidate) or \
                        intent.get('storage')!=receipt['storage'] or intent.get('sourceManifestSha256')!=digest:
                    raise ValueError('Recovery intent differs from source/candidate evidence')
                retired=_safe_path(source,'relocation-retired.json')
                expected={'version':1,'source':str(source),'candidate':str(candidate),
                          'selector':intent['selector'],'sourceManifestSha256':digest}
                if retired.exists():
                    if json.loads(retired.read_text('utf-8'))!=expected:
                        raise ValueError('Recovery source retirement evidence differs')
                    manifest['files']=[entry for entry in manifest['files'] if entry['path']!='relocation-retired.json']
                elif intent['phase']!='prepared':
                    raise ValueError('Recovery source retirement evidence is missing')
                source_state=validate_state(source,json.loads(_safe_path(source,'storage-state.json').read_text('utf-8')))
                if pointer.exists():
                    if validate_state(candidate,json.loads(pointer.read_text('utf-8')))!=receipt['storage']:
                        raise ValueError('Recovery candidate authority differs')
                elif intent['phase'] in ('authority-published','selector-published'):
                    raise ValueError('Recovery candidate authority is missing')
            else:
                source_state=load_state(source)
            if manifest!=receipt['sourceManifest'] or source_state!=receipt['storage']:
                raise ValueError('Relocation source changed after preparation')
            entries={entry['path']:entry for entry in receipt['sourceManifest']['files']}
            expected_databases={relative for relative in entries
                                if Path(relative).suffix.lower() in ('.sqlite','.db')}
            expected_deferred={relative for relative in entries
                               if relative=='storage-state.json' or any(
                                   relative==name+suffix for name in expected_databases
                                   for suffix in ('-wal','-shm','-journal'))}
            expected_copied=set(entries)-expected_databases-expected_deferred
            copy_targets=_copy_targets(receipt['sourceManifest'])
            candidate_manifest=scan_manifest(candidate)
            allowed_files=expected_databases | {copy_targets.get(name,name) for name in expected_copied}
            allowed_files |= _RELOCATION_FILES
            if _recovering:
                allowed_files.add('storage-state.json')
            unexpected={entry['path'] for entry in candidate_manifest['files']}-allowed_files
            if unexpected or not candidate_manifest['ready']:
                raise ValueError('Candidate contains unexpected or unsafe material: '+', '.join(sorted(unexpected)))
            if receipt.get('copyTargets',{})!=copy_targets:
                raise ValueError('Relocation history targets differ from source manifest')
            for field,expected in (('databases',expected_databases),
                                   ('deferredFiles',expected_deferred),('copiedFiles',expected_copied)):
                values=receipt.get(field)
                if not isinstance(values,list) or not all(isinstance(value,str) for value in values) or \
                        len(values)!=len(set(values)) or set(values)!=expected:
                    raise ValueError('Relocation receipt coverage differs from source manifest: '+field)
            for relative in receipt['copiedFiles']:
                target=_safe_path(candidate,copy_targets.get(relative,relative))
                entry=entries[relative]
                if not target.is_file() or target.stat().st_size!=entry['bytes']:
                    raise ValueError('Candidate material missing or size changed: '+relative)
                with target.open('rb') as reader:
                    digest=hashlib.file_digest(reader,'sha256').hexdigest()
                if digest!=entry['sha256']:
                    raise ValueError('Candidate material changed: '+relative)
            for relative in receipt['databases']:
                database=_safe_path(candidate,relative)
                with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as connection:
                    if connection.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                        raise ValueError('Candidate database integrity check failed: '+relative)
                    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                        raise ValueError('Candidate database foreign key check failed: '+relative)
                _compare_relocated_database(_safe_path(source,relative),database,source,candidate,
                                            paths_rewritten=True)
            if receipt['storage']['mode']=='sqlite':
                validate_state(candidate,receipt['storage'])
            path_report=json.loads(_safe_path(candidate,'relocation-paths.json').read_text('utf-8'))
            if path_report.get('phase')!='paths-rewritten-without-authority' or path_report.get('source')!=str(source):
                raise ValueError('Candidate path rewrite report is invalid')
            ledger_database=_safe_path(candidate,'trash-state.sqlite')
            if not ledger_database.exists():
                ledger_database=_safe_path(candidate,receipt['storage']['database'])
            with closing(sqlite3.connect(ledger_database.as_uri()+'?mode=ro',uri=True)) as connection:
                ledger=connection.execute('SELECT source,destination,result_json FROM data_relocation_paths WHERE version=1').fetchone()
                if not ledger or ledger[:2]!=(str(source),str(candidate)) or json.loads(ledger[2])!=path_report:
                    raise ValueError('Candidate path report differs from committed ledger')
            report={'version':1,'phase':'reconciled-without-authority','source':str(source),
                    'candidate':str(candidate),'filesVerified':len(receipt['copiedFiles']),
                    'databasesVerified':len(receipt['databases']),'authorityChanged':False,
                    'note':'Guards released on return; activation must revalidate while holding guards.'}
            return action(report,receipt)


def rewrite_relocation_paths(candidate):
    """Rewrite only candidate live trash paths; historical journal is immutable."""
    candidate=Path(candidate).resolve(strict=True)
    receipt_path=_safe_path(candidate,'relocation-databases.json')
    with DataWriterGuard(candidate):
        receipt=json.loads(receipt_path.read_text('utf-8'))
        if receipt.get('phase')!='files-without-authority' or receipt.get('authorityChanged') is not False:
            raise ValueError('Candidate is not awaiting path rewrites')
        source=Path(receipt['source']).resolve()
        state=receipt['storage']
        if state['mode']!='sqlite':
            raise ValueError('This path rewrite requires a SQLite candidate')
        primary=_safe_path(candidate,state['database'])
        with closing(sqlite3.connect(primary.as_uri()+'?mode=ro',uri=True)) as roots:
            for (root,) in roots.execute('SELECT path FROM library_root'):
                if Path(root).is_relative_to(source):
                    raise ValueError('Media roots inside the data directory require a separate media relocation')
        # CatalogTrash owns a separate index database; never assume it is in catalog.sqlite.
        trash_database=_safe_path(candidate,'trash-state.sqlite')
        database=trash_database if trash_database.exists() else primary
        changes=[]
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory=sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            schema=connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='data_relocation_paths'").fetchone()
            if schema and schema[0]!=_RELOCATION_PATH_SCHEMA:
                raise ValueError('Unrecognized relocation path ledger schema')
            if not schema:
                connection.execute(_RELOCATION_PATH_SCHEMA)
            saved=connection.execute('SELECT source,destination,result_json FROM data_relocation_paths WHERE version=1').fetchone()
            if saved and saved[:2]!=(str(source),str(candidate)):
                if saved[1]!=str(source):
                    raise ValueError('Previous relocation ledger does not belong to source directory')
                history={'version':1,'database':database.relative_to(candidate).as_posix(),
                         'schema':_RELOCATION_PATH_SCHEMA,'rows':[list(saved)]}
                archive=_safe_path(candidate,'relocation-ledger-history.json')
                if archive.exists():
                    if json.loads(archive.read_text('utf-8'))!=history:
                        raise ValueError('Previous relocation ledger archive differs')
                else:
                    # Persist history before replacing the active row. If the
                    # transaction rolls back, retry must match this same record.
                    from .relocation_cutover import _write_new_json
                    _write_new_json(archive,history)
                connection.execute('DELETE FROM data_relocation_paths WHERE version=1')
                saved=None
            if saved:
                if saved[:2]!=(str(source),str(candidate)):
                    raise ValueError('Candidate already belongs to another relocation')
                result=json.loads(saved[2])
                connection.rollback()
                output=_safe_path(candidate,'relocation-paths.json')
                if output.exists():
                    if json.loads(output.read_text('utf-8'))!=result:
                        raise ValueError('Relocation path report differs from committed ledger')
                else:
                    with output.open('x',encoding='utf-8') as stream:
                        json.dump(result,stream,ensure_ascii=False,indent=2)
                        stream.flush();os.fsync(stream.fileno())
                return result
            has_trash=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trash_item'").fetchone()
            if has_trash:
                from ..operations.trash import CatalogTrash
                CatalogTrash._check(connection)
                rows=connection.execute('SELECT key,payload_path,state FROM trash_item').fetchall()
                for key,path,status in rows:
                    if status in ('pending_trash','pending_restore','purging','conflict'):
                        raise ValueError('Unresolved trash state blocks relocation')
                    original=Path(path)
                    if not original.is_relative_to(source):
                        if status=='trashed':
                            raise ValueError('Active trash payload is outside the source data directory')
                        continue
                    relative=original.relative_to(source)
                    if not relative.parts or relative.parts[0]!='trash':
                        raise ValueError('Trash payload has unexpected data-directory location')
                    target=_safe_path(candidate,relative.as_posix())
                    if status=='trashed':
                        entry=next((entry for entry in receipt['sourceManifest']['files']
                                    if entry['path']==relative.as_posix()),None)
                        if entry is None or relative.as_posix() not in receipt['copiedFiles']:
                            raise ValueError('Active trash payload has no verified copy evidence')
                        if not target.is_file() or target.stat().st_size!=entry['bytes']:
                            raise ValueError('Active trash payload missing or size changed in candidate')
                        with target.open('rb') as reader:
                            digest=hashlib.file_digest(reader,'sha256').hexdigest()
                        if digest!=entry['sha256']:
                            raise ValueError('Active trash payload differs from copy evidence')
                    connection.execute('UPDATE trash_item SET payload_path=? WHERE key=?',(str(target),key))
                    changes.append({'key':key,'before':path,'after':str(target)})
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('Relocation path rewrite violates foreign keys')
            result={'version':1,'phase':'paths-rewritten-without-authority','source':str(source),
                'changes':changes,'historicalJournal':'retained without path mutation',
                'authorityChanged':False}
            connection.execute('INSERT INTO data_relocation_paths VALUES (1,?,?,?)',
                               (str(source),str(candidate),json.dumps(result,ensure_ascii=False)))
            connection.commit()
        with (candidate/'relocation-paths.json').open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        return result


def relocation_preflight(source, destination):
    source=Path(source).absolute()
    destination=Path(destination).absolute()
    for target in (source,destination):
        for part in (target,*target.parents):
            if part.is_symlink() or (part.exists() and getattr(part.stat(),'st_file_attributes',0)&0x400):
                raise ValueError('Relocation paths cannot contain links')
    source=source.resolve(strict=True)
    destination=destination.resolve()
    if source==destination or source in destination.parents or destination in source.parents:
        raise ValueError('Relocation requires separate directory trees')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Relocation destination must be empty; no overwrite allowed')
    with DataWriterGuard(source) as source_guard:
        from ..storage.runtime_state import recover_catalog_journal
        recover_catalog_journal(source,source_guard)
        state=load_state(source)
        manifest=scan_manifest(source)
        files=[item for item in manifest['files'] if item['path']!='.cmv-writer.lock']
        journal=source/'operation-journal.sqlite'
        pending=OperationJournal(journal).list_unfinished() if journal.is_file() else []
        pending_trash=[]
        trash_index=source/'trash-state.sqlite'
        if trash_index.is_file():
            with closing(sqlite3.connect(trash_index.as_uri()+'?mode=ro',uri=True)) as connection:
                connection.row_factory=sqlite3.Row
                from ..operations.trash import CatalogTrash
                CatalogTrash._check(connection)
                pending_trash=[dict(row) for row in connection.execute(
                    "SELECT key,state FROM trash_item WHERE state IN ('pending_trash','pending_restore','purging','conflict')")]
        # Classification is advisory: unknown files are retained, never dropped.
        return {'version':1,'source':str(source),'destination':str(destination),'storage':state,
                'sourceManifest':manifest,'files':files,'pendingOperations':pending,
                'pendingTrash':pending_trash,
                'blockers':([] if manifest['ready'] else ['source_preflight_issues'])+
                           (['unfinished_file_operations'] if pending else [])+
                           (['unresolved_trash_items'] if pending_trash else []),
                'requiresSqliteBackup':state['mode']=='sqlite',
                'requiredRewrites':['trash_item.payload_path'],
                'historicalJournal':'retain original evidence without path mutation',
                'authorityChanged':False,'copied':False,
                'note':'Planning only; source lock is released on return. Revalidate under both guards before copying.'}
