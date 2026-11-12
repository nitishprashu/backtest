"""Background jobs with progress and cancellation (threads; the engine releases the GIL in NumPy)."""
from __future__ import annotations

import threading
import time as time_module
import traceback
import uuid
from typing import Any, Callable, Dict, Optional


class JobCancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "queued"            # queued | running | done | error | cancelled
        self.progress = 0.0
        self.message = ""
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.created = time_module.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.cancel_requested = False
        self._lock = threading.Lock()

    def update(self, progress: Optional[float] = None, message: Optional[str] = None) -> None:
        if self.cancel_requested:
            raise JobCancelled()
        with self._lock:
            if progress is not None:
                self.progress = max(0.0, min(1.0, float(progress)))
            if message is not None:
                self.message = message

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            end = self.finished or time_module.time()
            return {"id": self.id, "kind": self.kind, "status": self.status, "progress": self.progress,
                    "message": self.message, "result": self.result, "error": self.error,
                    "elapsed": (end - self.started) if self.started else 0.0}


class JobManager:
    def __init__(self, max_keep: int = 50):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max_keep = max_keep
        self._run_lock = threading.Lock()      # one heavy job at a time keeps the database reads sane

    def submit(self, kind: str, fn: Callable[[Job], Dict[str, Any]]) -> Job:
        job = Job(kind)
        with self._lock:
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j.created)[:-self._max_keep]:
                if old.status in ("done", "error", "cancelled"):
                    self._jobs.pop(old.id, None)

        def runner():
            with self._run_lock:
                if job.cancel_requested:
                    job.status, job.finished = "cancelled", time_module.time()
                    return
                job.status, job.started = "running", time_module.time()
                try:
                    job.result = fn(job)
                    job.status = "done"
                    job.progress = 1.0
                except JobCancelled:
                    job.status = "cancelled"
                except Exception as exc:
                    job.status = "error"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.message = traceback.format_exc(limit=6)
                finally:
                    job.finished = time_module.time()

        threading.Thread(target=runner, daemon=True, name=f"job-{job.id}").start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)
