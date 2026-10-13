"""Nonblocking byte reservations for frozen temporary media.

ByteBudget remains process-local for injected callers. SharedByteBudget uses
kernel file locks so separately launched application processes share capacity.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import uuid
from itertools import islice
from backend.storage.work_limit import WorkBusy


class MediaTooLarge(WorkBusy):
    status = 413
    code = 'media_too_large'


def _try_lock(stream, offset=0):
    stream.seek(offset)
    if os.name == 'nt':
        import msvcrt
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _plain_regular(path):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & 0x400)


class ByteBudget:
    """Injectable process-local budget, retained for tests and isolated users."""
    def __init__(self, capacity, *, headroom=64 * 1024 * 1024, free_bytes=None):
        if type(capacity) is not int or capacity < 1 or type(headroom) is not int or headroom < 0:
            raise ValueError('Invalid frozen byte budget')
        self.capacity, self.headroom = capacity, headroom
        self.free_bytes = free_bytes or (lambda: shutil.disk_usage(tempfile.gettempdir()).free)
        self.used = 0
        self.lock = threading.Lock()

    @contextmanager
    def enter(self, size):
        if type(size) is not int or size < 0:
            raise ValueError('Invalid reservation size')
        if size > self.capacity:
            raise MediaTooLarge('Media exceeds the supported frozen-copy size limit')
        with self.lock:
            if self.used + size > self.capacity:
                raise WorkBusy('Frozen media byte capacity is in use; retry after other reads finish')
            if self.free_bytes() - self.used - size < self.headroom:
                raise WorkBusy('Temporary disk space is insufficient for a frozen media copy')
            self.used += size
        try:
            yield
        finally:
            with self.lock:
                self.used -= size


class SharedByteBudget(ByteBudget):
    """Cross-process budget backed by locked UUID lease files on temp volume."""
    marker = b'CMV-BYTE-BUDGET-1\n'
    max_entries = 2048
    lock_offset = 4096

    def __init__(self, capacity, *, headroom=64 * 1024 * 1024, free_bytes=None, base=None):
        super().__init__(capacity, headroom=headroom, free_bytes=free_bytes)
        parent = Path(base or tempfile.gettempdir()).absolute()
        current = Path(parent.anchor)
        missing = False
        for part in parent.parts[1:]:
            current = current / part
            if not current.exists():
                missing = True
                continue
            if missing:
                raise OSError('Frozen byte budget parent path has an unexpected gap')
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise OSError('Frozen byte budget parent path must not contain links')
        parent.mkdir(parents=True, exist_ok=True)
        self.base = parent / 'cmv-byte-budget-v1'
        self.base.mkdir(exist_ok=True)
        info = self.base.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise OSError('Frozen byte budget directory must not be a link')
        self.guard_path = self.base / 'global.lock'
        try:
            self.guard_path.lstat()
            if not _plain_regular(self.guard_path):
                raise OSError('Frozen byte budget lock must be a plain file')
        except FileNotFoundError:
            pass
        if free_bytes is None:
            self.free_bytes = lambda: shutil.disk_usage(self.base).free
        self.guard = self.guard_path.open('a+b')
        # Permanent guard: never unlink this inode, avoiding lock split-brain.

    def close(self):
        if not self.guard.closed:
            self.guard.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def enter(self, size):
        if type(size) is not int or size < 0:
            raise ValueError('Invalid reservation size')
        if size > self.capacity:
            raise MediaTooLarge('Media exceeds the supported frozen-copy size limit')
        lease = None
        with self.lock:
            if not _try_lock(self.guard, self.lock_offset):
                raise WorkBusy('Frozen media budget is busy; retry shortly')
            try:
                used = 0
                with os.scandir(self.base) as listing:
                    entries = [Path(entry.path) for entry in islice(listing, self.max_entries + 1)]
                if len(entries) > self.max_entries:
                    raise WorkBusy('Frozen media budget state exceeds its safe scan limit')
                for path in entries:
                    if path.name == 'global.lock':
                        continue
                    # Unknown names and malformed records are preserved and fail closed.
                    try:
                        amount = self._compatible_lease_size(path.name)
                        if not _plain_regular(path):
                            raise WorkBusy('Frozen media budget contains unknown state')
                        stream = path.open('r+b')
                    except (ValueError, OSError) as exc:
                        raise WorkBusy('Frozen media budget contains unknown state') from exc
                    if _try_lock(stream):
                        try:
                            if amount is None and not _try_lock(stream, self.lock_offset):
                                raise WorkBusy('Legacy frozen media reservation is still active')
                            stream.seek(0)
                            raw = stream.read(129)
                            parsed = self._parse(raw)
                            if amount is not None and parsed != amount:
                                raise ValueError('lease name and record disagree')
                            stream.close()
                            path.unlink()
                        except ValueError as exc:
                            stream.close()
                            raise WorkBusy('Frozen media budget contains malformed state') from exc
                        except Exception:
                            stream.close()
                            raise
                    else:
                        # Windows may deny reads of a locked file. The strict
                        # canonical filename carries the live reservation size.
                        stream.close()
                        if amount is None:
                            raise WorkBusy('Legacy frozen media reservation is still active')
                        used += amount
                if used + size > self.capacity:
                    raise WorkBusy('Frozen media byte capacity is in use; retry after other reads finish')
                if self.free_bytes() - used - size < self.headroom:
                    raise WorkBusy('Temporary disk space is insufficient for a frozen media copy')
                lease = self.base / (str(uuid.uuid4()) + '-' + str(size) + '.lease')
                stream = lease.open('x+b')
                if not _try_lock(stream):
                    stream.close()
                    lease.unlink(missing_ok=True)
                    raise WorkBusy('Could not claim frozen media budget lease')
                try:
                    stream.seek(0)
                    stream.write(self.marker + str(size).encode('ascii') + b'\n')
                    stream.flush()
                except Exception:
                    stream.close()
                    lease.unlink(missing_ok=True)
                    raise
            finally:
                _unlock(self.guard)
        try:
            yield
        finally:
            stream.close()
            self._reclaim(lease)

    def _reclaim(self, lease):
        # Normal release removes its own recognized lease under the global lock.
        with self.lock:
            if not _try_lock(self.guard, self.lock_offset):
                return
            try:
                if _plain_regular(lease):
                    with lease.open('r+b') as check:
                        if _try_lock(check):
                            check.seek(0)
                            if self._parse(check.read(129)) != self._lease_size(lease.name):
                                raise ValueError('lease name and record disagree')
                            check.close()
                            lease.unlink()
            except (OSError, ValueError):
                pass
            finally:
                _unlock(self.guard)

    @classmethod
    def _parse(cls, raw):
        if len(raw) > 128 or not raw.startswith(cls.marker) or not raw.endswith(b'\n'):
            raise ValueError('bad lease marker')
        value = raw[len(cls.marker):-1]
        if not value or not value.isdigit():
            raise ValueError('bad lease size')
        amount = int(value)
        if amount < 0:
            raise ValueError('bad lease size')
        return amount

    @staticmethod
    def _compatible_lease_size(name):
        # Earlier development builds stored the amount only in the record.
        # Recognize canonical UUID names, but reclaim only unlocked valid records;
        # an active legacy owner cannot be safely counted on Windows.
        if name.endswith('.lease'):
            identity = name[:-6]
            try:
                if str(uuid.UUID(identity)) == identity:
                    return None
            except ValueError:
                pass
        return SharedByteBudget._lease_size(name)

    @staticmethod
    def _lease_size(name):
        if not name.endswith('.lease'):
            raise ValueError('unknown lease name')
        identity, separator, amount_text = name[:-6].rpartition('-')
        if not separator or str(uuid.UUID(identity)) != identity or not amount_text.isdigit():
            raise ValueError('unknown lease name')
        amount = int(amount_text)
        if str(amount) != amount_text:
            raise ValueError('unknown lease size')
        return amount


def _unlock(stream):
    stream.seek(SharedByteBudget.lock_offset if Path(stream.name).name == 'global.lock' else 0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class _LazySharedByteBudget:
    """Avoid touching the temp volume during module import."""
    def __init__(self, capacity):
        self.capacity = capacity
        self._budget = None
        self._lock = threading.Lock()

    @contextmanager
    def enter(self, size):
        with self._lock:
            if self._budget is None:
                self._budget = SharedByteBudget(self.capacity)
            budget = self._budget
        with budget.enter(size):
            yield


FROZEN_BYTES = _LazySharedByteBudget(8 * 1024 * 1024 * 1024)
