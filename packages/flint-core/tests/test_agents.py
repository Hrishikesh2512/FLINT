"""The agent bus: selection, streaming progress, and honest results.

The CLI tests drive a real subprocess — a short python -c script — because the
things worth testing here (streaming, timeouts, killing a hung process, merged
stderr) are exactly the things a mocked Popen would not catch.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from flint_core.agents import (
    AgentRegistry,
    AgentRequest,
    AgentResult,
    AgentSpec,
    CLIAgentConfig,
    NoAgentAvailableError,
    agents_from_config,
    cli_agent_spec,
)
from flint_core.agents.cli import CLIAgent
from flint_core.llm.routing import Task


def spec(name, good_at=(), available=True, result=None, priority=0):
    return AgentSpec(
        name=name, summary=f"The {name} agent.",
        good_at=frozenset(good_at), available=available, priority=priority,
        run=lambda req: result or AgentResult(ok=True, summary=f"{name} did it"))


# ── selection ───────────────────────────────────────────────────────────────
def test_the_agent_named_for_the_task_wins():
    registry = AgentRegistry([spec("general"), spec("coder", [Task.CODE])])
    assert registry.pick(Task.CODE).name == "coder"


def test_a_general_agent_beats_one_specialised_elsewhere():
    registry = AgentRegistry([spec("researcher", [Task.REASONING]), spec("general")])
    assert registry.pick(Task.CODE).name == "general"


def test_priority_breaks_a_tie():
    registry = AgentRegistry([spec("slow", [Task.CODE], priority=1),
                              spec("fast", [Task.CODE], priority=9)])
    assert registry.pick(Task.CODE).name == "fast"


def test_an_unavailable_agent_is_never_picked():
    registry = AgentRegistry([spec("coder", [Task.CODE], available=False),
                              spec("general")])
    assert registry.pick(Task.CODE).name == "general"
    assert registry.names() == ["general"]


def test_selection_orders_without_dropping():
    registry = AgentRegistry([spec("a", [Task.CODE]), spec("b", [Task.REASONING])])
    assert len(registry.candidates(Task.CODE)) == 2


def test_no_agents_at_all_is_an_error_worth_hearing():
    with pytest.raises(NoAgentAvailableError, match="no agent is available"):
        AgentRegistry().run(AgentRequest(goal="do a thing"), task=Task.CODE)


def test_an_agent_can_be_named_explicitly():
    registry = AgentRegistry([spec("coder", [Task.CODE]), spec("other")])
    result = registry.run(AgentRequest(goal="x"), agent="other")
    assert result.summary == "other did it"


def test_naming_an_agent_that_does_not_exist_says_what_does():
    registry = AgentRegistry([spec("coder")])
    with pytest.raises(NoAgentAvailableError, match="have: coder"):
        registry.get("nonexistent")


def test_duplicate_agents_are_rejected():
    registry = AgentRegistry([spec("coder")])
    with pytest.raises(ValueError, match="duplicate agent"):
        registry.add(spec("coder"))


# ── running ─────────────────────────────────────────────────────────────────
def test_the_result_is_attributed_even_if_the_agent_forgets():
    registry = AgentRegistry([
        AgentSpec(name="forgetful", summary="s",
                  run=lambda req: AgentResult(ok=True, summary="done"))])
    assert registry.run(AgentRequest(goal="x"), agent="forgetful").agent == "forgetful"


def test_an_agent_that_raises_becomes_a_failed_result():
    """The caller is mid-conversation and needs something to say, not a traceback."""
    def explode(request):
        raise RuntimeError("the CLI is on fire")

    registry = AgentRegistry([AgentSpec(name="broken", summary="s", run=explode)])
    result = registry.run(AgentRequest(goal="x"), agent="broken")
    assert result.ok is False
    assert "on fire" in result.error
    assert result.agent == "broken"


def test_a_request_needs_a_goal():
    with pytest.raises(ValueError, match="needs a goal"):
        AgentRequest(goal="   ")


def test_a_result_can_ask_a_question_back():
    """The agent has no microphone; the caller does. Without this, an
    ambiguous task can only fail."""
    result = AgentResult(ok=False, summary="stopped",
                         question="Which branch should I target?")
    assert result.needs_input is True
    assert result.spoken() == "Which branch should I target?"


def test_a_spoken_summary_is_trimmed():
    from flint_core.agents.base import MAX_SPOKEN

    result = AgentResult(ok=True, summary="x" * 5000)
    assert len(result.spoken()) <= MAX_SPOKEN + 2


def test_progress_failures_never_break_the_run():
    def bad_listener(line):
        raise ValueError("listener exploded")

    request = AgentRequest(goal="x", on_progress=bad_listener)
    request.progress("something happened")      # must not raise


# ── the CLI agent, against real subprocesses ────────────────────────────────
def python_agent(script: str, **kw) -> CLIAgent:
    """A CLI agent that runs a python snippet, with {goal} interpolated."""
    return CLIAgent(CLIAgentConfig(
        name="pyagent", command=(sys.executable, "-c", script, "{goal}"), **kw))


def test_output_is_streamed_line_by_line_as_it_happens(tmp_path):
    """The whole difference from a one-shot RPC."""
    seen: list[str] = []
    agent = python_agent(
        "import sys\n"
        "for i in range(3): print('step', i, flush=True)\n"
        "print('finished', sys.argv[1])\n")
    result = agent.run(AgentRequest(goal="the task", cwd=str(tmp_path),
                                    on_progress=seen.append))
    assert result.ok is True
    assert "step 0" in seen and "step 2" in seen
    assert any("finished the task" in line for line in seen)


def test_the_goal_actually_reaches_the_command(tmp_path):
    agent = python_agent("import sys; print('got:', sys.argv[1])")
    result = agent.run(AgentRequest(goal="refactor the parser", cwd=str(tmp_path)))
    assert "got: refactor the parser" in result.detail


def test_a_nonzero_exit_is_a_failure_with_the_output_kept(tmp_path):
    agent = python_agent("import sys; print('it broke'); sys.exit(3)")
    result = agent.run(AgentRequest(goal="x", cwd=str(tmp_path)))
    assert result.ok is False
    assert "exited with code 3" in result.error
    assert "it broke" in result.detail


def test_stderr_is_captured_not_lost(tmp_path):
    agent = python_agent(
        "import sys; print('to stderr', file=sys.stderr); sys.stderr.flush()")
    result = agent.run(AgentRequest(goal="x", cwd=str(tmp_path)))
    assert "to stderr" in result.detail


def test_a_hung_agent_is_stopped_and_reported(tmp_path):
    """It printed something, then wedged. The partial output is kept."""
    import time as _time

    agent = python_agent("import time; print('working', flush=True); time.sleep(60)",
                         timeout=1.5)
    started = _time.monotonic()
    result = agent.run(AgentRequest(goal="x", cwd=str(tmp_path), timeout=1.5))
    assert result.ok is False
    assert "timed out" in result.error
    assert "stopped it" in result.summary
    assert "working" in result.detail
    assert _time.monotonic() - started < 30      # killed, not waited out


def test_an_agent_that_hangs_silently_is_still_stopped(tmp_path):
    """Regression: the timeout used to be checked inside the stdout loop, which
    blocks until the next line — so an agent that produced no output at all was
    never noticed, which is precisely the case a timeout is for."""
    import time as _time

    agent = python_agent("import time; time.sleep(60)", timeout=1.5)
    started = _time.monotonic()
    result = agent.run(AgentRequest(goal="x", cwd=str(tmp_path), timeout=1.5))
    assert result.ok is False and "timed out" in result.error
    assert _time.monotonic() - started < 30


def test_a_missing_executable_is_a_clean_failure(tmp_path):
    agent = CLIAgent(CLIAgentConfig(
        name="ghost", command=("definitely-not-a-real-binary-xyz", "{goal}")))
    result = agent.run(AgentRequest(goal="x", cwd=str(tmp_path)))
    assert result.ok is False
    assert "could not start" in result.error


def test_a_missing_directory_is_a_clean_failure():
    agent = python_agent("print('hi')")
    result = agent.run(AgentRequest(goal="x", cwd="/no/such/place/at/all"))
    assert result.ok is False
    assert "no such directory" in result.error


def test_changed_files_are_observed_from_git_not_claimed(tmp_path):
    """What actually changed on disk, not what the model said it did."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    agent = python_agent(
        "open('created_by_agent.txt', 'w').write('hello')\n"
        "print('wrote the file')\n")
    result = agent.run(AgentRequest(goal="make a file", cwd=str(tmp_path)))
    assert result.ok is True
    assert "created_by_agent.txt" in result.artifacts
    assert "1 file changed" in result.summary


