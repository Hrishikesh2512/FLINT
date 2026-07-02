"""Brain resolver — decides where Venom's intelligence lives right now.

Policy (the core product rule: the Pi never computes, it orchestrates):

    1. Probe the current brain first (stickiness). A healthy brain is kept,
       so a momentarily slow Wi-Fi doesn't flap the wearable between
       laptop and cloud mid-conversation.
    2. If the current brain is unhealthy (or none is held), probe candidates
       in priority order — laptop entries are configured with the lowest
       priority numbers so they always win over cloud endpoints.
    3. If nothing answers, the brain is None: offline mode.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from venom.config import BrainCandidate
from venom.monitors.network import probe_tcp

Prober = Callable[[str, int, float], Awaitable[bool]]


@dataclass(frozen=True)
class Resolution:
    brain: BrainCandidate | None
    switched: bool  # True when this call changed the active brain

    @property
    def online(self) -> bool:
        return self.brain is not None


class BrainResolver:
    def __init__(
        self,
        candidates: tuple[BrainCandidate, ...],
        probe_timeout: float = 3.0,
        prober: Prober = probe_tcp,
    ):
        if not candidates:
            raise ValueError("BrainResolver needs at least one candidate")
        self._candidates = tuple(sorted(candidates, key=lambda c: c.priority))
        self._timeout = probe_timeout
        self._probe = prober
        self._current: BrainCandidate | None = None

    @property
    def current(self) -> BrainCandidate | None:
        return self._current

    async def resolve(self) -> Resolution:
        previous = self._current

        # Stickiness: keep a healthy current brain, but let a configured
        # higher-priority candidate (the laptop coming back online) take over.
        if self._current is not None:
            better = [c for c in self._candidates if c.priority < self._current.priority]
            for candidate in better:
                if await self._probe(candidate.host, candidate.port, self._timeout):
                    self._current = candidate
                    return Resolution(candidate, switched=True)
            if await self._probe(self._current.host, self._current.port, self._timeout):
                return Resolution(self._current, switched=False)

        for candidate in self._candidates:
            if candidate == previous:
                continue  # already probed above
            if await self._probe(candidate.host, candidate.port, self._timeout):
                self._current = candidate
                return Resolution(candidate, switched=candidate != previous)

        self._current = None
        return Resolution(None, switched=previous is not None)
