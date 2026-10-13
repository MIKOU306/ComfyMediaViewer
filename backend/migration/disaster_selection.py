"""Explicit offline selection when the original directory is genuinely missing."""
from pathlib import Path

from .guard import DataWriterGuard
from .recovery_job import _verify_completed
from .recovery_restore import verify_recovery_bundle
from .recovery_selection import (_safe, _separate, _read_json, _selected_source,
                                 _selector_evidence)
from .relocation_cutover import _replace_json, _write_new_json


def _require_missing(source):
    # Only an actual missing-path error proves absence. Permission and I/O errors
    # must never silently become permission to skip backing up existing data.
    try:
        source.stat()
    except FileNotFoundError:
        return
    raise ValueError('Original source exists; use backup-first selection')


def select_missing_source_restore(selector, bundle, target, evidence_directory):
    selector,bundle,target,evidence=map(lambda p:_safe(Path(p)),
                                      (selector,bundle,target,evidence_directory))
    if selector.name!='portable.flag': raise ValueError('Only portable.flag is supported')
    report,_,_=verify_recovery_bundle(bundle)
    source=_safe(Path(report['sourceDataDir']))
    _separate((selector,bundle,target,evidence,source))
    binding={'action':'restore','bundle':str(bundle),'destination':str(target)}
    if not evidence.parent.is_dir(): raise ValueError('Evidence parent must exist')
    if not selector.parent.is_dir() or not target.is_dir():
        raise ValueError('Program directory and restored target must already exist')
    # Never acquire a guard inside the missing source: doing so would recreate it.
    roots=sorted((selector.parent,target),key=lambda p:str(p).casefold())
    with DataWriterGuard(roots[0]),DataWriterGuard(roots[1]):
        _verify_completed(binding)
        _require_missing(source)
        if evidence.exists():
            intent=_read_json(evidence/'disaster-selection-intent.json')
            receipt_path=evidence/'disaster-selection-receipt.json'
            if not receipt_path.exists():
                if (intent.get('version')!=1 or intent.get('state')!='prepared'
                        or intent.get('selector')!=str(selector) or intent.get('source')!=str(source)
                        or intent.get('bundle')!=str(bundle) or intent.get('target')!=str(target)
                        or intent.get('storageIdentity')!=report['storageIdentity']
                        or intent.get('sourceMissing') is not True
                        or intent.get('currentDataBackedUp') is not False):
                    raise ValueError('Incomplete disaster selection intent does not verify')
                selected=_selected_source(selector)
                if selected==source and _selector_evidence(selector)==intent.get('selectorBefore'):
                    _replace_json(selector,{'dir':str(target)})
                elif selected!=target:
                    raise ValueError('Incomplete disaster selection has an unknown selector outcome')
                _write_new_json(receipt_path,dict(intent,state='completed',selectorChanged=True))
            receipt=_read_json(evidence/'disaster-selection-receipt.json')
            if (receipt!=dict(intent,state='completed',selectorChanged=True)
                    or intent.get('selector')!=str(selector) or intent.get('source')!=str(source)
                    or intent.get('bundle')!=str(bundle) or intent.get('target')!=str(target)
                    or intent.get('storageIdentity')!=report['storageIdentity']
                    or _selected_source(selector)!=target):
                raise ValueError('Existing disaster selection evidence does not verify')
            return receipt
        if _selected_source(selector)!=source:
            raise ValueError('Selector does not point to the missing original source')
        before=_selector_evidence(selector)
        intent={'version':1,'state':'prepared','selector':str(selector),'source':str(source),
                'bundle':str(bundle),'target':str(target),'storageIdentity':report['storageIdentity'],
                'selectorBefore':before,'sourceMissing':True,'currentDataBackedUp':False,
                'warning':'Changes after this recovery bundle may be unavailable',
                'selectorChanged':False}
        evidence.mkdir()
        _write_new_json(evidence/'disaster-selection-intent.json',intent)
        _verify_completed(binding)
        _require_missing(source)
        if _selector_evidence(selector)!=before:
            raise ValueError('Source or selector changed before publication')
        _replace_json(selector,{'dir':str(target)})
        receipt=dict(intent,state='completed',selectorChanged=True)
        _write_new_json(evidence/'disaster-selection-receipt.json',receipt)
        return receipt
