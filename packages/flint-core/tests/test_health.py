"""Self-diagnostics: detect, repair once, and never hide that it happened."""

from __future__ import annotations

import pytest

from flint_core.health import (
    FAIL,
    OK,
    WARN,
    Check,
    HealthMonitor,
    disk_check,
    fail,
    job_backlog_check,
    ok,
    store_check,
    warn,
)


def check(name="thing", results=None, repair=None, critical=False):
    """A check returning each queued result in turn, then repeating the last."""
    queue = list(results or [ok()])

    def run():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return Check(name=name, summary=f"The {name}.", run=run, repair=repair,
                 critical=critical)


# ── reporting ───────────────────────────────────────────────────────────────
def test_all_clear():
    report = HealthMonitor([check(results=[ok("fine")])]).run()
    assert report.healthy is True
    assert report.spoken() == "Everything's fine — all checks passing."


def test_the_worst_status_wins():
    monitor = HealthMonitor([check("a", [ok()]), check("b", [warn("hmm")]),
                             check("c", [fail("broken")])])
    assert monitor.run().status == FAIL


def test_a_warning_alone_is_not_a_failure():
    assert HealthMonitor([check(results=[warn("disk 88% full")])]).run().status == WARN


def test_problems_are_listed_with_their_detail():
    report = HealthMonitor([check("disk", [fail("disk 99% full")])]).run()
    assert report.problems() == ["disk: disk 99% full"]
    assert "disk 99% full" in report.spoken()


def test_a_check_that_throws_is_a_failed_check_not_a_crash():
    """The monitor must outlive the things it monitors."""
    def explode():
        raise RuntimeError("the probe itself is broken")

    report = HealthMonitor([Check(name="x", summary="s", run=explode)]).run()
    assert report.status == FAIL
    assert "the check itself failed" in report.results["x"].detail


def test_a_check_returning_nonsense_is_a_failure():
    report = HealthMonitor([Check(name="x", summary="s",
                                  run=lambda: "fine, thanks")]).run()
    assert report.results["x"].status == FAIL


def test_duplicate_checks_are_rejected():
    monitor = HealthMonitor([check("disk")])
    with pytest.raises(ValueError, match="duplicate check"):
        monitor.add(check("disk"))


# ── repair ──────────────────────────────────────────────────────────────────
def test_a_successful_repair_is_verified_by_rerunning_the_check():
    """Believing the repair worked is not knowing it did."""
    attempts = []
    monitor = HealthMonitor([check("svc", [fail("down"), ok("back up")],
                                   repair=lambda: attempts.append(1) or True)])
    report = monitor.run()
    assert attempts == [1]
    assert report.repaired == ("svc",)


def test_a_repaired_fault_is_still_reported():
    """A device that silently patches itself all week is heading somewhere worse."""
    monitor = HealthMonitor([check("svc", [fail("down"), ok()],
                                   repair=lambda: True)])
    report = monitor.run()
    assert report.healthy is False              # not swept under the rug
    assert "fixed automatically" in report.results["svc"].detail
    assert "sorted it out" in report.spoken()


def test_a_repair_that_did_not_actually_work_is_not_claimed():
    monitor = HealthMonitor([check("svc", [fail("still down")],
                                   repair=lambda: True)])
    report = monitor.run()
    assert report.repaired == ()
    assert report.unrepaired == ("svc",)
    assert report.results["svc"].status == FAIL


def test_a_repair_that_throws_is_survivable():
    def broken_repair():
        raise OSError("cannot restart it")

    report = HealthMonitor([check("svc", [fail("down")],
                                  repair=broken_repair)]).run()
    assert report.results["svc"].status == FAIL


def test_repairs_are_bounded_not_retried_forever():
    """Something needing repair every cycle is broken in a way repair can't reach."""
    attempts = []
    monitor = HealthMonitor([check("svc", [fail("down")],
                                   repair=lambda: attempts.append(1) or True)])
    for _ in range(6):
        monitor.run()
    assert len(attempts) == HealthMonitor.MAX_REPAIRS


def test_recovery_resets_the_repair_budget():
    attempts = []
    monitor = HealthMonitor([check("svc", [fail("down"), ok(), fail("down again"), ok()],
                                   repair=lambda: attempts.append(1) or True)])
    monitor.run()                 # fails, repairs, verifies ok
    monitor.run()                 # healthy
    monitor.run()                 # fails again — budget was reset
    assert len(attempts) == 2


def test_a_check_with_no_repair_is_simply_reported():
    report = HealthMonitor([check("mic", [fail("no audio frames")])]).run()
    assert report.results["mic"].status == FAIL
    assert report.repaired == () and report.unrepaired == ()


# ── criticality ─────────────────────────────────────────────────────────────
def test_critical_failures_are_singled_out():
    monitor = HealthMonitor([check("disk", [fail("full")], critical=True),
                             check("tv", [fail("unreachable")])])
    report = monitor.run()
    assert monitor.critical_failures(report) == ["disk"]


def test_a_critical_check_that_only_warns_is_not_a_critical_failure():
    monitor = HealthMonitor([check("disk", [warn("85% full")], critical=True)])
    assert monitor.critical_failures(monitor.run()) == []


# ── the ready-made checks ───────────────────────────────────────────────────
def test_the_disk_check_reads_real_usage(tmp_path):
    result = disk_check(tmp_path).run()
    assert result.status in (OK, WARN, FAIL)
    assert "% full" in result.detail


def test_the_disk_check_warns_and_fails_at_its_thresholds(tmp_path):
    assert disk_check(tmp_path, warn_pct=0, fail_pct=101).run().status == WARN
    assert disk_check(tmp_path, warn_pct=0, fail_pct=0).run().status == FAIL


def test_the_disk_check_survives_a_missing_path():
    assert disk_check("/no/such/place/at/all").run().status == FAIL


def test_a_store_check_catches_corruption():
    def unreadable():
        raise ValueError("expecting value: line 1 column 1")

    assert store_check("memory", unreadable).run().status == FAIL
    assert store_check("memory", lambda: {"fine": True}).run().status == OK


def test_the_backlog_check_notices_jobs_piling_up():
    class Piled:
        def active(self):
            return list(range(25))

    result = job_backlog_check(Piled(), limit=10).run()
    assert result.status == WARN
    assert "may be stuck" in result.detail


def test_the_backlog_check_survives_a_broken_scheduler():
    class Broken:
        def active(self):
            raise RuntimeError("the store is gone")

    assert job_backlog_check(Broken()).run().status == FAIL
