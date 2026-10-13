"""One bounded background catalog scan per server; cancellation is cooperative."""
import threading
import uuid
from backend.storage.scans import CatalogScans
from backend.storage.scan_directory import scan_directory


class ScanBusy(RuntimeError):
    pass


class ScanJobs:
    def __init__(self, database, library_uuid, *, fingerprint=None):
        self.database, self.library_uuid = database, library_uuid
        self.fingerprint = fingerprint
        scans = CatalogScans(database)
        scans.initialize()
        self.startup_recovery = scans.recover_interrupted(library_uuid)
        self.lock = threading.Lock()
        self.jobs = {}
        self.worker = None
        self.cancel_event = None

    def start(self, root_uuid):
        catalog = CatalogScans(self.database).catalog
        with catalog._transaction(write=False) as db:
            catalog._scope(db, self.library_uuid, root_uuid)
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise ScanBusy('A catalog scan is already running')
            while len(self.jobs) >= 20:
                self.jobs.pop(next(iter(self.jobs)))
            job = str(uuid.uuid4())
            event = threading.Event()
            self.cancel_event = event
            self.jobs[job] = {'jobId': job, 'rootId': root_uuid, 'state': 'running'}
            self.worker = threading.Thread(target=self._run, args=(job, root_uuid, event), daemon=True)
            self.worker.start()
            return dict(self.jobs[job])

    def _run(self, job, root_uuid, event):
        def progress(value):
            with self.lock:
                self.jobs[job].update(value)
        try:
            result = scan_directory(self.database, self.library_uuid, root_uuid,
                                    cancelled=event.is_set, progress=progress, fingerprint=self.fingerprint)
        except Exception as error:
            result = {'state': 'failed', 'coverage': 'partial', 'error': str(error)}
        with self.lock:
            self.jobs[job].update(result)

    def status(self, job):
        with self.lock:
            if job not in self.jobs:
                raise ValueError('Unknown or expired scan job')
            return dict(self.jobs[job])

    def cancel(self, job):
        with self.lock:
            if job not in self.jobs:
                raise ValueError('Unknown or expired scan job')
            if self.jobs[job]['state'] == 'running':
                self.cancel_event.set()
                self.jobs[job]['cancelRequested'] = True
            return dict(self.jobs[job])
