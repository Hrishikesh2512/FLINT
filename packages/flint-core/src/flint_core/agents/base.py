"""Agents — things you hand a goal to, that are not this process.

Venom already delegates: `laptop_task` sends a sentence to FLINT over a
websocket and waits for one string back, capped at 600 characters and read
aloud. That is an RPC, and it is the reason "multi-agent collaboration" has
never been more than a phrase here. An RPC cannot:

  * report progress while it works (a coding task takes minutes; the caller
    sits blind and the user hears nothing)
  * say what it actually *did* — which files changed, whether tests passed —
    only prose about it
  * be chosen from among several agents on the merits of the task
  * be cancelled, budgeted, or resumed

An Agent fixes those four things and nothing else. It is deliberately the
same shape as the rest of this codebase: a spec with a name, a summary, what
it is good at, and whether it is here — like Capability and RunnerSpec.

    request  ->  goal + working directory + context, and a progress callback
    result   ->  ok/failed, a speakable summary, full detail, artifacts

The transport is not part of this. A local subprocess (a coding CLI), a
websocket to another machine, or an in-process function all implement the
same protocol, so the caller never learns which it got.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("flint.agents")

#: What a spoken summary may run to. The full text lives in `detail`; this is
#: what gets read into someone's ear, so it is short on purpose.
MAX_SPOKEN = 600


@dataclass(frozen=True)
class AgentRequest:
    """One task handed to an agent."""

    goal: str
    #: Where the work happens — a repo path for a coding agent. Empty means
    #: the agent's own default, which for a CLI is wherever it was launched.
    cwd: str = ""
    #: Anything the agent should know that isn't in the goal: the calling
    #: conversation, prior findings, the user's name. Agents ignore keys they
    #: don't understand.
    context: Mapping[str, Any] = field(default_factory=dict)
    #: Called with each line of progress as it happens. The whole point of
    #: not being an RPC — a five-minute task can say what it is doing.
    on_progress: Callable[[str], None] | None = None
    timeout: float = 600.0

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("an agent request needs a goal")

    def progress(self, line: str) -> None:
        """Report a step. Never raises — a broken listener must not kill work."""
        if self.on_progress is None:
            return
        line = " ".join(str(line or "").split())
        if not line:
            return
        try:
            self.on_progress(line)
        except Exception:            # noqa: BLE001
            log.debug("agent progress callback failed", exc_info=True)


@dataclass(frozen=True)
class AgentResult:
    """What came back. Structured, so a caller can act on it, not just say it."""

    ok: bool
    #: Short enough to read aloud. Always populated, success or failure.
    summary: str
    #: Everything the agent produced — the transcript, the diff, the log.
    detail: str = ""
    #: Files the agent says it created or changed.
    artifacts: tuple[str, ...] = ()
    error: str = ""
    agent: str = ""
    #: Set when an agent needs the user before it can continue. The caller is
    #: the one with a microphone; the agent is not. Without this a blocked
    #: agent can only fail, which is why one-shot delegation gives up on
    #: anything ambiguous.
    question: str = ""

    @property
    def needs_input(self) -> bool:
        return bool(self.question.strip())

    def spoken(self) -> str:
        """The summary, trimmed to something sayable."""
        text = " ".join((self.question or self.summary or "").split())
        return text[:MAX_SPOKEN] + (" …" if len(text) > MAX_SPOKEN else "")

    @classmethod
    def failed(cls, error: str, *, agent: str = "", detail: str = "") -> AgentResult:
        return cls(ok=False, summary=f"That didn't work — {error}", error=error,
                   agent=agent, detail=detail)


class Agent(Protocol):
    """Anything that can be handed a goal."""

    name: str

    def run(self, request: AgentRequest) -> AgentResult:
        ...


@dataclass(frozen=True)
class AgentSpec:
    """A registered agent: who it is, what it's for, and whether it's here."""

    name: str
    summary: str
    run: Callable[[AgentRequest], AgentResult]
    #: Task classes from flint_core.llm.routing.Task. Selection uses these,
    #: so the same vocabulary picks a model and picks an agent.
    good_at: frozenset[str] = field(default_factory=frozenset)
    available: bool = True
    permissions: tuple[str, ...] = ()
    #: Higher wins when two agents suit a task equally well.
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("an agent needs a name")
        if not self.summary.strip():
            raise ValueError(f"agent {self.name!r} needs a summary")

    def suits(self, task: str) -> bool:
        return not self.good_at or task in self.good_at
