"""Durable offline cutover preparation; publication is a separate operation."""
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .data_relocation import with_verified_relocation
from .guard import DataWriterGuard
from ..storage.runtime_state import _safe_path,publish_state,load_state


def _selector_snapshot(selector):
    selector=_safe_path(selector.parent,selector.name)
    if not selector.exists():
        return {'exists':False,'base64':None,'sha256':None}
    with selector.open('rb') as stream:
        raw=stream.read(65537)
    if len(raw)>65536:
        raise ValueError('Directory selector exceeds supported size')
    return {'exists':True,'base64':base64.b64encode(raw).decode('ascii'),
            'sha256':hashlib.sha256(raw).hexdigest()}


def _write_new_json(path,value):
    descriptor,name=tempfile.mkstemp(prefix='.relocation-intent-',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_bytes(path,raw):
    path=_safe_path(path.parent,path.name)
    descriptor,name=tempfile.mkstemp(prefix='.relocation-publish-',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        if os.name!='nt':
            descriptor=os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_json(path,value):
    _replace_bytes(path,(json.dumps(value,ensure_ascii=False,sort_keys=True)+'\n').encode('utf-8'))


def execute_prepared_relocation(candidate,*,_checkpoint=lambda phase:None,_recovering=False):
    """Offline publication. Interrupted publication stays fail-closed for recovery.

    Not wired to the running server: all cooperating writers must be stopped.
    Windows filesystem power-loss durability is not claimed by this API.
    """
    candidate=Path(candidate).absolute()
    def execute(report,receipt):
        path=_safe_path(candidate,'relocation-cutover.json')
        intent=json.loads(path.read_text('utf-8'))
        expected_hash=hashlib.sha256(json.dumps(receipt['sourceManifest'],sort_keys=True,
                                               ensure_ascii=False).encode('utf-8')).hexdigest()
        allowed=('prepared','source-retired','authority-published','selector-published') if _recovering else ('prepared',)
        if intent.get('version')!=1 or intent.get('phase') not in allowed or \
                intent.get('source')!=report['source'] or intent.get('candidate')!=str(candidate) or \
                intent.get('storage')!=receipt['storage'] or intent.get('sourceManifestSha256')!=expected_hash:
            raise ValueError('Cutover intent does not match reconciled candidate')
        selector=Path(intent['selector'])
        if not selector.is_absolute() or selector.name!='portable.flag' or any(
                selector.is_relative_to(Path(report[key])) for key in ('source','candidate')):
            raise ValueError('Cutover selector is invalid')
        raw=base64.b64decode(intent['selectorAfter']['base64'],validate=True)
        if hashlib.sha256(raw).hexdigest()!=intent['selectorAfter']['sha256'] or \
                json.loads(raw)!= {'dir':str(candidate)}:
            raise ValueError('Cutover target selector differs from candidate')
        with DataWriterGuard(selector.parent):
            selected=_selector_snapshot(selector)
            permitted=(intent['selectorBefore'],intent['selectorAfter']) if _recovering else (intent['selectorBefore'],)
            if selected not in permitted or (intent['phase']=='selector-published' and selected!=intent['selectorAfter']):
                raise ValueError('Selector changed since cutover preparation')
            retirement={'version':1,'source':report['source'],'candidate':str(candidate),
                        'selector':str(selector),'sourceManifestSha256':expected_hash}
            retired=_safe_path(Path(report['source']),'relocation-retired.json')
            if not retired.exists():
                _write_new_json(retired,retirement)
            elif not _recovering or json.loads(retired.read_text('utf-8'))!=retirement:
                raise ValueError('Source retirement evidence differs')
            _checkpoint('retirement-written')
            def phase(name):
                intent['phase']=name
                _replace_json(path,intent)
                _checkpoint(name)
            phase('source-retired')
            publish_state(candidate,receipt['storage'])
            _checkpoint('authority-written')
            phase('authority-published')
            _replace_bytes(selector,raw)
            _checkpoint('selector-written')
            phase('selector-published')
            intent['authorityChanged']=True
            phase('completed')
            return intent
    return with_verified_relocation(candidate,execute,_recovering=_recovering)


def recover_relocation_cutover(candidate,*,_checkpoint=lambda phase:None):
    """Reconcile an interrupted switch again and finish forward, never roll back."""
    candidate=Path(candidate).absolute()
    with DataWriterGuard(candidate):
        intent=json.loads(_safe_path(candidate,'relocation-cutover.json').read_text('utf-8'))
        if intent.get('phase')=='completed':
            selector=Path(intent['selector'])
            if not selector.is_absolute() or selector.name!='portable.flag' or selector.is_relative_to(candidate):
                raise ValueError('Completed cutover selector is invalid')
            with DataWriterGuard(selector.parent):
                load_state(candidate)
                return intent
    return execute_prepared_relocation(candidate,_checkpoint=_checkpoint,_recovering=True)


def prepare_relocation_cutover(candidate,selector):
    """Persist old/new portable selector bytes under all participating locks.

    Does not publish an authority pointer, retire the source or change selector.
    A prepared intent is evidence for recovery, never permission to skip a new
    locked reconciliation when executing the switch.
    """
    selector=Path(selector).absolute()
    if selector.name!='portable.flag':
        raise ValueError('Only the application portable.flag selector is supported')
    _safe_path(selector.parent,selector.name)
    if not selector.parent.is_dir():
        raise ValueError('Selector parent directory must exist')
    def prepare(report,receipt):
        source=Path(report['source']);target=Path(report['candidate'])
        if any(selector.is_relative_to(directory) or selector.parent==directory
               for directory in (source,target)):
            raise ValueError('Selector must be outside both data directories')
        with DataWriterGuard(selector.parent):
            before=_selector_snapshot(selector)
            new_bytes=(json.dumps({'dir':str(target)},ensure_ascii=False)+'\n').encode('utf-8')
            intent={'version':1,'phase':'prepared','source':str(source),'candidate':str(target),
                    'selector':str(selector),'selectorBefore':before,
                    'selectorAfter':{'exists':True,'base64':base64.b64encode(new_bytes).decode('ascii'),
                                     'sha256':hashlib.sha256(new_bytes).hexdigest()},
                    'storage':receipt['storage'],
                    'sourceManifestSha256':hashlib.sha256(json.dumps(receipt['sourceManifest'],
                        sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest(),
                    'authorityChanged':False}
            path=_safe_path(target,'relocation-cutover.json')
            if path.exists():
                if json.loads(path.read_text('utf-8'))!=intent:
                    raise ValueError('Prepared cutover intent differs from current directories or selector')
            else:
                _write_new_json(path,intent)
            return intent
    return with_verified_relocation(candidate,prepare)


def cancel_prepared_relocation(candidate):
    """Cancel only an untouched prepared switch, keeping all copied materials.

    Published pointers or a changed selector require the forward recovery path;
    this function must never erase their evidence or silently roll them back.
    """
    candidate=Path(candidate).absolute()
    def cancel(report,receipt):
        path=_safe_path(candidate,'relocation-cutover.json')
        archive=_safe_path(candidate,'relocation-cutover-cancelled.json')
        if not path.exists():
            if not archive.exists():
                raise ValueError('No prepared relocation to cancel')
            intent=json.loads(archive.read_text('utf-8'))
        else:
            intent=json.loads(path.read_text('utf-8'))
        if intent.get('version')!=1 or intent.get('phase')!='prepared' or \
                intent.get('source')!=report['source'] or intent.get('candidate')!=report['candidate']:
            raise ValueError('Only the matching prepared cutover can be cancelled')
        selector=Path(intent['selector'])
        if not selector.is_absolute() or selector.name!='portable.flag' or any(
                selector.is_relative_to(Path(report[key])) for key in ('source','candidate')):
            raise ValueError('Prepared cutover selector is invalid')
        with DataWriterGuard(selector.parent):
            if _selector_snapshot(selector)!=intent['selectorBefore']:
                raise ValueError('Selector changed; prepared cancellation cannot recover a published switch')
            if archive.exists():
                if json.loads(archive.read_text('utf-8'))!=intent:
                    raise ValueError('Cancelled cutover evidence differs')
            else:
                _write_new_json(archive,intent)
            path.unlink(missing_ok=True)
            return {'version':1,'phase':'cancelled','source':report['source'],
                    'candidate':report['candidate'],'authorityChanged':False,'materialsPreserved':True}
    return with_verified_relocation(candidate,cancel)
