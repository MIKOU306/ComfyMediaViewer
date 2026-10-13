"""Explicit, atomic creation of a per-data-directory cursor signing key."""
import os
import secrets
import tempfile
from pathlib import Path


def load_or_create_key(path):
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (candidate.exists() and
                getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Cursor key path cannot contain links')
    if not path.parent.is_dir():
        raise ValueError('Existing data directory required')
    def read_key():
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            stat = os.fstat(stream.fileno())
            import stat as stat_module
            if not stat_module.S_ISREG(stat.st_mode) or stat.st_size != 32:
                raise ValueError('Invalid cursor key file')
            key = stream.read(33)
        if len(key) != 32:
            raise ValueError('Invalid cursor key length')
        return key
    if path.exists():
        return read_key()
    descriptor, temporary = tempfile.mkstemp(prefix='.cursor-key-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # Exclusive publication, never replace an existing key.
        except FileExistsError:
            pass
        return read_key()
    finally:
        Path(temporary).unlink()
