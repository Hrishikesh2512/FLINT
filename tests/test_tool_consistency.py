"""Cross-checks between the tool registry, the agent executor, and the planner.

These guard against the drift that plagued v1 (tools declared in one place but
unhandled in another). They intentionally avoid importing main.py / ui.py,
which require a display and audio hardware.
"""

import re

from core.tool_registry import TOOL_DECLARATIONS
from agent import executor, planner


def _registry_names() -> set[str]:
    return {tool["name"] for tool in TOOL_DECLARATIONS}


def _executor_dispatch_names() -> set[str]:
    """Tool names the executor's _call_tool dispatch chain handles."""
    import inspect

    src = inspect.getsource(executor._call_tool)
    return set(re.findall(r'tool == "([a-z_]+)"', src))


def test_registry_has_no_duplicate_tools():
    names = [tool["name"] for tool in TOOL_DECLARATIONS]
    assert len(names) == len(set(names))


def test_every_declared_tool_has_schema():
    for tool in TOOL_DECLARATIONS:
        assert tool["description"].strip()
        assert "properties" in tool["parameters"] or tool["parameters"] == {}


def test_executor_dispatch_only_uses_registered_tools():
    # Everything the executor can run must be a declared tool the model
    # could also call directly. (The reverse is not required: some tools —
    # screen_process's silent mode, agent_task itself, save_memory,
    # shutdown_flint, file_processor — are live-session-only.)
    live_session_only = {"agent_task", "save_memory", "shutdown_flint", "file_processor"}
    assert _executor_dispatch_names() <= _registry_names() - live_session_only


def test_planner_prompt_only_references_registered_tools():
    # Tool sections in the planner prompt appear as a name on its own line
    # followed by an indented parameter list.
    prompt_tools = set(
        re.findall(r"^([a-z_]+)\n  ", planner.PLANNER_PROMPT, flags=re.MULTILINE)
    )
    unknown = prompt_tools - _registry_names()
    assert not unknown, f"Planner prompt references unregistered tools: {unknown}"


def test_removed_tools_are_gone_everywhere():
    removed = {"game_updater", "flight_finder", "cmd_control", "generated_code"}
    assert not removed & _registry_names()
    assert not removed & _executor_dispatch_names()
    assert not any(name in planner.PLANNER_PROMPT for name in removed)


def test_executor_rejects_unknown_tool():
    import pytest

    with pytest.raises(ValueError, match="Unknown tool"):
        executor._call_tool("definitely_not_a_tool", {}, None)
