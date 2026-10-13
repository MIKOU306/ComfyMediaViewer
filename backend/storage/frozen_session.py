"""Owned frozen-cache directories; process locks distinguish live sessions."""
import os
from pathlib import Path
import stat
import tempfile
import uuid
from itertools import islice


def _plain(path, directory=False):
    info = path.lstat()
    return not (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400) and (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def _lock(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class FrozenSession:
    def __init__(self, base=None):
        self.base = Path(base or tempfile.gettempdir()) / 'cmv-frozen-sessions-v1'
        self.base.mkdir(exist_ok=True)
        if not _plain(self.base, True):
            raise OSError('Frozen session directory must not be a link')
        self.recover()
        self.path = self.base / str(uuid.uuid4())
        self.path.mkdir()
        self.owner = (self.path / 'owner.lock').open('x+b')
        # Claim the session before publishing a recognizable marker.
        _lock(self.owner)
        self.owner.write(b'CMV-FROZEN-1\n')
        self.owner.flush()

    def recover(self):
        # Only recognized flat session directories are eligible; never recurse.
        for index, candidate in enumerate(self.base.iterdir()):
            if index >= 1000:
                break
            try:
                if str(uuid.UUID(candidate.name)) != candidate.name or not _plain(candidate, True):
                    continue
                # A cache session has at most four retained copies, plus
                # in-flight copies. Unexpected large directories stay untouched.
                with os.scandir(candidate) as listing:
                    children = [candidate / entry.name for entry in islice(listing, 10)]
                if len(children) >= 10:
                    continue
                marker = candidate / 'owner.lock'
                if not all(_plain(child) and (child.name == 'owner.lock' or
                        child.name.startswith('cmv-frozen-')) for child in children):
                    continue
                with marker.open('r+b') as owner:
                    if owner.read(64) != b'CMV-FROZEN-1\n':
                        continue
                    _lock(owner)  # A live owner or recovery worker makes this fail.
                    for child in children:
                        if child != marker:
                            child.unlink()
                marker.unlink()
                candidate.rmdir()
            except (OSError, ValueError):
                continue

    def close(self):
        self.owner.close()
        # Leave nonempty sessions for recovery; no deletion of active files.
        try:
            with os.scandir(self.path) as listing:
                names = [entry.name for entry in islice(listing, 2)]
            if names == ['owner.lock']:
                (self.path / 'owner.lock').unlink()
                self.path.rmdir()
        except OSError:
            pass
