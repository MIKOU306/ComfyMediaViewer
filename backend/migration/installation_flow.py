"""Offline installation continuation over the existing durable checkpoints."""
import json
import argparse
import sys
from pathlib import Path

from .install import _safe, install_migration
from .install_bundle import build_install_bundle
from .activate import activate_migration
from .status import preparation_status


def complete_installation(prepared, bundle_directory):
    prepared = Path(prepared).absolute()
    bundle_directory = Path(bundle_directory).absolute()
    report_path = _safe(prepared, 'preparation-report.json')
    if report_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Preparation report exceeds supported size')
    report = json.loads(report_path.read_text('utf-8'))
    status = preparation_status(prepared, report['runUuid'])
    if status['state'] not in ('reconciled', 'switching', 'active'):
        raise ValueError('Preparation is not ready for installation continuation')
    manifest = _safe(bundle_directory, 'bundle.json')
    if not manifest.exists():
        if status['state'] != 'reconciled':
            raise ValueError('Persisted installation bundle is missing; do not rebuild it')
        if bundle_directory.exists():
            raise ValueError('Incomplete bundle retained; use a new bundle directory before installation')
        build_install_bundle(prepared, bundle_directory)
    if manifest.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Installation manifest exceeds supported size')
    bundle = json.loads(manifest.read_text('utf-8'))
    if bundle.get('runUuid') != status['runUuid'] or bundle.get('sourceDataDir') != status['sourceDataDir']:
        raise ValueError('Installation bundle belongs to another preparation')
    if status['state'] != 'active':
        install_migration(prepared, bundle_directory)
    activated = activate_migration(prepared, bundle_directory)
    return {**activated, 'runUuid': status['runUuid'], 'authorityPublished': True,
            'requiresRestartAndRuntimeVerification': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline migration installation continuation')
    parser.add_argument('--installation-worker', action='store_true')
    parser.add_argument('--prepared', required=True)
    parser.add_argument('--bundle', required=True)
    args = parser.parse_args(argv)
    try:
        result = complete_installation(args.prepared, args.bundle)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'success':False, 'error':str(error),
                          'requiresStateInspection':True}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0
