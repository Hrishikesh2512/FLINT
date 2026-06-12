"""Asynchronous job pipeline: priority-ordered worker threads + an event bus.

Tool executions, vision calls and remote commands are submitted here instead
of blocking the asyncio session loop or the Qt frame.  Each job runs on a
pooled worker thread; callers can await the result through the job's
``concurrent.futures.Future`` (``asyncio.wrap_future`` makes it awaitable).

The event bus lets loosely-coupled parts of the app observe the pipeline:
the UI subscribes to show running-job spinners, the WebSocket listener
subscribes to forward job lifecycle events to remote clients.

Events emitted:
    job_submitted, job_started, job_finished, job_failed   payload: job dict
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable


class Priority(IntEnum):
    HIGH   = 0
    NORMAL = 1
    LOW    = 2


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int
    job: "Job" = field(compare=False)


class Job:
    __slots__ = ("id", "name", "fn", "priority", "future",
                 "submitted_at", "started_at", "finished_at", "state")

    def __init__(self, job_id: int, name: str, fn: Callable[[], Any],
                 priority: Priority):
        self.id = job_id
        self.name = name
        self.fn = fn
        self.priority = priority
        self.future: Future = Future()
        self.submitted_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.state = "queued"

    def describe(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "priority": int(self.priority),
            "queued_for": round((self.started_at or time.time()) - self.submitted_at, 3),
            "ran_for": (round(self.finished_at - self.started_at, 3)
                        if self.started_at and self.finished_at else None),
        }


class EventBus:
    """Minimal thread-safe pub/sub.  Subscriber errors never break emitters."""

    def __init__(self):
        self._subs: dict[str, list[Callable[[dict], None]]] = {}
        self._lock = threading.Lock()

    def on(self, event: str, cb: Callable[[dict], None]) -> None:
        with self._lock:
            self._subs.setdefault(event, []).append(cb)

    def off(self, event: str, cb: Callable[[dict], None]) -> None:
        with self._lock:
            try:
                self._subs.get(event, []).remove(cb)
            except ValueError:
                pass

    def emit(self, event: str, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(event, []))
        for cb in subs:
            try:
                cb(payload)
            except Exception:
                traceback.print_exc()


class AsyncPipeline:
    """Priority job queue drained by a fixed pool of daemon worker threads."""

    def __init__(self, workers: int = 4, name: str = "flint-pipeline"):
        self.bus = EventBus()
        self._queue: queue.PriorityQueue[_QueueItem] = queue.PriorityQueue()
        self._seq = itertools.count()
        self._job_ids = itertools.count(1)
        self._active: dict[int, Job] = {}
        self._lock = threading.Lock()
        self._workers = [
            threading.Thread(target=self._worker, daemon=True,
                             name=f"{name}-{i}")
            for i in range(workers)
        ]
        for w in self._workers:
            w.start()

    # ── submission ───────────────────────────────────────────────────────────
    def submit(self, name: str, fn: Callable[..., Any], *args,
               priority: Priority = Priority.NORMAL, **kwargs) -> Job:
        job = Job(next(self._job_ids), name,
                  (lambda: fn(*args, **kwargs)), priority)
        self._queue.put(_QueueItem(int(priority), next(self._seq), job))
        self.bus.emit("job_submitted", job.describe())
        return job

    def run(self, name: str, fn: Callable[..., Any], *args,
            timeout: float | None = None,
            priority: Priority = Priority.NORMAL, **kwargs) -> Any:
        """Submit and block the calling (non-worker) thread for the result."""
        return self.submit(name, fn, *args, priority=priority,
                           **kwargs).future.result(timeout)

    # ── introspection ────────────────────────────────────────────────────────
    def active_jobs(self) -> list[dict]:
        with self._lock:
            return [j.describe() for j in self._active.values()]

    def queued_count(self) -> int:
        return self._queue.qsize()

    # ── worker loop ──────────────────────────────────────────────────────────
    def _worker(self):
        while True:
            item = self._queue.get()
            job = item.job
            if job.future.cancelled():
                continue
            job.state = "running"
            job.started_at = time.time()
            with self._lock:
                self._active[job.id] = job
            self.bus.emit("job_started", job.describe())
            try:
                result = job.fn()
            except BaseException as e:
                job.state = "failed"
                job.finished_at = time.time()
                job.future.set_exception(e)
                desc = job.describe()
                desc["error"] = f"{type(e).__name__}: {e}"
                self.bus.emit("job_failed", desc)
                traceback.print_exc()
            else:
                job.state = "finished"
                job.finished_at = time.time()
                job.future.set_result(result)
                self.bus.emit("job_finished", job.describe())
            finally:
                with self._lock:
                    self._active.pop(job.id, None)


_pipeline: AsyncPipeline | None = None
_pipeline_lock = threading.Lock()


def get_pipeline() -> AsyncPipeline:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = AsyncPipeline()
        return _pipeline


if __name__ == "__main__":
    p = get_pipeline()
    p.bus.on("job_finished", lambda d: print("done:", d))
    jobs = [p.submit(f"job-{i}", lambda i=i: i * i) for i in range(5)]
    print("results:", [j.future.result(5) for j in jobs])
