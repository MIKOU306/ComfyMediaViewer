"""Verify and restore current-data material into a new isolated SQLite directory."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid
from contextlib import closing

from .backup import _safe_relative
from .guard import DataWriterGuard
from .recovery_export import _digest
from .relocation_cutover import _write_new_json
from .sqlite_backup import backup_database
from ..storage.runtime_state import _safe_path, publish_state


def verify_recovery_bundle(bundle):
    bundle=Path(bundle).absolute()
    report_path=_safe_path(bundle,'recovery-export.json')
    with report_path.open('rb') as stream:
        raw=stream.read(32*1024*1024+1)
    if len(raw)>32*1024*1024: raise ValueError('Recovery report exceeds supported size')
    report=json.loads(raw)
    if not isinstance(report,dict) or report.get('version')!=1 or report.get('authorityPublished') is not False:
        raise ValueError('Invalid recovery report')
    preserved=_safe_path(bundle,'preserved-current-data')
    for path in preserved.rglob('*'):
        _safe_path(preserved,path.relative_to(preserved).as_posix())
    expected={}
    for entry in report.get('preservedFiles',[]):
        relative=_safe_relative(entry['path']).as_posix()
        if relative in expected or relative=='storage-state.json': raise ValueError('Duplicate or activated recovery material')
        path=_safe_path(preserved,relative)
        if not path.is_file() or path.stat().st_size!=entry['bytes'] or _digest(path)!=entry['sha256']:
            raise ValueError('Recovery material mismatch: '+relative)
        expected[relative]=entry
    actual={p.relative_to(preserved).as_posix() for p in preserved.rglob('*') if p.is_file() and p.name!='.cmv-writer.lock'}
    if not expected or actual!=set(expected): raise ValueError('Recovery file set differs from manifest')
    return report, preserved, expected


def restore_recovery_bundle(bundle,destination):
    report,preserved,entries=verify_recovery_bundle(bundle)
    destination=Path(destination).absolute()
    bundle=Path(bundle).absolute()
    _safe_path(destination.parent,destination.name)
    source=Path(report['sourceDataDir'])
    if not source.is_absolute() or destination==source or source in destination.parents or destination in source.parents:
        raise ValueError('Restore must remain separate from the original data directory')
    if destination.exists() or destination==bundle or bundle in destination.parents or destination in bundle.parents:
        raise ValueError('Restore requires a new isolated directory')
    destination.mkdir()
    with DataWriterGuard(destination):
        for relative in entries:
            target=_safe_path(destination,relative)
            target.parent.mkdir(parents=True,exist_ok=True)
            origin=_safe_path(preserved,relative)
            if origin.suffix.lower() in ('.sqlite','.db'):
                backup_database(origin,target)
            else:
                with origin.open('rb') as reader,target.open('xb') as writer:
                    shutil.copyfileobj(reader,writer);writer.flush();os.fsync(writer.fileno())
                if _digest(target)!=entries[relative]['sha256']: raise ValueError('Restored file differs: '+relative)
        changes=[]
        trash=_safe_path(destination,'trash-state.sqlite')
        if trash.exists():
            from ..operations.trash import CatalogTrash
            with closing(sqlite3.connect(trash)) as db:
                db.row_factory=sqlite3.Row
                CatalogTrash._check(db)
                source=Path(report['sourceDataDir'])
                for key,path,state in db.execute('SELECT key,payload_path,state FROM trash_item').fetchall():
                    if state in ('pending_trash','pending_restore','purging','conflict'):
                        raise ValueError('Unresolved trash blocks recovery')
                    old=Path(path)
                    if not old.is_relative_to(source):
                        if state=='trashed': raise ValueError('Active trash payload has no source ownership')
                        continue
                    relative=old.relative_to(source).as_posix()
                    if not relative.startswith('trash/') or (state=='trashed' and relative not in entries):
                        raise ValueError('Trash payload lacks preservation evidence')
                    target=_safe_path(destination,relative)
                    db.execute('UPDATE trash_item SET payload_path=? WHERE key=?',(str(target),key))
                    changes.append({'key':key,'before':path,'after':str(target)})
                db.commit()
        # Recheck immutable bundle after copying; authority publication is the last step.
        verify_recovery_bundle(bundle)
        previous_receipt=_safe_path(destination,'recovery-restore.json')
        previous_history=None
        if previous_receipt.exists():
            history=_safe_path(destination,'.cmv-recovery-history')
            history.mkdir(exist_ok=True)
            archived=_safe_path(history,'restore-'+str(uuid.uuid4())+'.json')
            previous_receipt.rename(archived)
            previous_history=archived.relative_to(destination).as_posix()
        result={'version':1,'bundle':str(bundle),'destination':str(destination),'trashPathChanges':changes,
                'historicalJournal':'preserved without mutation','selectorChanged':False,'isolatedRestore':True,
                'previousRestoreReceipt':previous_history}
        _write_new_json(destination/'recovery-restore.json',result)
        publish_state(destination,report['storageIdentity'])
        return result
