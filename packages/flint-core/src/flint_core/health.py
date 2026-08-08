"""Noticing something is wrong, and fixing what can be fixed.

Venom is a headless box on someone's body. When it breaks there is no screen,
no log tailing, and usually no one who knows it broke — the failure mode is
not a crash but a slow slide into uselessness: the disk fills, a store gets
corrupted, jobs pile up behind something wedged, the mic goes deaf. All of
those are silent, and all of them are detectable.

`supervisor.py` already watches the three things it was built for (internet,
headset, brain). This generalises that into checks anything can register, and
adds the half that was missing: some checks know how to repair themselves.

The discipline that keeps self-repair from being worse than the fault:

  * **A repair runs once per detection, not on a loop.** Something that has
    to be fixed every minute is not fixed; escalating beats retrying.
  * **A check that fails is reported even when its repair succeeded.** A
    device that silently patches itself all week is a device on its way to a
    failure nobody saw coming.
  * **A check that throws is a failed check, never a crashed monitor.** The
    watchdog must outlive the thing it watches.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

log = logging.getLogger("flint.health")

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: Rank for "how bad is the worst thing right now".
SEVERITY = {OK: 0, WARN: 1, FAIL: 2}


@dataclass(frozen=True)
class CheckResult:
    status: str
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.status == OK


def ok(detail: str = "") -> CheckResult:
    return CheckResult(OK, detail)


def warn(detail: str) -> CheckResult:
    return CheckResult(WARN, detail)


def fail(detail: str) -> CheckResult:
    return CheckResult(FAIL, detail)


@dataclass(frozen=True)
class Check:
    """One thing that can be wrong, and optionally how to put it right."""

    name: str
    summary: str
    run: Callable[[], CheckResult]
    #: Called when the check fails. Returns True if it believes it fixed it —
    #: the check is then re-run to find out whether that was true.
    repair: Callable[[], bool] | None = None
    #: A failure here means the device is not doing its job at all.
    critical: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a check needs a name")


@dataclass
class _State:
    consecutive_failures: int = 0
    repairs_attempted: int = 0
    repaired_at: float = 0.0
    last_status: str = OK


@dataclass(frozen=True)
class HealthReport:
    results: dict[str, CheckResult] = field(default_factory=dict)
    repaired: tuple[str, ...] = ()
    unrepaired: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if not self.results:
            return OK
        return max(self.results.values(),
                   key=lambda r: SEVERITY.get(r.status, 0)).status

    @property
    def healthy(self) -> bool:
        return self.status == OK

    def problems(self) -> list[str]:
        return [f"{name}: {result.detail or result.status}"
                for name, result in sorted(self.results.items())
                if not result.healthy]

    def spoken(self) -> str:
        """One line for "are you okay?" — the question people actually ask."""
        problems = self.problems()
        if not problems:
            return "Everything's fine — all checks passing."
        if self.repaired:
            fixed = ", ".join(self.repaired)
            if not self.unrepaired:
                return (f"I had a problem with {fixed} but I've sorted it "
                        f"out — worth knowing it happened.")
        worst = "; ".join(problems[:3])
        return f"Not quite — {worst}."


class HealthMonitor:
    """Runs checks, attempts bounded repairs, and reports honestly."""

    #: Repair attempts for one continuous failure before giving up on it.
    #: Something needing repair every cycle is broken in a way repair cannot
    #: reach, and retrying forever hides that.
    MAX_REPAIRS = 2

    def __init__(self, checks: Iterable[Check] = (),
                 clock: Callable[[], float] = time.time):
        self._checks: list[Check] = list(checks)
        self._clock = clock
        self._state: dict[str, _State] = {}

    def add(self, check: Check) -> Check:
        if any(c.name == check.name for c in self._checks):
            raise ValueError(f"duplicate check: {check.name}")
        self._checks.append(check)
        return check

    def __len__(self) -> int:
        return len(self._checks)

    def __iter__(self) -> Iterator[Check]:
        return iter(self._checks)

    def _state_for(self, name: str) -> _State:
        return self._state.setdefault(name, _State())

    def _run_one(self, check: Check) -> CheckResult:
        try:
            result = check.run()
        except Exception as exc:            # noqa: BLE001
            # A check that throws is a failed check. The monitor must outlive
            # the things it monitors, or the first broken probe blinds it.
            log.warning("health: check %s raised: %s", check.name, exc)
            return fail(f"the check itself failed: {exc}")
        return result if isinstance(result, CheckResult) else fail("bad check result")

    def run(self) -> HealthReport:
        results: dict[str, CheckResult] = {}
        repaired: list[str] = []
        unrepaired: list[str] = []

        for check in self._checks:
            result = self._run_one(check)
            state = self._state_for(check.name)

            if result.healthy:
                if state.consecutive_failures:
                    log.info("health: %s recovered", check.name)
                state.consecutive_failures = 0
                state.repairs_attempted = 0
                state.last_status = result.status
                results[check.name] = result
                continue

            state.consecutive_failures += 1
            log.warning("health: %s %s — %s", check.name, result.status, result.detail)

            if check.repair is not None and state.repairs_attempted < self.MAX_REPAIRS:
                state.repairs_attempted += 1
                log.info("health: attempting repair of %s (%d/%d)",
                         check.name, state.repairs_attempted, self.MAX_REPAIRS)
                try:
                    attempted = bool(check.repair())
                except Exception as exc:    # noqa: BLE001
                    log.warning("health: repair of %s raised: %s", check.name, exc)
                    attempted = False
                if attempted:
                    # Believing the repair worked is not knowing it did.
                    after = self._run_one(check)
                    if after.healthy:
                        state.repaired_at = self._clock()
                        state.consecutive_failures = 0
                        repaired.append(check.name)
                        # Reported anyway: a device that silently patches
                        # itself all week is heading somewhere worse.
                        results[check.name] = warn(
                            f"{result.detail} — fixed automatically")
                        continue
                unrepaired.append(check.name)

            results[check.name] = result
            state.last_status = result.status

        return HealthReport(results=results, repaired=tuple(repaired),
                            unrepaired=tuple(unrepaired))

    def critical_failures(self, report: HealthReport) -> list[str]:
        """Failing checks that mean the device is not doing its job."""
        critical = {c.name for c in self._checks if c.critical}
        return sorted(name for name, result in report.results.items()
                      if name in critical and result.status == FAIL)


# ── checks anything can reuse ────────────────────────────────────────────────
def disk_check(path, warn_pct: float = 85.0, fail_pct: float = 95.0) -> Check:
    """Free space. The classic silent death of an always-on appliance."""
    def run() -> CheckResult:
        import shutil

        try:
            usage = shutil.disk_usage(str(path))
        except OSError as exc:
            return fail(f"can't read disk usage: {exc}")
        used = 100.0 * usage.used / usage.total if usage.total else 0.0
        detail = f"disk {used:.0f}% full"
        if used >= fail_pct:
            return fail(detail)
        return warn(detail) if used >= warn_pct else ok(detail)

    return Check(name="disk", summary="Free space on the state volume.",
                 run=run, critical=True)


def store_check(name: str, load: Callable[[], object]) -> Check:
    """A store that can still be read. Catches corruption after a power cut."""
    def run() -> CheckResult:
        try:
            load()
        except Exception as exc:            # noqa: BLE001
            return fail(f"{name} is unreadable: {exc}")
        return ok(f"{name} reads fine")

    return Check(name=f"store:{name}", summary=f"The {name} store is readable.",
                 run=run)


def job_backlog_check(scheduler, limit: int = 10) -> Check:
    """Jobs piling up — the symptom of one wedged runner starving the rest."""
    def run() -> CheckResult:
        try:
            active = len(scheduler.active())
        except Exception as exc:            # noqa: BLE001
            return fail(f"can't read the job queue: {exc}")
        detail = f"{active} job(s) in flight"
        return warn(f"{detail} — something may be stuck") if active > limit else ok(detail)

    return Check(name="jobs", summary="Background work isn't piling up.", run=run)
