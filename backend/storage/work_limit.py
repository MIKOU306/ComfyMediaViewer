"""Nonblocking bounded work admission; no unbounded waiter queue."""
from contextlib import contextmanager
import threading


class WorkBusy(RuntimeError):
    pass


class WorkLimit:
    def __init__(self, capacity):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('Positive integer work capacity required')
        self.semaphore = threading.BoundedSemaphore(capacity)

    @contextmanager
    def enter(self):
        if not self.semaphore.acquire(blocking=False):
            raise WorkBusy('Media processing is busy; retry shortly')
        try:
            yield
        finally:
            self.semaphore.release()
