"""Cooperative process guard. Historical executables do not obey this lock."""
from pathlib import Path
import os


class WriterBusy(RuntimeError):
    pass


class DataWriterGuard:
    def __init__(self, data_dir):
        self.path = Path(data_dir).resolve() / '.cmv-writer.lock'
        self.stream = None

    def acquire(self):
        if self.stream is not None: return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise WriterBusy('Another cooperating process owns this data directory') from error
        self.stream = stream
        return self

    def release(self):
        if self.stream is None: return
        stream, self.stream = self.stream, None
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self): return self.acquire()
    def __exit__(self, *_): self.release()
