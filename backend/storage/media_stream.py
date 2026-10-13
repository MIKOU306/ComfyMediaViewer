"""Freeze verified bytes before headers; path changes cannot alter the response."""
from contextlib import contextmanager
import hashlib
import os
import tempfile
from backend.storage.work_limit import WorkLimit, WorkBusy
from backend.storage.byte_budget import FROZEN_BYTES

FROZEN_WORK = WorkLimit(4)


@contextmanager
def reserve_frozen(size):
    from backend.storage.frozen_cache import VIDEO_CACHE
    from backend.storage.byte_budget import MediaTooLarge
    reservation = None
    for attempt in range(VIDEO_CACHE.maximum_entries + 1):
        try:
            pending = FROZEN_BYTES.enter(size)
            pending.__enter__()
            reservation = pending
            break
        except MediaTooLarge:
            raise
        except WorkBusy:
            if attempt == VIDEO_CACHE.maximum_entries or not VIDEO_CACHE.reclaim_idle():
                raise
    try:
        yield
    finally:
        reservation.__exit__(None, None, None)


class MediaStreamConflict(ValueError):
    pass


@contextmanager
def verified_stream(path, expected_hash, *, reuse=False, disk_backed=False):
    signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    with FROZEN_WORK.enter(), path.open('rb') as source:
        before = os.fstat(source.fileno())
        def produce(frozen):
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > before.st_size:
                    raise MediaStreamConflict('Media grew before transmission')
                frozen.write(chunk)
                digest.update(chunk)
            after = os.fstat(source.fileno())
            if signature(before) != signature(after) or digest.hexdigest() != expected_hash:
                raise MediaStreamConflict('Media changed before transmission')
            return 'sha256-' + digest.hexdigest()
        cache = None
        if reuse:
            from backend.storage.frozen_cache import VIDEO_CACHE
            if before.st_size <= VIDEO_CACHE.capacity:
                cache = VIDEO_CACHE
        if cache is not None:
            key = (os.path.abspath(path), expected_hash, signature(before))
            with cache.open(key, before.st_size, produce) as (frozen, version):
                if signature(before) != signature(os.fstat(source.fileno())):
                    raise MediaStreamConflict('Media changed before transmission')
                yield frozen, before, version
        elif disk_backed:
            from backend.storage.frozen_session import FrozenSession
            with reserve_frozen(before.st_size):
                session = FrozenSession()
                filename = None
                try:
                    with tempfile.NamedTemporaryFile(prefix='cmv-frozen-',dir=session.path,delete=False) as output:
                        filename = output.name
                        version = produce(output)
                        output.flush()
                    with open(filename,'rb') as frozen:
                        yield frozen, before, version
                finally:
                    try:
                        if filename:
                            os.unlink(filename)
                    finally:
                        session.close()
        else:
            with reserve_frozen(before.st_size), tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as frozen:
                version = produce(frozen)
                frozen.seek(0)
                yield frozen, before, version


@contextmanager
def verified_playback(path, expected_hash):
    """Pin one verified immutable disk resource for independent Range readers."""
    with verified_stream(path,expected_hash,reuse=True,disk_backed=True) as (frozen,info,version):
        filename=frozen.name
        @contextmanager
        def reader():
            with open(filename,'rb') as stream:
                yield stream,info,version
        yield reader
