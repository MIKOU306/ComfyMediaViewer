"""Order-independent, bounded-memory stat fingerprint; never read media bytes."""
import hashlib
import os
from pathlib import Path

MEDIA_EXTENSIONS = frozenset(('.png','.jpg','.jpeg','.webp','.gif','.bmp',
                              '.mp4','.webm','.mov','.m4v','.mkv','.avi'))


def fingerprint(path, recursive=True, *, extensions=MEDIA_EXTENSIONS, cancelled=lambda: False):
    root = Path(path)
    before = root.stat()
    total = count = size = newest = entries = 0
    stack = []
    def retain(kind, name, info):
        nonlocal total, entries
        fields = (kind,name,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_dev,info.st_ino)
        total = (total + int.from_bytes(hashlib.sha256(repr(fields).encode('utf-8','surrogatepass')).digest(),'big')) % (1 << 256)
        entries += 1
    retain('root',str(root),before)
    try:
        stack.append(os.scandir(root))
        while stack:
            if cancelled():
                from .scans import ScanCancelled
                raise ScanCancelled('Directory fingerprint cancelled')
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop().close();continue
            info = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or getattr(info,'st_file_attributes',0) & 0x400:
                retain('link',entry.path,info);continue
            if entry.is_dir(follow_symlinks=False):
                retain('directory',entry.path,info)
                if recursive:
                    stack.append(os.scandir(entry.path))
            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in extensions:
                retain('media',entry.path,info);count+=1;size+=info.st_size;newest=max(newest,info.st_mtime_ns)
        after = root.stat()
        if (before.st_dev,before.st_ino,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_mtime_ns):
            raise OSError('Directory changed during fingerprint collection')
    finally:
        for iterator in stack:
            iterator.close()
    digest = hashlib.sha256(f'1:{entries}:{total}'.encode()).hexdigest()
    return f'{count}:{size}:{newest}:{digest}'
