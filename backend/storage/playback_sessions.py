"""Bounded playback leases over an already verified immutable resource.

The provider yields an independent reader context factory. A lease owns the
provider until its last reader closes; no source-file access occurs here.
"""
from contextlib import contextmanager
import secrets
import threading
import time

from .work_limit import WorkBusy


class PlaybackExpired(ValueError):
    pass


class PlaybackSessions:
    def __init__(self, *, capacity=4, lifetime=120, clock=time.monotonic):
        if any(type(value) is not int or value < 1 for value in (capacity,lifetime)):
            raise ValueError('Playback budgets must be positive integers')
        self.capacity, self.lifetime, self.clock = capacity,lifetime,clock
        self.lock=threading.RLock()
        self.items={}
        self.pending=0
        self.clear_generation=0
        self.timer=None
        self.timer_generation=0

    def _schedule(self):
        self.timer_generation+=1
        generation=self.timer_generation
        if self.timer:
            self.timer.cancel()
        self.timer=None
        deadlines=[item['used']+self.lifetime for item in self.items.values() if not item['closed']]
        if not deadlines:
            return
        def tick():
            with self.lock:
                if generation!=self.timer_generation:
                    return
                self.expire()
        self.timer=threading.Timer(max(.1,min(deadlines)-self.clock()),tick)
        self.timer.daemon=True
        self.timer.start()

    def _discard(self, token):
        item=self.items[token]
        item['closed']=True
        if item['readers']==0:
            del self.items[token]
            item['provider'].__exit__(None,None,None)

    def expire(self):
        with self.lock:
            for token,item in list(self.items.items()):
                if self.clock()-item['used'] >= self.lifetime:
                    self._discard(token)
            self._schedule()

    def start(self, identity, provider):
        # Identity is a tuple supplied by the verified route, not a path lookup.
        if not isinstance(identity,tuple) or not identity:
            raise ValueError('Playback requires a frozen identity tuple')
        with self.lock:
            self.expire()
            if len(self.items)+self.pending >= self.capacity:
                raise WorkBusy('Playback session capacity is in use')
            self.pending+=1
            generation=self.clear_generation
        entered=False
        try:
            opener=provider.__enter__()
            entered=True
            if not callable(opener):
                raise ValueError('Playback provider must yield a reader factory')
            token=secrets.token_urlsafe(32)
            with self.lock:
                if generation != self.clear_generation:
                    raise PlaybackExpired('Playback creation cancelled during cleanup')
                self.items[token]={'identity':identity,'provider':provider,'opener':opener,
                                   'readers':0,'closed':False,'used':self.clock()}
                self._schedule()
            entered=False  # ownership transferred to registry
            return token
        finally:
            if entered:
                provider.__exit__(None,None,None)
            with self.lock:
                self.pending-=1

    def close(self, token, identity):
        with self.lock:
            item=self.items.get(token)
            if item is None:
                return False
            if item['identity'] != identity:
                raise PlaybackExpired('Playback identity does not match')
            self._discard(token)
            self._schedule()
            return True

    @contextmanager
    def read(self, token, identity):
        with self.lock:
            self.expire()
            item=self.items.get(token)
            if item is None or item['closed'] or item['identity']!=identity:
                raise PlaybackExpired('Playback session expired or identity changed')
            item['readers']+=1
            item['used']=self.clock()
        try:
            with item['opener']() as reader:
                yield reader
        finally:
            with self.lock:
                item['readers']-=1
                item['used']=self.clock()
                if item['closed'] and item['readers']==0:
                    self._discard(token)
                self._schedule()

    def clear(self):
        with self.lock:
            self.clear_generation+=1
            for token in list(self.items):
                self._discard(token)
            self._schedule()
