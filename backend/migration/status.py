"""Read a persisted preparation checkpoint without advancing or activating it."""
import json
from pathlib import Path
import uuid
from .run_state import MigrationRuns, MigrationRunError


def preparation_status(destination, run_uuid):
    destination=Path(destination).absolute()
    run_uuid=str(uuid.UUID(run_uuid))
    for path in (destination,*destination.parents):
        if path.is_symlink() or (path.exists() and getattr(path.stat(),'st_file_attributes',0)&0x400):
            raise MigrationRunError('Preparation status cannot follow links')
    registry=MigrationRuns(destination/'migration-runs.sqlite')
    run=registry.read(run_uuid)
    identity=run['identity']
    if identity['snapshot']!=str(destination/'snapshot') or identity['shadowDatabase']!=str(destination/'catalog.sqlite'):
        raise MigrationRunError('Preparation identity differs from its directory')
    report_path=destination/'preparation-report.json'
    report=None
    if report_path.exists():
        if report_path.is_symlink() or getattr(report_path.stat(),'st_file_attributes',0)&0x400:
            raise MigrationRunError('Preparation report cannot follow links')
        if report_path.stat().st_size>16*1024*1024:
            raise MigrationRunError('Preparation report exceeds status budget')
        report=json.loads(report_path.read_text(encoding='utf-8'))
        if report.get('runUuid')!=run_uuid or report.get('sourceDataDir')!=identity['sourceDataDir']:
            raise MigrationRunError('Preparation report ownership differs from checkpoint')
    agrees=bool(report and report.get('state')==run['state'])
    next_action = {
        'reconciled':'review-and-final-recheck',
        'switching':'inspect-installed-bundle-and-resume-activation',
        'active':'restart-and-verify-sqlite-runtime',
        'rollback_exported':'review-export-before-legacy-recovery',
    }.get(run['state'], 'inspect-and-resume-frozen-preparation')
    return {'runUuid':run_uuid,'state':run['state'],'checkpoint':run['checkpoint'],
            'eventNo':run['event_no'],'sourceDataDir':identity['sourceDataDir'],
            'snapshot':identity['snapshot'],'report':str(report_path) if report else None,
            'reportMatchesCheckpoint':agrees,
            'preparationReady':bool(agrees and run['state']=='reconciled' and report.get('readyForActivation')),
            'requiresFinalRecheckAndWriterLock':True,
            'latestEvidence':registry.latest_evidence(run_uuid),
            'checks':{key:report.get(key) for key in ['organizationCheck','trashCheck','sourceCheck']} if report else {},
            'nextAction':next_action if agrees or run['state'] in ('switching','active','rollback_exported')
                         else 'inspect-and-resume-frozen-preparation',
            'runningProcessVerified':False}
