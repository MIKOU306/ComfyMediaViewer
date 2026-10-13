"""Bounded-memory recursive stat scan; never reads media content or follows links."""
import os
import time
from pathlib import Path

from backend.storage.catalog import IdentityConflictError
from backend.storage.scans import CatalogScans

from backend.storage.directory_fingerprint import MEDIA_EXTENSIONS


def _registered_roots(db, library_id):
    return [tuple(row) for row in db.execute('SELECT id,path,case_sensitive FROM library_root '
                                            'WHERE library_id=? ORDER BY id', (library_id,))]


def scan_directory(database, library_uuid, root_uuid, *, cancelled=lambda: False, progress=None, fingerprint=None):
    scans = CatalogScans(database)
    with scans.catalog._transaction(write=False) as db:
        library, root = scans.catalog._scope(db, library_uuid, root_uuid)
        directory = Path(root['path'])
        registered = _registered_roots(db, library['id'])
    epoch = scans.begin(library_uuid, root_uuid)
    result = {'epoch': epoch, 'rootId': root_uuid, 'observed': 0, 'directories': 0, 'ownedElsewhere': 0}
    last_progress = 0.0
    pending = []
    batch_size = 64

    def flush_batch(*, respect_cancel=True):
        if not pending:
            return
        outcome = scans.observe_batch(library_uuid, root_uuid, epoch, tuple(pending),
                                      expected_roots=registered,
                                      cancelled=cancelled if respect_cancel else lambda: False)
        result['observed'] += outcome['observed']
        result['ownedElsewhere'] += outcome['ownedElsewhere']
        pending.clear()
        notify(force=True)

    def notify(force=False):
        nonlocal last_progress
        now = time.monotonic()
        if progress is not None and (force or now - last_progress >= .25):
            progress(dict(result))
            last_progress = now

    def fail(reason, error=None, *, flush_pending=True):
        if flush_pending and pending:
            flush_batch(respect_cancel=False)
        return {**result, **scans.finish(library_uuid, root_uuid, epoch, failure=reason),
                **({'error': str(error)} if error else {})}

    try:
        notify(force=True)
        for candidate in (directory, *directory.parents):
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                return fail('unreadable', 'Linked roots are not scanned')
        initial = directory.stat()
        if not directory.is_dir():
            return fail('unreadable', 'Registered root is not a directory')
        before_signature = fingerprint(directory,cancelled=cancelled) if fingerprint else None
        # Iterators form a depth-first stack; memory scales with depth, not file count.
        stack = [os.scandir(directory)]
        result['directories'] = 1
        try:
            while stack:
                if cancelled():
                    return fail('cancelled')
                notify()
                try:
                    entry = next(stack[-1])
                except StopIteration:
                    stack.pop().close()
                    continue
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                    # A skipped subtree cannot prove complete root coverage.
                    return fail('unreadable', 'Linked entries require explicit ownership')
                if entry.is_dir(follow_symlinks=False):
                    stack.append(os.scandir(entry.path))
                    scans.observe_directory(library_uuid, root_uuid, epoch, entry.path,
                                            expected_roots=registered)
                    result['directories'] += 1
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MEDIA_EXTENSIONS:
                    pending.append((entry.path, info))
                    if len(pending) >= batch_size:
                        flush_batch()
        finally:
            for iterator in stack:
                iterator.close()
        if cancelled():
            return fail('cancelled')
        flush_batch()
        if cancelled():
            return fail('cancelled')
        final = directory.stat()
        if (initial.st_dev, initial.st_ino) != (final.st_dev, final.st_ino):
            return fail('unreadable', 'Root identity changed during scan')
        with scans.catalog._transaction(write=False) as db:
            scope_changed = _registered_roots(db, library['id']) != registered
        if scope_changed:
            return fail('cancelled', 'Registered root scope changed during scan')
        after_signature = fingerprint(directory,cancelled=cancelled) if fingerprint else None
        if before_signature != after_signature:
            return fail('changed','Directory changed during scan; absence was not published')
        return {**result, **scans.finish(library_uuid, root_uuid, epoch, expected_roots=registered,
                                       fingerprint=after_signature)}
    except FileNotFoundError as error:
        # A vanished child does not prove root offline; neither case publishes absence.
        return fail('offline' if not directory.exists() else 'unreadable', error)
    except PermissionError as error:
        return fail('unreadable', error)
    except OSError as error:
        return fail('unreadable', error)
    except IdentityConflictError as error:
        # A superseded worker must not finish or mutate the new epoch.
        with scans.catalog._transaction(write=False) as db:
            _, scoped_root = scans.catalog._scope(db, library_uuid, root_uuid)
            current = db.execute('SELECT epoch,state FROM catalog_scan WHERE root_id=?',
                                 (scoped_root['id'],)).fetchone()
        if current is None or current['epoch'] != epoch or current['state'] != 'running':
            return {**result, 'state': 'superseded', 'coverage': 'partial', 'error': str(error)}
        return fail('unreadable', error, flush_pending=False)
    except Exception as error:
        from backend.storage.scans import ScanCancelled
        if isinstance(error, ScanCancelled):
            return fail('cancelled')
        # A worker exception must not leave a writable running checkpoint.
        # Failure to persist this result still propagates to the job service;
        # startup recovery will invalidate the remaining running epoch.
        return fail('unreadable', error, flush_pending=False)
