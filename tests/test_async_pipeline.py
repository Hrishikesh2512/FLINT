"""Behavioral tests for core.async_pipeline — the job queue every tool call rides on."""

import threading
import time

import pytest

from core.async_pipeline import AsyncPipeline, Priority


@pytest.fixture()
def pipeline():
    return AsyncPipeline(workers=2, name="test-pipeline")


def test_submit_returns_result(pipeline):
    job = pipeline.submit("square", lambda: 7 * 7)
    assert job.future.result(timeout=5) == 49
    assert job.state == "finished"


def test_args_and_kwargs_are_passed(pipeline):
    job = pipeline.submit("concat", lambda a, b="": a + b, "foo", b="bar")
    assert job.future.result(timeout=5) == "foobar"


def test_exception_propagates_to_future(pipeline):
    def boom():
        raise RuntimeError("expected failure")

    job = pipeline.submit("boom", boom)
    with pytest.raises(RuntimeError, match="expected failure"):
        job.future.result(timeout=5)
    assert job.state == "failed"


def test_event_bus_emits_lifecycle_events(pipeline):
    seen = []
    done = threading.Event()

    pipeline.bus.on("job_submitted", lambda d: seen.append("submitted"))
    pipeline.bus.on("job_started", lambda d: seen.append("started"))

    def on_finished(d):
        seen.append("finished")
        done.set()

    pipeline.bus.on("job_finished", on_finished)
    pipeline.submit("noop", lambda: None)

    assert done.wait(timeout=5)
    assert seen[0] == "submitted"
    assert "started" in seen and "finished" in seen


def test_subscriber_error_does_not_break_job(pipeline):
    def bad_subscriber(_):
        raise ValueError("subscriber bug")

    pipeline.bus.on("job_started", bad_subscriber)
    job = pipeline.submit("resilient", lambda: "ok")
    assert job.future.result(timeout=5) == "ok"


def test_high_priority_jobs_run_before_low():
    # Single worker so ordering is observable: block it, queue LOW then HIGH,
    # then release — HIGH must be picked up first.
    p = AsyncPipeline(workers=1, name="prio-test")
    gate = threading.Event()
    order = []

    p.submit("blocker", gate.wait, 5)
    time.sleep(0.1)  # let the worker occupy itself with the blocker
    low = p.submit("low", lambda: order.append("low"), priority=Priority.LOW)
    high = p.submit("high", lambda: order.append("high"), priority=Priority.HIGH)
    gate.set()

    low.future.result(timeout=5)
    high.future.result(timeout=5)
    assert order == ["high", "low"]


def test_run_blocks_until_result(pipeline):
    assert pipeline.run("inline", lambda: 21 * 2, timeout=5) == 42
