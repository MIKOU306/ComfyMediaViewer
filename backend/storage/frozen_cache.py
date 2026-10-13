"""Process-local frozen copies with independent readers and pinned eviction."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time

from backend.storage.work_limit import WorkBusy
from backend.storage.frozen_session import FrozenSession


class FrozenCache:
    def __init__(self, *, capacity=512 * 1024 * 1024, entries=4, lifetime=120, clock=time.monotonic, byte_budget=None):
        if any(type(value) is not int or value < 1 for value in (capacity, entries, lifetime)):
            raise ValueError('Frozen cache budgets require positive integers')
        self.capacity, self.maximum_entries, self.lifetime = capacity, entries, lifetime
        self.clock = clock
        self.byte_budget = byte_budget
        self.lock = threading.Lock()
        self.items = {}
        self.pending = {}
        self.clear_generation = 0
        self.timer = None
        self.timer_generation = 0
        self.session = None

    def _remove(self, key):
        item = self.items[key]
        if item['pins']:
            return False
        try:
            Path(item['file']).unlink(missing_ok=True)
        except OSError:
            item['used'] = self.clock()
            return False
        del self.items[key]
        if item.get('reservation') is not None:
            item['reservation'].__exit__(None, None, None)
        return True

    def _schedule(self):
        self.timer_generation += 1
        generation = self.timer_generation
        if self.timer:
            self.timer.cancel()
        self.timer = None
        deadlines = [item['used'] + self.lifetime for item in self.items.values() if not item['pins']]
        if not deadlines:
            return
        def tick():
            with self.lock:
                if generation != self.timer_generation:
                    return
                self._expire()
                self._schedule()
        self.timer = threading.Timer(max(.1, min(deadlines) - self.clock()), tick)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        now = self.clock()
        for key, item in list(self.items.items()):
            if now - item['used'] >= self.lifetime:
                self._remove(key)

    def expire(self):
        with self.lock:
            self._expire()
            self._schedule()

    @contextmanager
    def open(self, key, size, producer):
        if type(size) is not int or size < 0:
            raise ValueError('Frozen copy size must be a nonnegative integer')
        with self.lock:
            now = self.clock()
            self._expire()
            item = self.items.get(key)
            if item:
                if item.get('discard'):
                    raise WorkBusy('Frozen media is being released')
                item['pins'] += 1
                item['used'] = now
            else:
                if key in self.pending:
                    raise WorkBusy('A frozen copy of this version is being prepared')
                if size > self.capacity:
                    raise WorkBusy('Media exceeds frozen cache capacity')
                def crowded():
                    return (len(self.items) + len(self.pending) >= self.maximum_entries or
                            sum(entry['size'] for entry in self.items.values()) + sum(self.pending.values()) + size > self.capacity)
                while crowded():
                    available = [(entry['used'], candidate) for candidate, entry in self.items.items() if not entry['pins']]
                    if not available:
                        raise WorkBusy('Frozen media capacity is in use')
                    if not self._remove(min(available)[1]):
                        raise WorkBusy('Frozen media could not be released')
                self.pending[key] = size
                generation = self.clear_generation
        if item is None:
            filename = None
            reservation = None
            try:
                if self.byte_budget is not None:
                    for attempt in range(self.maximum_entries + 1):
                        try:
                            pending_reservation = self.byte_budget.enter(size)
                            pending_reservation.__enter__()
                            reservation = pending_reservation
                            break
                        except WorkBusy:
                            # Reclaim an idle copy before rejecting new work.
                            # Each retry removes one entry; pinned copies stay.
                            with self.lock:
                                available = [(entry['used'], candidate) for candidate, entry
                                             in self.items.items() if not entry['pins']]
                                if not available or not self._remove(min(available)[1]):
                                    raise
                    if reservation is None:
                        raise WorkBusy('Frozen media capacity remains in use')
                with self.lock:
                    if self.session is None:
                        self.session = FrozenSession()
                with tempfile.NamedTemporaryFile(prefix='cmv-frozen-', dir=self.session.path, delete=False) as output:
                    filename = output.name
                    result = producer(output)
                    if output.tell() != size:
                        raise ValueError('Frozen copy size changed')
                    output.flush()
                with self.lock:
                    if generation != self.clear_generation:
                        raise WorkBusy('Frozen copy creation cancelled during cleanup')
                    item = {'file':filename,'size':size,'pins':1,'used':self.clock(),'result':result,
                            'reservation':reservation}
                    self.items[key] = item
                    reservation = None  # ownership lasts until successful unlink
                    self.pending.pop(key, None)
            except BaseException:
                if filename:
                    try:
                        Path(filename).unlink(missing_ok=True)
                    except OSError:
                        # Keep failed cleanup counted and retry through expiry.
                        with self.lock:
                            self.items[key] = {'file':filename,'size':size,'pins':0,
                                'used':self.clock(),'discard':True,'reservation':reservation}
                            reservation = None
                            self._schedule()
                raise
            finally:
                if reservation is not None:
                    reservation.__exit__(None, None, None)
                with self.lock:
                    self.pending.pop(key, None)
        try:
            with open(item['file'], 'rb') as stream:
                yield stream, item['result']
        finally:
            with self.lock:
                item['pins'] -= 1
                item['used'] = self.clock()
                if item.get('discard'):
                    self._remove(key)
                self._schedule()

    def clear(self):
        with self.lock:
            self.clear_generation += 1
            for key in list(self.items):
                self.items[key]['discard'] = True
                self._remove(key)
            self._schedule()

    def reclaim_idle(self):
        """Release at most one unpinned copy for another frozen operation."""
        with self.lock:
            available = [(entry['used'], key) for key, entry in self.items.items()
                         if not entry['pins']]
            return bool(available) and self._remove(min(available)[1])


from backend.storage.byte_budget import FROZEN_BYTES
VIDEO_CACHE = FrozenCache(byte_budget=FROZEN_BYTES)
import atexit
atexit.register(VIDEO_CACHE.clear)
