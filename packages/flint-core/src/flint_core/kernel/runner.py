"""Runners — the pluggable "what this kind of job actually does" half.

The kernel owns scheduling, persistence, budgets and delivery. It knows
nothing about searching the web, driving a coding agent, or watching a cricket
score. Those live in runners, registered by job type, and the split is what
makes a new autonomous capability a small self-contained file rather than a
change to the scheduler.

Deliberately shaped like `flint_core.tools.ToolRegistry`: one decoration
declares the type, its limits and its model-facing description, and the
registry is the single source of truth for all three.

    runners = RunnerRegistry()

    @runners.runner("research", description="Read around a topic and report back.",
                    default_interval=60.0, max_steps=8, max_concurrent=2)
    def run_research(ctx: JobContext) -> Outcome:
        ...

A runner does ONE step and returns. It must not loop, sleep, or wait for the
next interval itself — that is the kernel's job, and a runner that blocks for
an hour is a runner that cannot be cancelled, budgeted, or restarted. Runners
may block for the length of one step (an HTTP call, a subprocess); the
scheduler runs them off the event loop so that is safe.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from flint_core.kernel.job import (
    DEFAULT_INTERVAL,
    DEFAULT_MAX_STEPS,
    DEFAULT_TTL_HOURS,
    Job,
    Outcome,
)

log = logging.getLogger("flint.kernel")


class UnknownJobTypeError(Exception):
    pass


@dataclass
class JobContext:
    """Everything one step is allowed to see.

    `services` is how a platform-free kernel hands a runner platform things —
    an LLM provider, a tool registry, a shell. The kernel never inspects it;
    the host builds it once and every runner picks out what it needs.
    """

    job: Job
    log: Callable[[str], None]
    services: dict[str, Any] = field(default_factory=dict)

    @property
    def goal(self) -> str:
        return self.job.goal

    @property
    def params(self) -> dict[str, Any]:
        return self.job.params

    @property
    def scratch(self) -> dict[str, Any]:
        """What previous steps of this same job left behind."""
        return self.job.scratch

    @property
    def step(self) -> int:
        """1 for the first step, 2 for the second..."""
        return self.job.steps_done + 1

    def service(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    def require(self, name: str) -> Any:
        """A service the runner cannot work without."""
        try:
            return self.services[name]
        except KeyError:
            raise LookupError(
                f"job type {self.job.type!r} needs the {name!r} service, "
                f"which this host did not provide"
            ) from None


@dataclass(frozen=True)
class RunnerSpec:
    type: str
    description: str
    handler: Callable[[JobContext], Outcome]
    default_interval: float = DEFAULT_INTERVAL
    max_steps: int = DEFAULT_MAX_STEPS
    ttl_hours: float = DEFAULT_TTL_HOURS
    #: How many jobs of this type may exist at once. Each one costs API calls
    #: or a subprocess, so the ceiling is per-type rather than global.
    max_concurrent: int = 3

    def __post_init__(self) -> None:
        if not self.type.isidentifier():
            raise ValueError(f"job type must be an identifier: {self.type!r}")
        if not self.description.strip():
            raise ValueError(f"job type {self.type!r} needs a description")
        if self.max_concurrent < 1:
            raise ValueError(f"job type {self.type!r}: max_concurrent must be >= 1")


class RunnerRegistry:
    def __init__(self) -> None:
        self._runners: dict[str, RunnerSpec] = {}

    # ── registration ────────────────────────────────────────────────────────
    def register(self, spec: RunnerSpec) -> None:
        if spec.type in self._runners:
            raise ValueError(f"duplicate job type: {spec.type}")
        self._runners[spec.type] = spec

    def runner(self, type: str, *, description: str,
               default_interval: float = DEFAULT_INTERVAL,
               max_steps: int = DEFAULT_MAX_STEPS,
               ttl_hours: float = DEFAULT_TTL_HOURS,
               max_concurrent: int = 3) -> Callable:
        def decorator(func: Callable[[JobContext], Outcome]):
            self.register(RunnerSpec(
                type=type, description=description.strip(), handler=func,
                default_interval=default_interval, max_steps=max_steps,
                ttl_hours=ttl_hours, max_concurrent=max_concurrent,
            ))
            return func

        return decorator

    # ── lookup ──────────────────────────────────────────────────────────────
    def __contains__(self, type: str) -> bool:
        return type in self._runners

    def __iter__(self) -> Iterator[RunnerSpec]:
        return iter(self._runners.values())

    def __len__(self) -> int:
        return len(self._runners)

    def get(self, type: str) -> RunnerSpec:
        try:
            return self._runners[type]
        except KeyError:
            raise UnknownJobTypeError(
                f"unknown job type {type!r} — known types: "
                f"{', '.join(sorted(self._runners)) or '(none)'}"
            ) from None

    def types(self) -> list[str]:
        return sorted(self._runners)

    # ── job construction ────────────────────────────────────────────────────
    def build(self, type: str, goal: str, **overrides: Any) -> Job:
        """A Job of `type` with the runner's limits as defaults.

        Callers may pass tighter values; the runner's registered limits are
        the ceiling for steps and lifetime, because those bound the bill and a
        caller (often a language model) should not be able to raise them.
        """
        spec = self.get(type)
        interval = overrides.pop("interval", None)
        max_steps = overrides.pop("max_steps", None)
        ttl_hours = overrides.pop("ttl_hours", None)
        return Job.create(
            type=spec.type,
            goal=goal,
            interval=spec.default_interval if interval is None else float(interval),
            max_steps=(spec.max_steps if max_steps is None
                       else min(spec.max_steps, int(max_steps))),
            ttl_hours=(spec.ttl_hours if ttl_hours is None
                       else min(spec.ttl_hours, float(ttl_hours))),
            **overrides,
        )

    # ── model-facing docs ───────────────────────────────────────────────────
    def documentation(self) -> str:
        """The job-type list for a planner prompt — generated, never written."""
        return "\n".join(
            f"{spec.type} — {spec.description} "
            f"(up to {spec.max_steps} steps over {spec.ttl_hours:g}h)"
            for spec in sorted(self._runners.values(), key=lambda s: s.type)
        )
