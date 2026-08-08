"""Where jobs live between steps — SQLite, not a JSON blob.

Every other store in Venom (`venom/stores.py`, `flint_core.memory`) rewrites a
whole JSON document under a lock, which is exactly right for a handful of
reminders read once a minute. Jobs are a different load: each one appends
progress events as it goes, several may run at once, the web console reads
them while the scheduler writes them, and the scheduler asks "what is due?"
rather than "give me everything". Rewriting the file per progress note would
be both slow and lossy under concurrency.

So: one SQLite file in the same state dir, WAL mode so a reader never blocks
the writer, and a lock around the connection because runners touch it from
worker threads.

**Crash recovery.** A job in RUNNING when the process dies is a job whose step
will never return. On open, any such job is put back to WAITING with an event
recording what happened, so a power cut costs one step rather than a stuck
job. This is the smallest useful piece of self-repair, and it belongs here
rather than in every runner.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from flint_core.kernel.job import Job, JobState

log = logging.getLogger("flint.kernel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    goal        TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    scratch     TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL,
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    next_run_at REAL NOT NULL DEFAULT 0,
    interval    REAL NOT NULL DEFAULT 300,
    steps_done  INTEGER NOT NULL DEFAULT 0,
    max_steps   INTEGER NOT NULL DEFAULT 40,
    expires_at  REAL NOT NULL DEFAULT 0,
    urgent      INTEGER NOT NULL DEFAULT 0,
    origin      TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT '',
    say         TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(state, next_run_at);

CREATE TABLE IF NOT EXISTS job_events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts     REAL NOT NULL,
    note   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, seq);
"""

_COLUMNS = ("id", "type", "goal", "params", "scratch", "state", "created",
            "updated", "next_run_at", "interval", "steps_done", "max_steps",
            "expires_at", "urgent", "origin", "result", "say", "error")

#: Progress notes kept per job. A runner that logs every poll would otherwise
#: grow the table without bound over a long-lived job.
MAX_EVENTS_PER_JOB = 200


