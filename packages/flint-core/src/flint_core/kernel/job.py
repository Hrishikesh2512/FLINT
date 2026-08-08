"""The job model — what a unit of self-directed work *is*.

Everything else in Venom answers inside a conversation: a tool is called, it
returns a string, the session speaks it, and the whole thing dies when the
conversation does. A job is the opposite shape. It is handed a goal, it runs
on its own schedule for as long as its budget allows, it survives a reboot,
and it comes back when it has something to say.

`venom/watch.py` already proved the pattern for one narrow case ("tell me when
X happens"). This is that pattern with the watch-specific parts taken out:

    runner   ->  does ONE step of work and says what should happen next
    kernel   ->  owns scheduling, persistence, budgets, retries, delivery

A step returns one of three outcomes, and that small vocabulary is what lets a
polling watch, a multi-step research job and a long coding task all be the
same kind of thing:

    Continue  — not finished; here's what I learned, wake me again later
    Finish    — done; here's the result (and optionally what to tell the user)
    Fail      — this went wrong (retry=True if it's worth another go)

`Continue.scratch` is the piece that matters most. It merges into the job's
scratch dict, which the next step receives — so step N genuinely consumes what
step N-1 produced. FLINT's planner (`agent/planner.py`) forbids exactly this
("NEVER reference previous step results"), which is why it caps out at five
independent steps and cannot build anything real.

Budgets are not optional. A job is a loop that spends money and attention
until told otherwise, so every one carries a step budget and a wall-clock
expiry, enforced by the kernel rather than trusted to the runner.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

# ── budget defaults ─────────────────────────────────────────────────────────
# Deliberately stingy. A job that dies early is an annoyance; a job that runs
# all month is why the feature gets switched off.
DEFAULT_MAX_STEPS = 40
DEFAULT_TTL_HOURS = 24.0
MIN_INTERVAL = 30.0          # seconds between steps of one job, floor
DEFAULT_INTERVAL = 300.0


class JobState:
    """Where a job is in its life. Plain strings — they land in SQLite and in
    the console as-is, and an unknown value from an older database must never
    crash the scheduler the way a strict enum would."""

    PENDING = "pending"      # created, never run
    WAITING = "waiting"      # ran at least one step, sleeping until next_run_at
    RUNNING = "running"      # a step is in flight right now
    HELD = "held"            # finished with something to say, not yet delivered
    DONE = "done"            # finished (and delivered, if it had a voice)
    FAILED = "failed"
    CANCELLED = "cancelled"

    #: States the scheduler may pick up and run.
    RUNNABLE = (PENDING, WAITING)
    #: States that will never change again without a new request.
    TERMINAL = (DONE, FAILED, CANCELLED)


# ── step outcomes ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Continue:
    """Not done yet. Merge `scratch`, note `note`, run again in `sleep`s.

    `sleep=None` means "use the job's own interval", which is the normal case;
    a runner only overrides it when it learned something about timing (a rate
    limit, a build that will obviously take ten minutes).
    """

    note: str = ""
    scratch: dict[str, Any] = field(default_factory=dict)
    sleep: float | None = None


@dataclass(frozen=True)
class Finish:
    """Done. `result` is the durable answer; `say` is the spoken version.

    A blank `say` means the job finishes silently — it goes straight to DONE
    without ever interrupting the user. That is the right default for work
    nobody asked to be told about (a nightly cleanup); anything the user
    explicitly delegated should set it.
    """

    result: str
    say: str = ""
    urgent: bool = False


@dataclass(frozen=True)
class Fail:
    """This step went wrong. `retry=True` for transient trouble (a flaky
    search, a rate limit) — the kernel will try again on the next tick and
    only give up when the step budget runs out."""

    error: str
    retry: bool = False


Outcome = Continue | Finish | Fail


@dataclass(frozen=True)
class Job:
    """One unit of delegated work, as it exists on disk.

    Frozen on purpose: a runner receives a snapshot and cannot quietly mutate
    shared state. All changes go through JobStore, which is the only thing
    that knows how to persist them.
    """

    id: str
    type: str
    goal: str
    params: dict[str, Any] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)
    state: str = JobState.PENDING
    created: float = 0.0
    updated: float = 0.0
    next_run_at: float = 0.0
    interval: float = DEFAULT_INTERVAL
    steps_done: int = 0
    max_steps: int = DEFAULT_MAX_STEPS
    expires_at: float = 0.0
    urgent: bool = False
    origin: str = ""          # which device/agent asked for this
    result: str = ""
    say: str = ""
    error: str = ""

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def create(cls, type: str, goal: str, *, params: dict[str, Any] | None = None,
               interval: float | None = None, max_steps: int | None = None,
               ttl_hours: float | None = None, urgent: bool = False,
               origin: str = "", now: float | None = None) -> Job:
        """Build a fresh job with its budgets clamped to sane values."""
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a job needs a goal")
        if not (type or "").strip():
            raise ValueError("a job needs a type")
        now = time.time() if now is None else now
        ttl = float(ttl_hours if ttl_hours is not None else DEFAULT_TTL_HOURS)
        return cls(
            id=uuid.uuid4().hex[:8],
            type=type.strip(),
            goal=goal,
            params=dict(params or {}),
            state=JobState.PENDING,
            created=now,
            updated=now,
            next_run_at=now,     # first step runs at the next tick
            interval=max(MIN_INTERVAL,
                         float(interval if interval is not None else DEFAULT_INTERVAL)),
            max_steps=max(1, int(max_steps if max_steps is not None else DEFAULT_MAX_STEPS)),
            expires_at=now + max(0.0, ttl) * 3600,
            urgent=bool(urgent),
            origin=origin,
        )

    # ── budget ──────────────────────────────────────────────────────────────
    def out_of_budget(self, now: float) -> str:
        """Why this job must stop, or "" if it may keep going.

        Held results are exempt: the work is already paid for and the user is
        owed the answer, so an expiry must not swallow it.
        """
        if self.state == JobState.HELD:
            return ""
        if self.steps_done >= self.max_steps:
            return f"used its whole budget of {self.max_steps} steps"
        if self.expires_at and now >= self.expires_at:
            return "ran out of time"
        return ""

    def is_due(self, now: float) -> bool:
        return self.state in JobState.RUNNABLE and now >= self.next_run_at

    # ── serialisation ───────────────────────────────────────────────────────
    def with_scratch(self, update: dict[str, Any]) -> Job:
        merged = dict(self.scratch)
        merged.update(update or {})
        return replace(self, scratch=merged)

    def to_row(self) -> dict[str, Any]:
        row = self.__dict__.copy()
        row["params"] = json.dumps(self.params, ensure_ascii=False)
        row["scratch"] = json.dumps(self.scratch, ensure_ascii=False)
        row["urgent"] = int(self.urgent)
        return row

    @classmethod
    def from_row(cls, row) -> Job:
        data = dict(row)
        data["params"] = _loads(data.get("params"), {})
        data["scratch"] = _loads(data.get("scratch"), {})
        data["urgent"] = bool(data.get("urgent"))
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})

    # ── display ─────────────────────────────────────────────────────────────
    def describe(self) -> str:
        """One short line, for a spoken status or a console row."""
        bits = [self.goal]
        if self.state == JobState.HELD:
            bits.append("(done, waiting to tell you)")
        elif self.state == JobState.FAILED:
            bits.append(f"(failed: {self.error})" if self.error else "(failed)")
        elif self.state in JobState.TERMINAL:
            bits.append(f"({self.state})")
        elif self.steps_done:
            bits.append(f"(step {self.steps_done} of {self.max_steps})")
        return " ".join(bits)


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default
