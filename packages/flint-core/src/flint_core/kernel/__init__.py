"""flint-core kernel — work that outlives the conversation that asked for it.

A tool call is a question answered inside a session: it runs, returns a
string, and disappears when the session does. A *job* is the other shape —
handed a goal, it runs on its own schedule, survives a reboot, keeps what it
learned between steps, and comes back when it has something worth saying.

    store     JobStore     durable state (SQLite) + crash recovery
    runners   RunnerRegistry   pluggable "what this job type does"
    scheduler Scheduler    the loop: deliver, expire, run

Platform-free like the rest of flint-core: the kernel never searches the web,
opens a shell, or speaks. Hosts inject those through `Scheduler(services=...)`
and hand it a `deliver` callback for anything a finished job wants said.
"""

from flint_core.kernel.job import (
    DEFAULT_INTERVAL,
    DEFAULT_MAX_STEPS,
    DEFAULT_TTL_HOURS,
    MIN_INTERVAL,
    Continue,
    Fail,
    Finish,
    Job,
    JobState,
    Outcome,
)
from flint_core.kernel.runner import (
    JobContext,
    RunnerRegistry,
    RunnerSpec,
    UnknownJobTypeError,
)
from flint_core.kernel.scheduler import Scheduler
from flint_core.kernel.store import JobStore

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_TTL_HOURS",
    "MIN_INTERVAL",
    "Continue",
    "Fail",
    "Finish",
    "Job",
    "JobContext",
    "JobState",
    "JobStore",
    "Outcome",
    "RunnerRegistry",
    "RunnerSpec",
    "Scheduler",
    "UnknownJobTypeError",
]
