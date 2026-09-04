"""Background job tracking for the UI (15.3).

A six-hour broadcast does not process inside an HTTP request, so submitting a
source starts a worker thread and the UI polls the job. The job carries the
pipeline state (14장) and a live log, which is exactly what the progress monitor
of 15.3 shows.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from aicut.pipeline.states import State

log = logging.getLogger(__name__)

MAX_LOG_LINES = 500


@dataclass
class Job:
    job_id: str
    project_id: str
    source: str
    state: str = State.QUEUED.value
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str = ""
    report: dict[str, Any] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, level: str, message: str) -> None:
        with self._lock:
            self.log.append({"at": time.time(), "level": level, "message": message})
            del self.log[:-MAX_LOG_LINES]

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            tail = list(self.log[-120:])
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "source": self.source,
            "state": self.state,
            "running": self.running,
            "elapsed_sec": round((self.finished_at or time.time()) - self.started_at, 1),
            "error": self.error,
            "report": self.report,
            "log": tail,
        }


class JobLogHandler(logging.Handler):
    """Pipes the pipeline's own logging into the job's log (15.3 realtime log)."""

    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("aicut"):
            return
        self.job.append(record.levelname.lower(), record.getMessage())
        # State transitions are logged by the runner; mirror them onto the job so
        # the monitor does not have to parse the database on every poll.
        message = record.getMessage()
        if " -> " in message and message.startswith("project "):
            self.job.state = message.rsplit(" -> ", 1)[-1].strip()


class JobRunner:
    """Runs pipeline jobs on worker threads and keeps their state for polling."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        return [job.to_dict() for job in sorted(self.jobs.values(), key=lambda j: -j.started_at)]

    def start(self, job_id: str, project_id: str, source: str, work: Callable[[Job], Any]) -> Job:
        job = Job(job_id=job_id, project_id=project_id, source=source)
        with self._lock:
            self.jobs[job_id] = job

        def target() -> None:
            handler = JobLogHandler(job)
            root = logging.getLogger("aicut")
            root.addHandler(handler)
            try:
                result = work(job)
                job.state = getattr(result, "final_state", State.FAILED).value
                job.report = getattr(result, "report", {}) or {}
            except Exception as exc:
                job.state = State.FAILED.value
                job.error = f"{type(exc).__name__}: {exc}"
                job.append("error", job.error)
                log.error("job %s failed\n%s", job_id, traceback.format_exc())
            finally:
                root.removeHandler(handler)
                job.finished_at = time.time()

        threading.Thread(target=target, name=f"aicut-job-{job_id[:8]}", daemon=True).start()
        return job
