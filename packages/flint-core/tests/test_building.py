"""Building an app and fixing it until it runs — the loop, not the generation."""

from __future__ import annotations

import asyncio

import pytest

from flint_core.agents import AgentRegistry, AgentResult, AgentSpec
from flint_core.building import (
    MAX_FIX_ATTEMPTS,
    detect_verify_command,
    run_build,
)
from flint_core.kernel import (
    Continue,
    Fail,
    Finish,
    JobStore,
    RunnerRegistry,
    Scheduler,
)


class Ctx:
    """A JobContext without a kernel behind it, for single-phase tests."""

    def __init__(self, goal, cwd, scratch=None, services=None, **params):
        self.job = type("J", (), {"goal": goal, "type": "build"})()
        self.goal = goal
        self.params = {"cwd": str(cwd), **params}
        self.scratch = dict(scratch or {})
        self.services = services or {}
        self.notes: list[str] = []

    def log(self, note):
        self.notes.append(note)

    def require(self, name):
        return self.services[name]

    def service(self, name, default=None):
        return self.services.get(name, default)


def agent_registry(*results):
    """An agent returning each result in turn, then repeating the last."""
    queue = list(results) or [AgentResult(ok=True, summary="wrote it")]
    seen: list = []

    def run(request):
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    registry = AgentRegistry([AgentSpec(name="coder", summary="Writes code.",
                                        run=run)])
    registry.seen = seen        # type: ignore[attr-defined]
    return registry


def verifier(*results):
    """A verify service returning each (ok, output) in turn."""
    queue = list(results)
    calls: list = []

    def verify(cwd, command, timeout):
        calls.append((cwd, tuple(command)))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    verify.calls = calls        # type: ignore[attr-defined]
    return verify


# ── choosing how to check the work ──────────────────────────────────────────
@pytest.mark.parametrize("files,expected", [
    (["tests", "main.py"], ("python", "-m", "pytest", "-q")),
    (["pytest.ini", "app.py"], ("python", "-m", "pytest", "-q")),
    (["package.json", "index.js"], ("npm", "test", "--silent")),
    (["Cargo.toml", "src"], ("cargo", "test", "-q")),
    (["go.mod"], ("go", "test", "./...")),
    (["main.py"], ("python", "main.py")),
    (["index.js"], ("node", "index.js")),
    (["test_thing.py"], ("python", "-m", "pytest", "-q")),
])
def test_the_check_is_chosen_from_what_is_actually_there(files, expected):
    assert detect_verify_command(files)[0] == expected


def test_a_real_test_suite_beats_a_smoke_run():
    command, description = detect_verify_command(["main.py", "tests"])
    assert command == ("python", "-m", "pytest", "-q")
    assert description == "the test suite"


def test_an_unrecognisable_project_gets_no_command():
    """Better to say it couldn't be checked than to invent a way to run it."""
    assert detect_verify_command(["README.md", "notes.txt"]) == ((), "")


def test_the_command_never_comes_from_the_model(tmp_path):
    """The thing being executed must not be chosen by the thing being debugged."""
    (tmp_path / "main.py").write_text("print('hi')", encoding="utf-8")
    verify = verifier((True, "ok"))
    ctx = Ctx("build a thing", tmp_path,
              scratch={"phase": "verify", "verify_command": ["rm", "-rf", "/"]},
              services={"agents": agent_registry(), "verify": verify})
    run_build(ctx)
    assert verify.calls[0][1] == ("python", "main.py")


# ── the loop ────────────────────────────────────────────────────────────────
def test_building_then_verifying_is_two_phases(tmp_path):
    (tmp_path / "main.py").write_text("x", encoding="utf-8")
    agents = agent_registry(AgentResult(ok=True, summary="wrote it",
                                        artifacts=("main.py",)))
    ctx = Ctx("build a cli", tmp_path, services={"agents": agents})

    first = run_build(ctx)
    assert isinstance(first, Continue)
    assert first.scratch["phase"] == "verify"
    assert first.scratch["written"] == ["main.py"]


