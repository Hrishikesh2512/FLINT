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
        fail_threshold: int = 1,
        success_threshold: int = 1,
    ):
        if not candidates:
            raise ValueError("BrainResolver needs at least one candidate")
        self._candidates = tuple(sorted(candidates, key=lambda c: c.priority))
        self._timeout = probe_timeout
        self._probe = prober
        self._current: BrainCandidate | None = None
        # FIXED (Fix 2): hysteresis so a single latency blip can't flap the
        # brain. fail_threshold = consecutive failed probes of the held brain
        # before we abandon it; success_threshold = consecutive successes a
        # higher-priority candidate needs before it pre-empts. Defaults of 1/1
        # preserve the original immediate-switch behaviour (and the existing
        # tests); the supervisor wires in 3/2 for production.
        self._fail_threshold = max(1, fail_threshold)
        self._success_threshold = max(1, success_threshold)
        self._fail_streak = 0
        self._up_streak: dict[str, int] = {}

    @property
    def current(self) -> BrainCandidate | None:
        return self._current

    def _switch_to(self, candidate: BrainCandidate | None) -> None:
        self._current = candidate
        self._fail_streak = 0
        self._up_streak.clear()

    async def resolve(self) -> Resolution:
        previous = self._current

        # Stickiness: keep a healthy current brain, but let a configured
        # higher-priority candidate (the laptop coming back online) take over.
        if self._current is not None:
            better = [c for c in self._candidates if c.priority < self._current.priority]
            for candidate in better:
                if await self._probe(candidate.host, candidate.port, self._timeout):
                    # FIXED (Fix 2): only pre-empt after N consecutive successes
                    # so one lucky probe doesn't yank a working brain.
                    self._up_streak[candidate.name] = self._up_streak.get(candidate.name, 0) + 1
                    if self._up_streak[candidate.name] >= self._success_threshold:
                        self._switch_to(candidate)
                        return Resolution(candidate, switched=True)
                else:
                    self._up_streak[candidate.name] = 0
            if await self._probe(self._current.host, self._current.port, self._timeout):
                self._fail_streak = 0
                return Resolution(self._current, switched=False)
            # FIXED (Fix 2): a failed probe of the held brain is tolerated up to
            # fail_threshold times in a row before we fall back — an India->US
            # blip no longer switches us off a perfectly working Gemini session.
            self._fail_streak += 1
            if self._fail_streak < self._fail_threshold:
                return Resolution(self._current, switched=False)
            self._fail_streak = 0
            self._current = None  # genuinely unhealthy — re-resolve below

        for candidate in self._candidates:
            if candidate == previous:
                continue  # already probed above
            if await self._probe(candidate.host, candidate.port, self._timeout):
                self._switch_to(candidate)
                return Resolution(candidate, switched=candidate != previous)

        self._switch_to(None)
        return Resolution(None, switched=previous is not None)
