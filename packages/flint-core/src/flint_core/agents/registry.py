"""Which agent gets the job.

Same selection vocabulary as model routing (`flint_core.llm.routing.Task`), on
purpose: "this is a coding task" should pick both the right model and the right
agent, and having two vocabularies for one question guarantees they drift.

Selection is ordering, not filtering — exactly like the model router. An agent
that suits the task poorly still sorts last rather than vanishing, so a caller
walking the list always has somewhere to fall when the best one is missing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

from flint_core.agents.base import AgentRequest, AgentResult, AgentSpec

log = logging.getLogger("flint.agents")


class NoAgentAvailableError(Exception):
    pass


class AgentRegistry:
    def __init__(self, agents: Iterable[AgentSpec] = (), on_decision=None):
        # Called with (question, chosen, reason, alternatives) each time an
        # agent is picked, so "why did you use Claude for that?" can be
        # answered from what happened rather than reconstructed afterwards.
        self._on_decision = on_decision
        self._agents: list[AgentSpec] = []
        for agent in agents:
            self.add(agent)

    # ── building ────────────────────────────────────────────────────────────
    def add(self, agent: AgentSpec) -> AgentSpec:
        if any(a.name == agent.name for a in self._agents):
            raise ValueError(f"duplicate agent: {agent.name}")
        self._agents.append(agent)
        return agent

    # ── inspection ──────────────────────────────────────────────────────────
    def __iter__(self) -> Iterator[AgentSpec]:
        return iter(self._agents)

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, name: str) -> bool:
        return any(a.name == name for a in self._agents)

    def get(self, name: str) -> AgentSpec:
        for agent in self._agents:
            if agent.name == name:
                return agent
        raise NoAgentAvailableError(
            f"no agent called {name!r} — have: {', '.join(self.names()) or 'none'}")

    def available(self) -> list[AgentSpec]:
        return [a for a in self._agents if a.available]

    def names(self) -> list[str]:
        return [a.name for a in self.available()]

    # ── selection ───────────────────────────────────────────────────────────
    def candidates(self, task: str) -> list[AgentSpec]:
        """Available agents, best fit for `task` first."""
        def key(agent: AgentSpec) -> tuple:
            if task in agent.good_at:
                fit = 0                  # named for exactly this work
            elif not agent.good_at:
                fit = 1                  # general purpose
            else:
                fit = 2                  # specialised in something else
            return (fit, -agent.priority, agent.name)

        return sorted(self.available(), key=key)

    def pick(self, task: str) -> AgentSpec | None:
        found = self.candidates(task)
        return found[0] if found else None

    # ── running ─────────────────────────────────────────────────────────────
    def run(self, request: AgentRequest, *, task: str = "",
            agent: str = "") -> AgentResult:
        """Hand `request` to the best agent for the job and return its result.

        `agent` names one explicitly; otherwise the task picks. A chosen agent
        that raises is reported as a failed result rather than an exception:
        the caller is usually mid-conversation and needs something to say.
        """
        if agent:
            chosen = self.get(agent)
        else:
            chosen = self.pick(task)
            if chosen is None:
                raise NoAgentAvailableError(
                    "no agent is available to do that right now")
        log.info("agents: %s -> %s", task or "(explicit)", chosen.name)
        self._note_decision(chosen, task, agent)
        try:
            result = chosen.run(request)
        except Exception as exc:         # noqa: BLE001 — a broken agent is a result
            log.warning("agent %s raised: %s", chosen.name, exc)
            return AgentResult.failed(f"{type(exc).__name__}: {exc}",
                                      agent=chosen.name)
        # An agent that forgot to name itself still gets attributed, so the
        # audit trail and the spoken reply agree on who did the work.
        if not result.agent:
            from dataclasses import replace

            result = replace(result, agent=chosen.name)
        return result

    def _note_decision(self, chosen: AgentSpec, task: str, explicit: str) -> None:
        if self._on_decision is None:
            return
        if explicit:
            reason = "you asked for that one by name"
        elif task and task in chosen.good_at:
            reason = f"it's the one built for {task} work"
        elif chosen.good_at:
            reason = "nothing better was available"
        else:
            reason = "it's the general-purpose one"
        others = [a.name for a in self.available() if a.name != chosen.name]
        try:
            self._on_decision("which agent", chosen.name, reason, others)
        except Exception:            # noqa: BLE001 — bookkeeping is not the job
            log.debug("agents: decision callback failed", exc_info=True)

    def describe(self) -> str:
        lines = []
        for agent in sorted(self._agents, key=lambda a: a.name):
            mark = "on " if agent.available else "off"
            good = ", ".join(sorted(agent.good_at)) or "anything"
            lines.append(f"[{mark}] {agent.name} — {agent.summary} ({good})")
        return "\n".join(lines)
