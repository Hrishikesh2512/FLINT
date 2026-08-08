"""JobStore: persistence, claiming, budgets, crash recovery."""

from __future__ import annotations

from dataclasses import replace

import pytest

from flint_core.kernel import Job, JobState, JobStore


@pytest.fixture()
def store(tmp_path, fake_clock):
    return JobStore(tmp_path / "jobs.db", clock=fake_clock)


def make(store, goal="watch the score", type="watch", **kw):
    """Add a job whose clock matches the store's."""
    return store.add(Job.create(type, goal, now=store._clock(), **kw))


def test_roundtrip_preserves_every_field(store):
    job = make(store, params={"condition": "under twenty"}, urgent=True,
               origin="venom")
    loaded = store.get(job.id)
    assert loaded == job
    assert loaded.params == {"condition": "under twenty"}
    assert loaded.urgent is True
    assert loaded.origin == "venom"


def test_missing_job_is_none(store):
    assert store.get("nosuchid") is None


def test_scratch_survives_a_save(store):
    job = make(store)
    store.save(job.with_scratch({"observation": "India 40/2"}))
    assert store.get(job.id).scratch == {"observation": "India 40/2"}


def test_scratch_merges_rather_than_replaces(store):
    job = make(store)
    job = store.save(job.with_scratch({"seen": 1, "topic": "cricket"}))
    job = store.save(job.with_scratch({"seen": 2}))
    assert store.get(job.id).scratch == {"seen": 2, "topic": "cricket"}


def test_due_skips_jobs_scheduled_for_later(store, fake_clock):
    now = make(store, goal="check now")
    later = make(store, goal="check later")
    store.save(replace(later, next_run_at=fake_clock.now + 100))

    assert [j.id for j in store.due()] == [now.id]
    fake_clock.advance(100)
    assert {j.id for j in store.due()} == {now.id, later.id}


def test_due_ignores_held_and_terminal_jobs(store):
    held = make(store, goal="held one")
    store.save(replace(held, state=JobState.HELD, say="done"))
    cancelled = make(store, goal="cancelled one")
    store.cancel(cancelled.id)
    assert store.due() == []


def test_claim_is_exclusive(store):
    job = make(store)
    assert store.claim(job.id) is True
    assert store.claim(job.id) is False          # already RUNNING
    assert store.get(job.id).state == JobState.RUNNING


def test_claim_refuses_a_cancelled_job(store):
    job = make(store)
    store.cancel(job.id)
    assert store.claim(job.id) is False


def test_cancel_leaves_terminal_jobs_alone(store):
    job = make(store)
    assert store.cancel(job.id) is True
    assert store.cancel(job.id) is False
    assert store.get(job.id).state == JobState.CANCELLED


def test_cancel_matching_by_goal_text(store):
    make(store, goal="watch the cricket score")
    make(store, goal="watch the election result")
    assert store.cancel_matching("cricket") == 1
    assert len(store.active()) == 1
    assert store.cancel_matching() == 1           # blank = everything
    assert store.active() == []


def test_expire_fails_jobs_past_their_deadline(store, fake_clock):
    job = make(store, ttl_hours=1)
    fake_clock.advance(3601)
    expired = store.expire()
    assert [j.id for j in expired] == [job.id]
    assert store.get(job.id).state == JobState.FAILED
    assert "ran out of time" in store.get(job.id).error


def test_expire_fails_jobs_out_of_steps(store):
    job = make(store, max_steps=2)
    store.save(replace(job, steps_done=2))
    store.expire()
    assert "budget of 2 steps" in store.get(job.id).error


def test_expire_never_touches_a_held_result(store, fake_clock):
    """The work is already paid for and the user is owed the answer."""
    job = make(store, ttl_hours=1)
    store.save(replace(job, state=JobState.HELD, say="India need 18"))
    fake_clock.advance(86400)
    assert store.expire() == []
    assert store.get(job.id).state == JobState.HELD


def test_events_are_appended_and_capped(store):
    from flint_core.kernel.store import MAX_EVENTS_PER_JOB

    job = make(store)
    for i in range(MAX_EVENTS_PER_JOB + 25):
        store.event(job.id, f"note {i}")
    events = store.events(job.id, limit=1000)
    assert len(events) == MAX_EVENTS_PER_JOB
    assert events[-1]["note"] == f"note {MAX_EVENTS_PER_JOB + 24}"   # newest kept


def test_blank_events_are_ignored(store):
    job = make(store)
    store.event(job.id, "   ")
    assert store.events(job.id) == []


def test_count_running_is_per_type(store):
    make(store, goal="one")
    make(store, goal="two")
    make(store, goal="read about rust", type="research")
    assert store.count_running("watch") == 2
    assert store.count_running("research") == 1
    assert store.count_running() == 3


def test_count_running_excludes_finished_work(store):
    job = make(store)
    store.cancel(job.id)
    assert store.count_running("watch") == 0


def test_restart_requeues_a_job_that_was_mid_step(tmp_path, fake_clock):
    path = tmp_path / "jobs.db"
    first = JobStore(path, clock=fake_clock)
    job = make(first)
    assert first.claim(job.id) is True            # RUNNING when the power goes
    first.close()

    second = JobStore(path, clock=fake_clock)     # process restarts
    assert second.get(job.id).state == JobState.WAITING
    assert "interrupted mid-step" in second.events(job.id)[-1]["note"]


def test_restart_leaves_settled_jobs_untouched(tmp_path, fake_clock):
    path = tmp_path / "jobs.db"
    first = JobStore(path, clock=fake_clock)
    waiting = make(first)
    done = make(first, goal="already done")
    first.save(replace(done, state=JobState.DONE))
    first.close()

    second = JobStore(path, clock=fake_clock)
    assert second.get(waiting.id).state == JobState.PENDING
    assert second.get(done.id).state == JobState.DONE


def test_purge_removes_old_terminal_jobs_and_their_events(store, fake_clock):
    job = make(store)
    store.event(job.id, "something happened")
    store.cancel(job.id)
    assert store.purge(older_than_seconds=100) == 0    # too recent
    fake_clock.advance(200)
    assert store.purge(older_than_seconds=100) == 1
    assert store.get(job.id) is None
    assert store.events(job.id) == []


def test_purge_spares_live_jobs(store, fake_clock):
    job = make(store)
    fake_clock.advance(10_000_000)
    assert store.purge(older_than_seconds=1) == 0
    assert store.get(job.id) is not None