def test_no_git_repo_means_no_artifacts_rather_than_a_crash(tmp_path):
    agent = python_agent("open('x.txt','w').write('hi'); print('done')")
    result = agent.run(AgentRequest(goal="x", cwd=str(tmp_path)))
    assert result.ok is True
    assert result.artifacts == ()


def test_huge_output_is_truncated_in_the_middle(tmp_path):
    from flint_core.agents.cli import MAX_DETAIL

    agent = python_agent("print('x' * 200000)")
    result = agent.run(AgentRequest(goal="x", cwd=str(tmp_path)))
    assert len(result.detail) < MAX_DETAIL + 200
    assert "characters omitted" in result.detail


# ── configuration ───────────────────────────────────────────────────────────
def test_a_command_without_a_goal_placeholder_is_rejected():
    """It would run the same thing whatever it was asked."""
    with pytest.raises(ValueError, match="no .goal. placeholder"):
        CLIAgentConfig(name="x", command=("claude", "-p", "hardcoded"))


def test_an_empty_command_is_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        CLIAgentConfig(name="x", command=())


def test_an_uninstalled_cli_registers_as_unavailable():
    registered = cli_agent_spec(CLIAgentConfig(
        name="ghost", command=("definitely-not-a-real-binary-xyz", "{goal}")))
    assert registered.available is False


