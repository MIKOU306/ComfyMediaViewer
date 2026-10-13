"""Expose observed scan checkpoints without implying metadata index completeness."""
import hashlib
import json


def scan_coverage(db, library_id, root_uuids=()):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_scan'").fetchone()
    roots = db.execute('SELECT id,uuid,availability FROM library_root WHERE library_id=? ORDER BY uuid',
                       (library_id,)).fetchall()
    selected = set(root_uuids)
    records = []
    from backend.storage.scan_fingerprint import initialize
    has_fingerprints = initialize(db,create=False)
    for root in roots:
        if selected and root['uuid'] not in selected:
            continue
        scan = db.execute('SELECT epoch,state,error FROM catalog_scan WHERE root_id=?',
                          (root['id'],)).fetchone() if exists else None
        fingerprint = db.execute('SELECT signature FROM catalog_scan_fingerprint WHERE root_id=? AND epoch=?',
                                (root['id'],scan['epoch'])).fetchone() if has_fingerprints and scan and scan['state']=='complete' else None
        records.append({'rootId': root['uuid'], 'availability': root['availability'],
                        'epoch': scan['epoch'] if scan else None,
                        'state': scan['state'] if scan else 'not_scanned',
                        'failure': scan['error'] if scan else None,
                        'fingerprint':fingerprint[0] if fingerprint else None})
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'scanCheckpointHash': digest, 'scanCoverage': {'roots': records,
            'completedRoots': sum(row['state'] == 'complete' for row in records),
            'totalRoots': len(records), 'scope': 'registered_roots',
            'semantics': 'last_observed_scan', 'metadataCoverage': 'partial'}}
