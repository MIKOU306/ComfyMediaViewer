"""Bounded prompt index maintenance for a writer-guarded server lifetime."""
import threading
from .prompt_fts import refresh_pending


class PromptIndexRefresh:
    def __init__(self, database, *, max_rows=500):
        self.database = database
        self.max_rows = max_rows
        self.after_id = 0
        self._lock = threading.Lock()

    def refresh(self):
        # Concurrent maintenance owns the batch; pending rows stay searchable.
        if not self._lock.acquire(blocking=False):
            return None
        try:
            result = refresh_pending(self.database, max_rows=self.max_rows,
                                     after_id=self.after_id)
            self.after_id = (result['nextAfterId']
                             if result['hasMore'] else 0)
            return result
        finally:
            self._lock.release()