def test_a_passing_check_finishes(tmp_path):
    (tmp_path / "tests").mkdir()
    ctx = Ctx("build a cli", tmp_path,
              scratch={"phase": "verify", "agent_summary": "wrote 3 files",
                       "written": ["a.py", "b.py"]},
              services={"agents": agent_registry(),
                        "verify": verifier((True, "3 passed"))})
    outcome = run_build(ctx)
    assert isinstance(outcome, Finish)
    assert "the test suite passes" in outcome.say
    assert "3 passed" in outcome.result


def test_a_failing_check_goes_to_fix_and_keeps_the_error(tmp_path):
    (tmp_path / "tests").mkdir()
    ctx = Ctx("build a cli", tmp_path, scratch={"phase": "verify"},
              services={"agents": agent_registry(),
                        "verify": verifier((False, "AssertionError: nope"))})
    outcome = run_build(ctx)
    assert isinstance(outcome, Continue)
    assert outcome.scratch["phase"] == "fix"
    assert "AssertionError" in outcome.scratch["last_error"]


def test_the_failure_is_handed_back_to_the_agent(tmp_path):
    agents = agent_registry(AgentResult(ok=True, summary="fixed it"))
    ctx = Ctx("build a cli", tmp_path,
              scratch={"phase": "fix", "last_error": "AssertionError: nope",
                       "verify_command": ["python", "-m", "pytest"]},
              services={"agents": agents})
    run_build(ctx)
    instruction = agents.seen[0].goal
    assert "AssertionError: nope" in instruction
    assert "build a cli" in instruction
    assert "Do not delete tests" in instruction     # the obvious cheat, forbidden


def test_fixing_counts_attempts_and_eventually_gives_up(tmp_path):
    ctx = Ctx("build a cli", tmp_path,
              scratch={"phase": "fix", "attempts": MAX_FIX_ATTEMPTS,
                       "last_error": "still broken"},
              services={"agents": agent_registry()})
    outcome = run_build(ctx)
    assert isinstance(outcome, Fail)
    assert outcome.retry is False
    assert f"after {MAX_FIX_ATTEMPTS} attempts" in outcome.error
    assert "still broken" in outcome.error


def test_a_fix_increments_the_attempt_count(tmp_path):
    ctx = Ctx("x", tmp_path, scratch={"phase": "fix", "attempts": 1},
              services={"agents": agent_registry()})
    assert run_build(ctx).scratch["attempts"] == 2


def test_building_does_not_count_as_a_fix_attempt(tmp_path):
    ctx = Ctx("x", tmp_path, services={"agents": agent_registry()})
    assert run_build(ctx).scratch["attempts"] == 0


# ── honesty about what was achieved ─────────────────────────────────────────
def test_an_unverifiable_project_says_so_rather_than_claiming_success(tmp_path):
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    ctx = Ctx("write some docs", tmp_path,
              scratch={"phase": "verify", "agent_summary": "wrote a readme"},
              services={"agents": agent_registry()})
    outcome = run_build(ctx)
    assert isinstance(outcome, Finish)
    assert "nothing I could run to check" in outcome.say


def test_the_number_of_fixes_is_reported(tmp_path):
    (tmp_path / "tests").mkdir()
    ctx = Ctx("x", tmp_path,
              scratch={"phase": "verify", "attempts": 2, "written": ["a.py"]},
              services={"agents": agent_registry(),
                        "verify": verifier((True, "ok"))})
    assert "after 2 fixes" in run_build(ctx).say


def test_a_broken_agent_is_not_retried_as_a_code_failure(tmp_path):
    """The agent failing to start is not the same as the code failing."""
    ctx = Ctx("x", tmp_path, services={
        "agents": agent_registry(AgentResult.failed("claude is not installed"))})
    outcome = run_build(ctx)
    assert isinstance(outcome, Fail)
    assert outcome.retry is False
    assert "not installed" in outcome.error


