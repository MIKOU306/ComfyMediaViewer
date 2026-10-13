"""Bounded, nonblocking admission: one active producer per explicit target."""
from contextlib import contextmanager
import threading
from backend.storage.work_limit import WorkBusy


class SingleFlight:
    def __init__(self, capacity):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('Positive single-flight capacity required')
        self.capacity = capacity
        self.active = set()
        self.lock = threading.Lock()

    @contextmanager
    def enter(self, key):
        with self.lock:
            if key in self.active:
                raise WorkBusy('This thumbnail version is already being prepared; retry shortly')
            if len(self.active) >= self.capacity:
                raise WorkBusy('Thumbnail preparation capacity is in use')
            self.active.add(key)
        try:
            yield
        finally:
            with self.lock:
                self.active.remove(key)
