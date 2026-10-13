"""Offline preparation entry shared by source and packaged backend."""
import argparse
import json
import sys
from pathlib import Path

from .prepare import prepare_migration, resume_preparation
from .status import preparation_status


def main(argv=None):
    parser=argparse.ArgumentParser(description='Prepare a frozen migration; never activate live authority')
    parser.add_argument('--migration-worker',action='store_true')
    parser.add_argument('--destination',required=True)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--data-dir')
    source.add_argument('--resume-run')
    source.add_argument('--status-run')
    args=parser.parse_args(argv)
    try:
        if args.status_run:
            print(json.dumps(preparation_status(args.destination,args.status_run),ensure_ascii=False))
            return 0
        result=(resume_preparation(args.destination,args.resume_run) if args.resume_run
                else prepare_migration(args.data_dir,args.destination))
    except (OSError,ValueError,RuntimeError) as error:
        print(json.dumps({'state':'failed','authorityPublished':False,'error':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1
    print(json.dumps({'runUuid':result['runUuid'],'state':result['state'],
                      'readyForActivation':result['readyForActivation'],'authorityPublished':False,
                      'report':str(Path(args.destination).absolute()/'preparation-report.json')},ensure_ascii=False))
    return 0 if result['readyForActivation'] else 2
