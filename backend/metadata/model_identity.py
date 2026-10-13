"""On-demand model-file identity checks; names and metadata claims are not hashes."""
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path


class ModelIdentityError(ValueError):
    pass


def inspect_model_file(path, *, max_bytes=32*1024**3, max_seconds=120,
                       cancelled=None, progress=None):
    try:
        return _inspect_model_file(path, max_bytes=max_bytes, max_seconds=max_seconds,
                                   cancelled=cancelled, progress=progress)
    except OSError as error:
        raise ModelIdentityError('Model cannot be read or changed during verification') from error


def _inspect_model_file(path, *, max_bytes, max_seconds, cancelled, progress):
    """Read twice and reject differing content or file identities.

    Callers must run this as a cancellable job, not as part of a library scan.
    The result describes this observation; it is not an execution attestation.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ModelIdentityError('Invalid model byte budget')
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 3600:
        raise ModelIdentityError('Invalid model time budget')
    target = Path(path).absolute()
    if target.is_symlink() or not target.is_file():
        raise ModelIdentityError('Model must be a regular file')
    started = time.monotonic()
    before = target.stat()
    if before.st_size == 0:
        raise ModelIdentityError('Empty file is not a model artifact')
    if before.st_size > max_bytes:
        raise ModelIdentityError('Model exceeds byte budget')
    def signature(info):
        return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)
    hashes = []
    read_bytes = 0
    with target.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if signature(before) != signature(opened):
            raise ModelIdentityError('Model changed before reading')
        for pass_index in range(2):
            stream.seek(0)
            digest = hashlib.sha256()
            pass_bytes = 0
            while True:
                if (cancelled and cancelled()) or time.monotonic()-started > max_seconds:
                    raise ModelIdentityError('Model verification cancelled or time budget exceeded')
                chunk = stream.read(min(4*1024**2, max_bytes-pass_bytes+1))
                if not chunk:
                    break
                pass_bytes += len(chunk)
                read_bytes += len(chunk)
                if pass_bytes > max_bytes:
                    raise ModelIdentityError('Model exceeds byte budget during reading')
                digest.update(chunk)
                if progress:
                    progress({'pass':pass_index+1, 'bytesRead':read_bytes, 'sizeBytes':before.st_size})
            if pass_bytes != before.st_size:
                raise ModelIdentityError('Model size changed during reading')
            hashes.append(digest.hexdigest())
        after_fd = os.fstat(stream.fileno())
    after = target.stat()
    if hashes[0] != hashes[1] or any(signature(info)!=signature(before) for info in (after_fd, after)):
        raise ModelIdentityError('Model changed during verification')
    return {'path':str(target), 'sha256':hashes[0], 'sizeBytes':before.st_size,
            'identityState':'file_hash_verified', 'mtimeNs':before.st_mtime_ns,
            'bytesRead':read_bytes, 'hashPasses':2,
            'verifiedAt':datetime.now(timezone.utc).isoformat(),
            'observation':{'mtimeNs':after.st_mtime_ns, 'ctimeNs':after.st_ctime_ns,
                           'device':after.st_dev, 'inode':after.st_ino}}
