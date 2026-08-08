"""The scheduler: step outcomes, budgets, retries, and delivery discipline.

No network, no sleeping, no real clock — runners here are two-line fakes, and
the whole point is that the kernel's rules hold regardless of what they do.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from flint_core.kernel import (
    Continue,
    Fail,
    Finish,
    JobState,
    JobStore,
    RunnerRegistry,
    Scheduler,
    UnknownJobTypeError,
)


@pytest.fixture()
def store(tmp_path, fake_clock):
    return JobStore(tmp_path / "jobs.db", clock=fake_clock)


def scheduler(store, registry, fake_clock, **kw):
    kw.setdefault("clock", fake_clock)
    return Scheduler(store, registry, **kw)


def tick(sched):
    asyncio.run(sched.tick())


def registry_returning(*outcomes, type="probe", **spec):
    """A registry whose runner returns each outcome in turn, then repeats the last."""
    runners = RunnerRegistry()
    seen: list = []
    queue = list(outcomes)

    @runners.runner(type, description="A test job.", **spec)
    def _run(ctx):
        seen.append(ctx)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    runners.calls = seen          # type: ignore[attr-defined]
    return runners


# ── submitting ──────────────────────────────────────────────────────────────
def test_submit_rejects_an_unknown_job_type(store, fake_clock):
    sched = scheduler(store, RunnerRegistry(), fake_clock)
    with pytest.raises(UnknownJobTypeError):
        sched.submit("nonsense", "do a thing")


def test_submit_enforces_the_per_type_ceiling(store, fake_clock):
    runners = registry_returning(Continue(), max_concurrent=2)
    sched = scheduler(store, runners, fake_clock)
    sched.submit("probe", "one")
    sched.submit("probe", "two")
    with pytest.raises(ValueError, match="that's the limit"):
        sched.submit("probe", "three")


def test_cancelling_frees_a_slot(store, fake_clock):
    runners = registry_returning(Continue(), max_concurrent=1)
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "one")
    sched.cancel(job.id)
    assert sched.submit("probe", "two").goal == "two"


def test_build_clamps_a_callers_budget_to_the_runners_ceiling(store, fake_clock):
    runners = registry_returning(Continue(), max_steps=5, ttl_hours=2)
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "greedy", max_steps=500, ttl_hours=999)
    assert job.max_steps == 5
    assert job.expires_at == pytest.approx(fake_clock.now + 2 * 3600)


def test_build_honours_a_tighter_budget(store, fake_clock):
    runners = registry_returning(Continue(), max_steps=50)
    sched = scheduler(store, runners, fake_clock)
    assert sched.submit("probe", "modest", max_steps=3).max_steps == 3


# ── step outcomes ───────────────────────────────────────────────────────────
def test_continue_carries_scratch_into_the_next_step(store, fake_clock):
    """Step N sees what step N-1 produced — the whole reason jobs exist."""
    runners = RunnerRegistry()
    saw: list[dict] = []

    @runners.runner("probe", description="Counts.", default_interval=60)
    def _run(ctx):
        saw.append(dict(ctx.scratch))
        return Continue(note=f"step {ctx.step}",
                        scratch={"count": ctx.scratch.get("count", 0) + 1})

    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "count up")
    for _ in range(3):
        tick(sched)
        fake_clock.advance(60)

    assert saw == [{}, {"count": 1}, {"count": 2}]
    assert store.get(job.id).scratch == {"count": 3}
    assert store.get(job.id).steps_done == 3


def test_continue_schedules_the_next_step_by_the_jobs_interval(store, fake_clock):
    runners = registry_returning(Continue(), default_interval=120)
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "poll")
    tick(sched)
    assert store.get(job.id).state == JobState.WAITING
    assert store.get(job.id).next_run_at == pytest.approx(fake_clock.now + 120)

    tick(sched)                      # not due yet
    assert store.get(job.id).steps_done == 1


def test_the_default_cadence_is_floored(store, fake_clock):
    """Below MIN_INTERVAL the bill outruns the value — the floor is not advisory."""
    from flint_core.kernel import MIN_INTERVAL

    runners = registry_returning(Continue(), default_interval=1)
    sched = scheduler(store, runners, fake_clock)
    assert sched.submit("probe", "eager").interval == MIN_INTERVAL


def test_a_runner_may_override_the_next_wake(store, fake_clock):
    """A step that knows better than the default cadence — polling a build,
    backing off a rate limit — sets its own next wake, floor included."""
    runners = registry_returning(Continue(sleep=5), default_interval=600)
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "back soon")
    tick(sched)
    assert store.get(job.id).next_run_at == pytest.approx(fake_clock.now + 5)


def test_finish_with_nothing_to_say_completes_silently(store, fake_clock):
    spoken: list = []
    runners = registry_returning(Finish("tidied up"))
    sched = scheduler(store, runners, fake_clock,
                      deliver=lambda job: spoken.append(job) or True)
    job = sched.submit("probe", "cleanup")
    tick(sched)
    assert store.get(job.id).state == JobState.DONE
    assert store.get(job.id).result == "tidied up"
    assert spoken == []


def test_finish_with_something_to_say_is_held_then_delivered(store, fake_clock):
    spoken: list = []
    runners = registry_returning(Finish("India need 18", say="India need 18 off 12"))
    sched = scheduler(store, runners, fake_clock,
                      deliver=lambda job: spoken.append(job.say) or True)
    job = sched.submit("probe", "watch the chase")

    tick(sched)                                   # runs, then delivers
    assert spoken == ["India need 18 off 12"]
    assert store.get(job.id).state == JobState.DONE


def test_a_refused_delivery_keeps_waiting(store, fake_clock):
    """Mid-conversation or quiet hours: the result waits, it does not vanish."""
    allowed = False
    runners = registry_returning(Finish("done", say="all done"))
    sched = scheduler(store, runners, fake_clock, deliver=lambda job: allowed)
    job = sched.submit("probe", "thing")

    tick(sched)
    assert store.get(job.id).state == JobState.HELD
    tick(sched)
    assert store.get(job.id).state == JobState.HELD

    allowed = True
    tick(sched)
    assert store.get(job.id).state == JobState.DONE


def test_only_one_result_is_delivered_per_tick(store, fake_clock):
    spoken: list = []
    runners = registry_returning(Finish("done", say="ready"), max_concurrent=5)
    sched = scheduler(store, runners, fake_clock,
                      deliver=lambda job: spoken.append(job.id) or True)
    sched.submit("probe", "one")
    sched.submit("probe", "two")

    tick(sched)               # both run; at most one speaks
    assert len(spoken) == 1
    tick(sched)               # the other is delivered next time round
    assert len(spoken) == 2


def test_delivery_happens_before_new_work_is_started(store, fake_clock):
    """A result in hand beats spending API calls on another step."""
    runners = registry_returning(Finish("done", say="ready"), max_concurrent=5)
    sched = scheduler(store, runners, fake_clock, deliver=lambda job: True)
    held = sched.submit("probe", "already finished")
    tick(sched)
    assert store.get(held.id).state == JobState.DONE

    # Re-arm: one held result and one job that has never run.
    store.save(replace(store.get(held.id), state=JobState.HELD))
    fresh = sched.submit("probe", "not started")
    tick(sched)
    assert store.get(fresh.id).steps_done == 0     # skipped in favour of delivery
    assert store.get(held.id).state == JobState.DONE


def test_a_delivery_callback_that_raises_does_not_lose_the_result(store, fake_clock):
    def explode(job):
        raise RuntimeError("the mouth is broken")

    runners = registry_returning(Finish("done", say="ready"))
    sched = scheduler(store, runners, fake_clock, deliver=explode)
    job = sched.submit("probe", "thing")
    tick(sched)
    assert store.get(job.id).state == JobState.HELD


# ── failure ─────────────────────────────────────────────────────────────────
def test_a_hard_failure_stops_the_job(store, fake_clock):
    runners = registry_returning(Fail("the API key is wrong"))
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "doomed")
    tick(sched)
    assert store.get(job.id).state == JobState.FAILED
    assert store.get(job.id).error == "the API key is wrong"


def test_a_retryable_failure_tries_again(store, fake_clock):
    runners = registry_returning(Fail("flaky search", retry=True),
                                 Finish("got it", say="here you go"),
                                 default_interval=60)
    sched = scheduler(store, runners, fake_clock, deliver=lambda job: True)
    job = sched.submit("probe", "persistent")

    tick(sched)
    assert store.get(job.id).state == JobState.WAITING
    assert store.get(job.id).error == "flaky search"

    fake_clock.advance(60)
    tick(sched)
    assert store.get(job.id).state == JobState.DONE
    assert store.get(job.id).error == ""          # cleared on success


def test_a_runner_that_raises_is_retried_not_fatal(store, fake_clock):
    runners = RunnerRegistry()

    @runners.runner("probe", description="Explodes.", default_interval=60)
    def _run(ctx):
        raise ConnectionError("network went away")

    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "unlucky")
    tick(sched)
    assert store.get(job.id).state == JobState.WAITING
    assert "ConnectionError" in store.get(job.id).error


def test_endless_retries_still_stop_at_the_step_budget(store, fake_clock):
    """The budget is enforced outside the runner, so a broken one cannot loop forever."""
    runners = registry_returning(Fail("still broken", retry=True),
                                 max_steps=3, default_interval=60)
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "hopeless")

    for _ in range(5):
        tick(sched)
        fake_clock.advance(60)

    final = store.get(job.id)
    assert final.state == JobState.FAILED
    assert final.steps_done == 3
    assert "budget" in final.error


def test_a_job_that_spends_its_last_step_reports_finished_immediately(store, fake_clock):
    runners = registry_returning(Continue(), max_steps=1)
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "one shot")
    tick(sched)
    assert store.get(job.id).state == JobState.FAILED     # not left WAITING


def test_expiry_stops_a_job_before_it_runs_again(store, fake_clock):
    runners = registry_returning(Continue(), default_interval=60, ttl_hours=1)
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "long runner")
    tick(sched)

    fake_clock.advance(3601)
    tick(sched)
    assert store.get(job.id).state == JobState.FAILED
    assert "ran out of time" in store.get(job.id).error
    assert store.get(job.id).steps_done == 1              # no extra step was run


def test_a_job_whose_runner_disappeared_fails_cleanly(store, fake_clock):
    """A type removed from the code, with rows still in the database."""
    runners = registry_returning(Continue())
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "orphan")
    orphaned = scheduler(store, RunnerRegistry(), fake_clock)

    asyncio.run(orphaned.tick())
    assert store.get(job.id).state == JobState.FAILED
    assert "unknown job type" in store.get(job.id).error


def test_a_cancelled_job_is_not_run(store, fake_clock):
    runners = registry_returning(Continue())
    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "never mind")
    sched.cancel(job.id)
    tick(sched)
    assert store.get(job.id).steps_done == 0


# ── runner plumbing ─────────────────────────────────────────────────────────
def test_an_async_runner_is_awaited(store, fake_clock):
    runners = RunnerRegistry()

    @runners.runner("probe", description="Async.")
    async def _run(ctx):
        await asyncio.sleep(0)
        return Finish("done asynchronously")

    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "async work")
    tick(sched)
    assert store.get(job.id).result == "done asynchronously"


def test_services_reach_the_runner(store, fake_clock):
    runners = RunnerRegistry()

    @runners.runner("probe", description="Uses a service.")
    def _run(ctx):
        return Finish(ctx.require("greeter")())

    sched = scheduler(store, runners, fake_clock,
                      services={"greeter": lambda: "hello from the host"})
    job = sched.submit("probe", "greet")
    tick(sched)
    assert store.get(job.id).result == "hello from the host"


def test_a_missing_service_is_a_retryable_failure_with_a_clear_message(store, fake_clock):
    runners = RunnerRegistry()

    @runners.runner("probe", description="Needs something absent.")
    def _run(ctx):
        return Finish(ctx.require("nonexistent"))

    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "needs a service")
    tick(sched)
    assert "nonexistent" in store.get(job.id).error


def test_runner_log_notes_are_recorded_as_progress(store, fake_clock):
    runners = RunnerRegistry()

    @runners.runner("probe", description="Chatty.")
    def _run(ctx):
        ctx.log("looked at the first source")
        ctx.log("looked at the second source")
        return Finish("read two sources")

    sched = scheduler(store, runners, fake_clock)
    job = sched.submit("probe", "research")
    tick(sched)
    notes = [event["note"] for event in store.events(job.id)]
    assert "looked at the first source" in notes
    assert "looked at the second source" in notes


def test_max_running_caps_how_many_steps_run_in_one_tick(store, fake_clock):
    runners = registry_returning(Continue(), max_concurrent=10, default_interval=60)
    sched = scheduler(store, runners, fake_clock, max_running=2)
    jobs = [sched.submit("probe", f"job {i}") for i in range(5)]
    tick(sched)
    assert sum(1 for j in jobs if store.get(j.id).steps_done == 1) == 2


def test_immediate_continuations_run_back_to_back_in_one_tick(store, fake_clock):
    """Consecutive steps of one continuous job — plan, then search, then write."""
    runners = registry_returning(Continue(sleep=0), max_steps=20)
    sched = scheduler(store, runners, fake_clock, max_passes=4)
    job = sched.submit("probe", "continuous work")
    tick(sched)
    assert store.get(job.id).steps_done == 4          # not 1


def test_a_greedy_runner_cannot_monopolise_the_loop(store, fake_clock):
    """sleep=0 forever is bounded per tick, and by the step budget overall."""
    runners = registry_returning(Continue(sleep=0), max_steps=100)
    sched = scheduler(store, runners, fake_clock, max_passes=3)
    job = sched.submit("probe", "greedy")
    tick(sched)
    assert store.get(job.id).steps_done == 3


def test_chasing_does_not_sweep_up_unrelated_backlog(store, fake_clock):
    """Only the jobs already touched this tick get chased, never the queue."""
    runners = registry_returning(Continue(sleep=0), max_concurrent=10, max_steps=20)
    sched = scheduler(store, runners, fake_clock, max_running=1, max_passes=3)
    first = sched.submit("probe", "first")
    second = sched.submit("probe", "second")
    tick(sched)
    assert store.get(first.id).steps_done == 3
    assert store.get(second.id).steps_done == 0       # waits its turn


# ── reporting ───────────────────────────────────────────────────────────────
def test_summary_when_nothing_is_running(store, fake_clock):
    sched = scheduler(store, RunnerRegistry(), fake_clock)
    assert "Nothing running" in sched.summary()


def test_summary_names_the_work_in_flight(store, fake_clock):
    runners = registry_returning(Continue(), max_concurrent=5)
    sched = scheduler(store, runners, fake_clock)
    sched.submit("probe", "watch the cricket")
    sched.submit("probe", "track the parcel")
    summary = sched.summary()
    assert "2 things going" in summary
    assert "watch the cricket" in summary
    assert "track the parcel" in summary


def test_status_reports_progress_then_the_result(store, fake_clock):
    runners = registry_returning(Continue(note="checked once"),
                                 Finish("it landed", say="it landed"),
                                 default_interval=60)
    sched = scheduler(store, runners, fake_clock, deliver=lambda job: False)
    job = sched.submit("probe", "track the parcel")

    tick(sched)
    assert "checked once" in sched.status(job.id)

    fake_clock.advance(60)
    tick(sched)
    assert "it landed" in sched.status(job.id)


def test_status_of_an_unknown_job(store, fake_clock):
    sched = scheduler(store, RunnerRegistry(), fake_clock)
    assert "don't have a job" in sched.status("abcd1234")


def test_documentation_lists_every_type_with_its_budget(store, fake_clock):
    runners = RunnerRegistry()
    runners.runner("research", description="Read around a topic.",
                   max_steps=8, ttl_hours=6)(lambda ctx: Finish("x"))
    runners.runner("watch", description="Wait for something to happen.",
                   max_steps=40, ttl_hours=24)(lambda ctx: Finish("x"))
    docs = runners.documentation()
    assert "research — Read around a topic. (up to 8 steps over 6h)" in docs
    assert "watch — Wait for something to happen. (up to 40 steps over 24h)" in docs