def test_a_coding_agent_declares_what_it_needs_permission_for():
    registered = cli_agent_spec(CLIAgentConfig(
        name="x", command=(sys.executable, "-c", "pass", "{goal}")))
    assert set(registered.permissions) == {"shell", "files"}


def test_agents_can_be_built_from_config():
    specs = agents_from_config([
        {"name": "claude", "command": ["claude", "-p", "{goal}"],
         "good_at": ["code"], "priority": 5},
    ])
    assert len(specs) == 1
    assert specs[0].name == "claude" and Task.CODE in specs[0].good_at


def test_a_bad_agent_entry_is_skipped_not_fatal():
    specs = agents_from_config([
        {"name": "", "command": ["x", "{goal}"]},          # no name
        {"name": "noplaceholder", "command": ["x"]},        # no {goal}
        {"name": "fine", "command": ["echo", "{goal}"]},
    ])
    assert [s.name for s in specs] == ["fine"]


def test_the_shipped_default_can_actually_write_files():
    """Regression, found on hardware: `claude -p "{goal}"` alone answers the
    question and writes nothing, so a build job reported success having
    produced zero files. An agent hired to build must be allowed to build."""
    from flint_core.agents import CLAUDE_CODE_DEFAULT

    assert "--permission-mode" in CLAUDE_CODE_DEFAULT
    assert "acceptEdits" in CLAUDE_CODE_DEFAULT
    CLIAgentConfig(name="claude", command=CLAUDE_CODE_DEFAULT)   # still valid


def test_the_shipped_default_does_not_bypass_every_check():
    """acceptEdits, not bypassPermissions — the loop runs the tests itself, so
    the agent never needs shell access to verify its own work."""
    from flint_core.agents import CLAUDE_CODE_DEFAULT

    assert "bypassPermissions" not in CLAUDE_CODE_DEFAULT
    assert not any("dangerously" in part for part in CLAUDE_CODE_DEFAULT)