class JobStore:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets the console read while the scheduler writes. It is not
            # available on every filesystem (a tmpfs or a network mount can
            # refuse); falling back to the default journal is fine.
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                log.debug("WAL unavailable — using the default journal")
            self._db.executescript(SCHEMA)
            self._db.commit()
        self._recover()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── crash recovery ──────────────────────────────────────────────────────
    def _recover(self) -> int:
        """Put jobs that were mid-step when the process died back in the queue."""
        now = self._clock()
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE state = ?", (JobState.RUNNING,)
            ).fetchall()
            if not rows:
                return 0
            self._db.execute(
                "UPDATE jobs SET state = ?, updated = ?, next_run_at = ? "
                "WHERE state = ?",
                (JobState.WAITING, now, now, JobState.RUNNING),
            )
            self._db.executemany(
                "INSERT INTO job_events (job_id, ts, note) VALUES (?, ?, ?)",
                [(r["id"], now, "interrupted mid-step — requeued") for r in rows],
            )
            self._db.commit()
        log.info("kernel: requeued %d job(s) interrupted by a restart", len(rows))
        return len(rows)

    # ── writing ─────────────────────────────────────────────────────────────
    def add(self, job: Job) -> Job:
        row = job.to_row()
        with self._lock:
            self._db.execute(
                f"INSERT INTO jobs ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in _COLUMNS)})",
                row,
            )
            self._db.commit()
        return job

    def save(self, job: Job) -> Job:
        """Persist every mutable field of `job`, stamping `updated`."""
        job = replace(job, updated=self._clock())
        row = job.to_row()
        assignments = ", ".join(f"{c} = :{c}" for c in _COLUMNS if c != "id")
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {assignments} WHERE id = :id", row)
            self._db.commit()
        return job

    def claim(self, job_id: str) -> bool:
        """Mark a runnable job RUNNING. False if someone else got there first.

        The state check lives in the UPDATE so two schedulers (or a scheduler
        and a console "run now" button) can never hand the same job to two
        runners at once.
        """
        with self._lock:
            cursor = self._db.execute(
                "UPDATE jobs SET state = ?, updated = ? "
                "WHERE id = ? AND state IN (?, ?)",
                (JobState.RUNNING, self._clock(), job_id,
                 JobState.PENDING, JobState.WAITING),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def event(self, job_id: str, note: str) -> None:
        """Append one progress note, trimming the oldest past the cap."""
        note = " ".join(str(note or "").split())
        if not note:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO job_events (job_id, ts, note) VALUES (?, ?, ?)",
                (job_id, self._clock(), note),
            )
            self._db.execute(
                "DELETE FROM job_events WHERE job_id = ? AND seq NOT IN "
                "(SELECT seq FROM job_events WHERE job_id = ? "
                " ORDER BY seq DESC LIMIT ?)",
                (job_id, job_id, MAX_EVENTS_PER_JOB),
            )
            self._db.commit()

    def cancel(self, job_id: str) -> bool:
        """Stop one job. Terminal jobs are left alone."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE jobs SET state = ?, updated = ? "
                "WHERE id = ? AND state NOT IN (?, ?, ?)",
                (JobState.CANCELLED, self._clock(), job_id,
                 JobState.DONE, JobState.FAILED, JobState.CANCELLED),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def cancel_matching(self, text: str = "") -> int:
        """Cancel live jobs whose goal contains `text` — all of them if blank."""
        needle = (text or "").strip().lower()
        victims = [j for j in self.active()
                   if not needle or needle in j.goal.lower()]
        return sum(1 for j in victims if self.cancel(j.id))

    # ── reading ─────────────────────────────────────────────────────────────
    def _query(self, sql: str, args: tuple = ()) -> list[Job]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [Job.from_row(row) for row in rows]

    def get(self, job_id: str) -> Job | None:
        found = self._query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return found[0] if found else None

    def by_state(self, *states: str) -> list[Job]:
        if not states:
            return []
        placeholders = ", ".join("?" * len(states))
        return self._query(
            f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created",
            states,
        )

    def active(self) -> list[Job]:
        """Everything not yet finished — the list a user thinks of as "running"."""
        return self.by_state(JobState.PENDING, JobState.WAITING,
                             JobState.RUNNING, JobState.HELD)

    def held(self) -> list[Job]:
        return self.by_state(JobState.HELD)

    def due(self, now: float | None = None, limit: int = 20) -> list[Job]:
        now = self._clock() if now is None else now
        return self._query(
            "SELECT * FROM jobs WHERE state IN (?, ?) AND next_run_at <= ? "
            "ORDER BY next_run_at LIMIT ?",
            (JobState.PENDING, JobState.WAITING, now, limit),
        )

    def count_running(self, type: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM jobs WHERE state IN (?, ?, ?)"
        args: tuple = (JobState.PENDING, JobState.WAITING, JobState.RUNNING)
        if type:
            sql += " AND type = ?"
            args += (type,)
        with self._lock:
            return int(self._db.execute(sql, args).fetchone()[0])

    def events(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent progress notes, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, note FROM job_events WHERE job_id = ? "
                "ORDER BY seq DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [{"ts": r["ts"], "note": r["note"]} for r in reversed(rows)]

    # ── housekeeping ────────────────────────────────────────────────────────
    def expire(self, now: float | None = None) -> list[Job]:
        """Fail every job that has run out of steps or time. Returns them.

        This is the kernel's cost discipline: a runner that never returns
        Finish still stops, because the budget is enforced from outside it.
        """
        now = self._clock() if now is None else now
        expired = []
        for job in self.by_state(JobState.PENDING, JobState.WAITING):
            reason = job.out_of_budget(now)
            if not reason:
                continue
            self.event(job.id, f"stopped: {reason}")
            expired.append(self.save(replace(job, state=JobState.FAILED,
                                             error=reason)))
        return expired

    def purge(self, older_than_seconds: float = 7 * 86400,
              now: float | None = None) -> int:
        """Delete long-finished jobs and their events. Keeps the file small."""
        now = self._clock() if now is None else now
        cutoff = now - max(0.0, older_than_seconds)
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE state IN (?, ?, ?) AND updated < ?",
                (JobState.DONE, JobState.FAILED, JobState.CANCELLED, cutoff),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                marks = ", ".join("?" * len(ids))
                self._db.execute(f"DELETE FROM job_events WHERE job_id IN ({marks})", ids)
                self._db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", ids)
                self._db.commit()
        return len(ids)
