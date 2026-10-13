"""Packaged and source offline recovery entry; never switches the selector."""
import argparse
import json
from pathlib import Path
import sys

from .recovery_export import export_recovery_bundle
from .recovery_restore import restore_recovery_bundle


def main(argv=None):
    parser=argparse.ArgumentParser(description='Preserve current data or restore an isolated copy')
    parser.add_argument('--recovery-worker',action='store_true')
    commands=parser.add_subparsers(dest='command',required=True)
    export=commands.add_parser('export')
    export.add_argument('--data-dir',required=True)
    export.add_argument('--destination',required=True)
    restore=commands.add_parser('restore')
    restore.add_argument('--bundle',required=True)
    restore.add_argument('--destination',required=True)
    args=parser.parse_args(argv)
    try:
        paths=[args.destination, args.data_dir if args.command=='export' else args.bundle]
        if any(not Path(value).is_absolute() or '..' in Path(value).parts for value in paths):
            raise ValueError('Recovery requires absolute paths without parent traversal')
        if args.command=='export':
            report=export_recovery_bundle(args.data_dir,args.destination)
            result={'report':str(Path(args.destination).absolute()/'recovery-export.json'),
                    'readyForLegacyStartup':False,'authorityPublished':False,
                    'organizationExportable':report['downgradeReview']['organizationExportable']}
        else:
            result=restore_recovery_bundle(args.bundle,args.destination)
    except (OSError,ValueError,RuntimeError) as error:
        print(json.dumps({'success':False,'error':str(error),'selectorChanged':False},ensure_ascii=False),file=sys.stderr)
        return 2
    print(json.dumps({**result,'selectorChanged':False},ensure_ascii=False))
    return 0
