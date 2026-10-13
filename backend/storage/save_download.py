"""Publish completed downloads exclusively; never replace a concurrent file."""
import os
import errno
from pathlib import Path
import shutil
import tempfile


def save_download(directory, name, stream):
    directory = Path(directory)
    if not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('Download name must be a basename')
    descriptor, staging_name = tempfile.mkstemp(prefix='.cmv-download-', suffix='.part', dir=directory)
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, 'wb') as writer:
            shutil.copyfileobj(stream, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        basename = Path(name)
        for index in range(10000):
            target = directory / (name if index == 0 else f'{basename.stem} ({index}){basename.suffix}')
            try:
                try:
                    os.link(staging, target)
                except OSError as error:
                    if isinstance(error, FileExistsError):
                        raise
                    if os.name != 'nt' or (error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP)
                                           and getattr(error, 'winerror', None) not in (1, 50)):
                        raise
                    # Windows rename refuses an existing destination, including
                    # FAT/exFAT. Both paths are in the same destination directory.
                    # POSIX rename replaces files and is never used as fallback.
                    os.rename(staging, target)
                return target
            except FileExistsError:
                continue
        raise FileExistsError('No free download filename within the supported range')
    finally:
        staging.unlink(missing_ok=True)
