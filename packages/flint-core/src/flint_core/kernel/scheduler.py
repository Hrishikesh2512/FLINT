"""The scheduler — the loop that actually moves jobs forward.

One tick, in this order, and the order is the design:

    1. deliver   a result already in hand beats spending money on new work
    2. expire    stop anything out of steps or out of time, before running it
    3. run       take what's due, one step each, within the concurrency cap

Modelled on `venom.watch.WatchLoop`, which got the two hard parts right and
deserves to have them generalised rather than reimplemented:

  * **Cost.** Budgets are enforced from outside the runner (step 2), so a
    runner that never returns Finish still stops. Per-type ceilings are
    checked at submit time, when there is still someone to tell.

  * **Interrupting.** A finished job does not get to barge in. `deliver` may
    refuse — mid-conversation, quiet hours, do-not-disturb — and the result
    waits in HELD until it is welcome. One interruption per tick, ever.

Failures never escape: a runner that raises is treated as a retryable step,
because the alternative (killing a day-long job over one flaky HTTP call) is
worse, and the step budget still guarantees it stops.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from flint_core.kernel.job import Continue, Fail, Finish, Job, JobState, Outcome
from flint_core.kernel.runner import JobContext, RunnerRegistry, UnknownJobTypeError
from flint_core.kernel.store import JobStore

log = logging.getLogger("flint.kernel")


class Scheduler:
    def __init__(
        self,
        store: JobStore,
        runners: RunnerRegistry,
        *,
        services: dict[str, Any] | None = None,
        deliver: Callable[[Job], bool] | None = None,
        # Called when a job settles: (job, succeeded, seconds). The kernel
        # knows better than anything else whether work actually worked, so
        # this is where the record of it has to come from — without it the
        # learning tier has tools to read a history that nobody writes.
        on_outcome: Callable[[Job, bool, float], None] | None = None,
        tick_seconds: float = 30.0,
        max_running: int = 4,
        max_passes: int = 4,
        clock: Callable[[], float] = time.time,
    ):
        self._store = store
        self._runners = runners
        self._services = dict(services or {})
        # Default delivery: nothing has a mouth, so a finished job with
        # something to say simply completes quietly rather than piling up.
        self._deliver = deliver or (lambda job: True)
        self._on_outcome = on_outcome
        self._tick_seconds = float(tick_seconds)
        self._max_running = max(1, int(max_running))
        self._max_passes = max(1, int(max_passes))
        self._clock = clock

    # ── submitting ──────────────────────────────────────────────────────────
    def submit(self, type: str, goal: str, **overrides: Any) -> Job:
        """Create and persist a job. Raises ValueError when a limit says no.

        The per-type ceiling is checked here rather than in the loop, because
        this is the only moment there is still a user listening who can be
        told "you already have three of those running".
        """
        spec = self._runners.get(type)     # UnknownJobTypeError if bogus
        live = self._store.count_running(spec.type)
        if live >= spec.max_concurrent:
            raise ValueError(
                f"already running {live} {spec.type} job(s) — that's the limit. "
                f"Cancel one first."
            )
        # The job's timestamps must come from the scheduler's clock, not the
        # wall clock: a host that injects a clock (or a test) would otherwise
        # create jobs whose next_run_at its own tick can never reach.
        overrides.setdefault("now", self._clock())
        job = self._runners.build(type, goal, **overrides)
        self._store.add(job)
        self._store.event(job.id, f"created: {job.goal}")
        log.info("kernel: job %s (%s) created — %s", job.id, job.type, job.goal)
        return job

    def cancel(self, job_id: str) -> bool:
        stopped = self._store.cancel(job_id)
        if stopped:
            self._store.event(job_id, "cancelled")
            log.info("kernel: job %s cancelled", job_id)
        return stopped

    def cancel_matching(self, text: str = "") -> int:
        """Stop live jobs whose goal mentions `text` — all of them if blank.

        For the spoken case ("stop that research"), where the user has a few
        words of the goal rather than an id.
        """
        stopped = self._store.cancel_matching(text)
        if stopped:
            log.info("kernel: cancelled %d job(s) matching %r", stopped, text)
        return stopped

    # ── one step of one job ─────────────────────────────────────────────────
    async def _run_step(self, job: Job) -> None:
        if not self._store.claim(job.id):
            return          # cancelled or picked up elsewhere between tick and now

        notes: list[str] = []
        context = JobContext(job=job, log=notes.append, services=self._services)
        try:
            spec = self._runners.get(job.type)
        except UnknownJobTypeError as exc:
            # A job whose runner was removed or renamed. Nothing can ever move
            # it forward, so retrying would just burn its budget silently.
            self._finish_failed(job, str(exc), retry=False)
            return

        try:
            if inspect.iscoroutinefunction(spec.handler):
                outcome = await spec.handler(context)
            else:
                # Runners are allowed to block for the length of one step —
                # off the event loop, so audio and the live session never stall.
                outcome = await asyncio.to_thread(spec.handler, context)
        except Exception as exc:  # noqa: BLE001 — a broken runner must not stop the loop
            log.warning("kernel: job %s (%s) step raised: %s", job.id, job.type, exc)
            outcome = Fail(f"{type(exc).__name__}: {exc}", retry=True)

        for note in notes:
            self._store.event(job.id, note)
        self._apply(job, outcome)

    def _record_outcome(self, job: Job, ok: bool) -> None:
        if self._on_outcome is None:
            return
        try:
            self._on_outcome(job, ok, max(0.0, self._clock() - job.created))
        except Exception:            # noqa: BLE001 — bookkeeping is not the job
            log.debug("kernel: outcome callback failed", exc_info=True)

    def _apply(self, job: Job, outcome: Outcome) -> None:
        now = self._clock()
        stepped = replace(job, steps_done=job.steps_done + 1)

        if isinstance(outcome, Finish):
            state = JobState.HELD if outcome.say.strip() else JobState.DONE
            self._store.save(replace(
                stepped, state=state, result=outcome.result,
                say=outcome.say.strip(), error="",
                urgent=job.urgent or outcome.urgent))
            self._store.event(job.id, f"finished: {outcome.result}")
            log.info("kernel: job %s finished after %d step(s)",
                     job.id, stepped.steps_done)
            self._record_outcome(stepped, True)
            return

        if isinstance(outcome, Fail):
            self._finish_failed(stepped, outcome.error, retry=outcome.retry, now=now)
            return

        if not isinstance(outcome, Continue):
            self._finish_failed(
                stepped,
                f"runner returned {type(outcome).__name__}, not a job outcome",
                retry=False)
            return

        # Continue: carry the scratch forward — this is the whole point of a
        # multi-step job, and the thing FLINT's flat planner cannot express.
        sleep = job.interval if outcome.sleep is None else max(0.0, outcome.sleep)
        moved = replace(stepped.with_scratch(outcome.scratch),
                        state=JobState.WAITING, next_run_at=now + sleep, error="")
        if outcome.note:
            self._store.event(job.id, outcome.note)
        # Check the budget now rather than next tick, so a job that has just
        # spent its last step reports as finished instead of sitting in WAITING.
        reason = moved.out_of_budget(now)
        if reason:
            self._store.save(replace(moved, state=JobState.FAILED, error=reason))
            self._store.event(job.id, f"stopped: {reason}")
            return
        self._store.save(moved)

    def _finish_failed(self, job: Job, error: str, *, retry: bool,
                       now: float | None = None) -> None:
        now = self._clock() if now is None else now
        if not retry:
            self._store.save(replace(job, state=JobState.FAILED, error=error))
            self._store.event(job.id, f"failed: {error}")
            log.warning("kernel: job %s failed — %s", job.id, error)
            self._record_outcome(job, False)
            return
        retrying = replace(job, state=JobState.WAITING, error=error,
                           next_run_at=now + job.interval)
        reason = retrying.out_of_budget(now)
        if reason:
            self._store.save(replace(retrying, state=JobState.FAILED,
                                     error=f"{error} ({reason})"))
            self._store.event(job.id, f"gave up: {error} — {reason}")
            return
        self._store.save(retrying)
        self._store.event(job.id, f"step failed, will retry: {error}")

    # ── delivery ────────────────────────────────────────────────────────────
    def _deliver_one(self) -> bool:
        """Speak at most one finished result. True if something was delivered."""
        for job in self._store.held():
            try:
                if not self._deliver(job):
                    continue        # not a good moment — it keeps waiting
            except Exception:
                log.exception("kernel: delivering job %s failed", job.id)
                continue
            self._store.save(replace(job, state=JobState.DONE))
            self._store.event(job.id, "delivered")
            return True             # one interruption at a time
        return False

    # ── the loop ────────────────────────────────────────────────────────────
    async def tick(self) -> None:
        if self._deliver_one():
            return                  # a result in hand beats new work this tick
        for job in self._store.expire():
            log.info("kernel: job %s stopped — %s", job.id, job.error)

        # Run what's due, then chase only the jobs that asked to be woken
        # again immediately (Continue(sleep=0)) — consecutive steps of one
        # continuous piece of work, like a research job moving from planning
        # to its first search. Making those wait a full tick each would turn
        # two minutes of work into ten.
        #
        # Deliberately *only* those: a backlog of unrelated due jobs still
        # drains `max_running` per tick, so one busy type cannot monopolise
        # the loop, and `max_passes` bounds a runner that always asks for
        # sleep=0.
        due = self._store.due(limit=self._max_running)
        for _ in range(self._max_passes):
            if not due:
                break
            await asyncio.gather(*(self._run_step(job) for job in due))
            # Re-read exactly the jobs we just ran, rather than re-querying the
            # queue: with several jobs due at the same instant, a LIMITed query
            # can hand back a different one on the tie and end the chase early.
            due = self._refresh_due(job.id for job in due)
        self._deliver_one()

    def _refresh_due(self, job_ids) -> list[Job]:
        """Of the jobs just run, the ones asking to go again right now."""
        now = self._clock()
        refreshed = (self._store.get(job_id) for job_id in job_ids)
        return [job for job in refreshed if job is not None and job.is_due(now)]

    async def run(self) -> None:
        log.info("kernel: scheduler up (tick %.0fs, %d job type(s): %s)",
                 self._tick_seconds, len(self._runners),
                 ", ".join(self._runners.types()) or "none")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("kernel: tick failed")
            await asyncio.sleep(self._tick_seconds)

    # ── housekeeping ────────────────────────────────────────────────────────
    def active(self) -> list[Job]:
        """Everything not yet finished — what a user thinks of as "running"."""
        return self._store.active()

    def purge_finished(self, keep_hours: float) -> int:
        """Forget jobs that finished long enough ago to be no longer interesting."""
        return self._store.purge(older_than_seconds=max(0.0, keep_hours) * 3600)

    # ── reporting ───────────────────────────────────────────────────────────
    def summary(self) -> str:
        """One spoken line about everything in flight."""
        jobs = self._store.active()
        if not jobs:
            return "Nothing running in the background right now."
        lines = [job.describe() for job in jobs]
        head = "I'm working on " if len(lines) == 1 else f"I have {len(lines)} things going: "
        return head + "; ".join(lines) + "."

    def status(self, job_id: str, events: int = 5) -> str:
        """A fuller answer about one job, including recent progress."""
        job = self._store.get(job_id)
        if job is None:
            return f"I don't have a job {job_id}."
        parts = [job.describe()]
        if job.result:
            parts.append(f"Result: {job.result}")
        recent = self._store.events(job.id, limit=events)
        if recent and not job.result:
            parts.append("Latest: " + "; ".join(event["note"] for event in recent[-3:]))
        return " ".join(parts)
