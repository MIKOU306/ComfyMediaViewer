"""Immediate filesystem folders plus retained indexed paths; no media reads."""
import os
import hashlib
import json
from pathlib import Path


def list_directories(root, directory, indexed_paths, *, budget=2000, after=None):
    base = Path(root['path'])
    target = base.joinpath(*(directory.split('/') if directory else []))
    sensitive = root['caseSensitive'] != 0
    fold = (lambda value: value) if sensitive else str.casefold
    indexed, live = {}, {}
    totals = {'indexed': [0, 0], 'live': [0, 0]}
    overflow = {'indexed': False, 'live': False}
    def retain(bucket, item, source):
        name = item['name']
        key = fold(name)
        encoded = json.dumps([name, item['availability']], ensure_ascii=False).encode('utf-8')
        totals[source][0] += 1
        totals[source][1] = (totals[source][1] + int.from_bytes(hashlib.sha256(encoded).digest(), 'big')) % (1 << 256)
        if after is not None and key <= after:
            return
        if key not in bucket and len(bucket) >= budget:
            overflow[source] = True
            largest = max(bucket)
            if key >= largest:
                return
            del bucket[largest]
        bucket[key] = item
    source = indexed_paths() if callable(indexed_paths) else indexed_paths
    for name in source:
        retain(indexed, {'name':name,'path':str(target / name),'availability':'unknown','indexed':True}, 'indexed')
    status = 'online'
    identity = None
    try:
        parts = directory.split('/') if directory else []
        ancestors = [base, *base.parents, *[base.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)]]
        for candidate in ancestors:
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise PermissionError('Linked directory browsing is not supported')
        before = target.stat()
        with os.scandir(target) as iterator:
            for entry in iterator:
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                retain(live, {'name':entry.name,'path':entry.path,'availability':'online','indexed':False}, 'live')
        after_info = target.stat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after_info.st_dev, after_info.st_ino, after_info.st_mtime_ns):
            raise OSError('Directory changed during listing; refresh required')
        identity = [after_info.st_dev, after_info.st_ino, after_info.st_mtime_ns]
    except FileNotFoundError:
        status = 'missing' if base.exists() else 'offline'
    except OSError:
        status = 'unreadable'
    entries = dict(indexed)
    for item in entries.values():
        item['availability'] = 'missing' if status == 'online' else status
    if status == 'online':
        for key, item in live.items():
            item['indexed'] = key in indexed
            entries[key] = item
    generation = hashlib.sha256(json.dumps([root['path'], root['caseSensitive'], directory,
                                           totals['indexed'], totals['live'] if status == 'online' else None,
                                           status, identity], sort_keys=True).encode()).hexdigest()
    keys = sorted(entries)
    truncated = len(keys) > budget or overflow['indexed'] or (status == 'online' and overflow['live'])
    return {'path': str(target), 'dirs': [entries[key] for key in keys[:budget]],
            'directoryAvailability': status, 'directoriesTruncated': truncated,
            'directoryGeneration':generation,
            'directoryCoverage': 'indexed_and_live' if status == 'online' else 'indexed_only'}