def test_an_agent_question_comes_back_to_the_user(tmp_path):
    ctx = Ctx("x", tmp_path, services={"agents": agent_registry(
        AgentResult(ok=False, summary="", question="Python or Node?"))})
    outcome = run_build(ctx)
    assert isinstance(outcome, Finish)
    assert outcome.say == "Python or Node?"


def test_a_missing_directory_fails_cleanly(tmp_path):
    ctx = Ctx("x", tmp_path / "nope", services={"agents": agent_registry()})
    outcome = run_build(ctx)
    assert isinstance(outcome, Fail) and "no such directory" in outcome.error


# ── the whole cycle, on the real kernel ─────────────────────────────────────
def test_a_build_that_fails_twice_then_passes(tmp_path, fake_clock):
    """The point of the whole module: it does not work, then it does."""
    (tmp_path / "tests").mkdir()
    agents = agent_registry(AgentResult(ok=True, summary="wrote it",
                                        artifacts=("main.py",)))
    verify = verifier((False, "ImportError: no module named x"),
                      (False, "AssertionError: wrong answer"),
                      (True, "3 passed"))
    runners = RunnerRegistry()
    runners.runner("build", description="Builds an app.", max_steps=20,
                   default_interval=60.0)(run_build)

    spoken: list[str] = []
    store = JobStore(tmp_path / "jobs.db", clock=fake_clock)
    sched = Scheduler(store, runners, clock=fake_clock,
                      services={"agents": agents, "verify": verify},
                      deliver=lambda job: spoken.append(job.say) or True)
    job = sched.submit("build", "build a cli tool", params={"cwd": str(tmp_path)})

    for _ in range(4):
        asyncio.run(sched.tick())
        fake_clock.advance(60)

    assert spoken and "passes after 2 fixes" in spoken[0]
    assert store.get(job.id).state == "done"
    assert len(verify.calls) == 3
    assert len(agents.seen) == 3            # one build, two fixes


def test_a_build_that_never_works_gives_up_and_says_so(tmp_path, fake_clock):
    (tmp_path / "tests").mkdir()
    runners = RunnerRegistry()
    runners.runner("build", description="Builds an app.", max_steps=40,
                   default_interval=60.0)(run_build)
    store = JobStore(tmp_path / "jobs.db", clock=fake_clock)
    sched = Scheduler(store, runners, clock=fake_clock, max_passes=30,
                      services={"agents": agent_registry(),
                                "verify": verifier((False, "still broken"))})
    job = sched.submit("build", "build the impossible", params={"cwd": str(tmp_path)})

    for _ in range(6):
        asyncio.run(sched.tick())
        fake_clock.advance(60)

    final = store.get(job.id)
    assert final.state == "failed"
    assert "couldn't get it working" in final.error


def test_progress_is_journalled_as_it_goes(tmp_path, fake_clock):
    (tmp_path / "tests").mkdir()
    runners = RunnerRegistry()
    runners.runner("build", description="Builds.", max_steps=20,
                   default_interval=60.0)(run_build)
    store = JobStore(tmp_path / "jobs.db", clock=fake_clock)
    sched = Scheduler(store, runners, clock=fake_clock,
                      services={"agents": agent_registry(),
                                "verify": verifier((False, "boom"), (True, "ok"))},
                      deliver=lambda job: True)
    job = sched.submit("build", "build a cli", params={"cwd": str(tmp_path)})
    for _ in range(3):
        asyncio.run(sched.tick())
        fake_clock.advance(60)

    notes = [e["note"] for e in store.events(job.id)]
    assert any("writing the first version" in n for n in notes)
    assert any("failed" in n for n in notes)
    assert any("fixing (attempt 1" in n for n in notes)
