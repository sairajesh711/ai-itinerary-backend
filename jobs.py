# jobs.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
import logging, threading, time, uuid

log = logging.getLogger("jobs")

@dataclass
class Job:
    id: str
    status: str = "pending"  # pending|running|done|error
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    steps: List[Dict[str, Any]] = field(default_factory=list)  # {'seq', 'ts', 'msg'}
    result: Optional[Any] = None
    error: Optional[str] = None

class JobManager:
    def __init__(self, max_workers: int = 4):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = threading.BoundedSemaphore(max_workers)

    def _progress_for(self, job: Job) -> Callable[[str], None]:
        def progress(msg: str) -> None:
            with self._lock:
                seq = len(job.steps) + 1
                job.steps.append(
                    {"seq": seq, "ts": datetime.now(timezone.utc).isoformat(), "msg": msg}
                )
                job.updated_at = datetime.now(timezone.utc).isoformat()
        return progress

    def create(self, target: Callable[..., Any], *, args: tuple = (), kwargs: dict | None = None) -> Job:
        if kwargs is None:
            kwargs = {}
        job = Job(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job

        progress = self._progress_for(job)

        def runner():
            job.status = "running"
            progress("Job started")
            self._pool.acquire()
            try:
                res = target(*args, **{**kwargs, "progress": progress})
                with self._lock:
                    job.result = res
                    job.status = "done"
                    job.updated_at = datetime.now(timezone.utc).isoformat()
                progress("Job finished")
            except Exception as e:
                log.exception("Job %s failed", job.id)
                with self._lock:
                    job.error = str(e)
                    job.status = "error"
                    job.updated_at = datetime.now(timezone.utc).isoformat()
                progress(f"Error: {e}")
            finally:
                self._pool.release()

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def prune(self, older_than_seconds: int = 3600) -> None:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            to_del = []
            for jid, job in self._jobs.items():
                try:
                    ts = datetime.fromisoformat(job.updated_at).timestamp()
                except Exception:
                    ts = time.time()
                if ts < cutoff:
                    to_del.append(jid)
            for jid in to_del:
                self._jobs.pop(jid, None)

manager = JobManager()
