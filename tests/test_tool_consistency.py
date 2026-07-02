"""Guards for the single-source tool system.

core/tools.py is the only place tools are defined; these tests verify that
everything downstream (Live declarations, planner docs, dispatch) is derived
from it and stays coherent — the drift that plagued v1 cannot recur silently.
"""

import sys

import pytest

from agent import planner
from core.tool_registry import TOOL_DECLARATIONS
from core.tools import EMPTY_RESULT_FALLBACKS, PLANNER_HIDDEN, get_registry
from flint_core.tools import InvalidArgumentsError, UnknownToolError

EXPECTED_TOOLS = {
    "open_app", "web_search", "weather_report", "send_message", "reminder",
    "youtube_video", "screen_process", "computer_settings", "browser_control",
    "file_controller", "desktop_control", "code_helper", "dev_agent",
    "agent_task", "computer_control", "file_processor", "shutdown_flint",
    "save_memory",
}


def test_registry_defines_the_expected_toolset():
    assert set(get_registry().names()) == EXPECTED_TOOLS


def test_importing_tools_does_not_import_action_modules():
    # Lazy imports keep startup light and CI free of pyautogui/cv2/playwright.
    heavy = [m for m in sys.modules if m.startswith("actions.")]
    assert heavy == [], f"actions imported eagerly: {heavy}"


def test_live_declarations_derived_from_registry():
    decls = {d["name"]: d for d in TOOL_DECLARATIONS}
    assert set(decls) == EXPECTED_TOOLS
    # Live API proto form: uppercase type names all the way down.
    schema = decls["open_app"]["parameters"]
    assert schema["type"] == "OBJECT"
    assert schema["properties"]["app_name"]["type"] == "STRING"
    assert decls["web_search"]["parameters"]["properties"]["items"]["type"] == "ARRAY"


def test_live_declarations_validate_against_gemini_sdk():
    genai_types = pytest.importorskip("google.genai.types")
    # If the SDK's Schema proto rejects our declarations, the live session
    # would fail at connect time — catch that here instead.
    config = genai_types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        tools=[{"function_declarations": TOOL_DECLARATIONS}],
    )
    assert config.tools


def test_planner_prompt_contains_generated_docs_only():
    prompt = planner.planner_prompt()
    for name in EXPECTED_TOOLS - set(PLANNER_HIDDEN):
        assert name in prompt, f"{name} missing from planner docs"
    for name in PLANNER_HIDDEN:
        assert f"\n{name}\n" not in prompt, f"hidden tool {name} leaked into planner docs"
    assert "<TOOL_DOCS>" not in prompt


def test_executor_dispatch_rejects_unknown_tool():
    from agent.executor import _call_tool

    with pytest.raises(UnknownToolError):
        _call_tool("definitely_not_a_tool", {}, None)


def test_dispatch_validates_required_args():
    with pytest.raises(InvalidArgumentsError, match="app_name"):
        get_registry().dispatch("open_app", {})


def test_save_memory_dispatch_round_trip(tmp_path, monkeypatch):
    from memory import memory_manager as mm

    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    result = get_registry().dispatch(
        "save_memory",
        {"category": "preferences", "key": "editor", "value": "VS Code"},
    )
    assert result == "ok"
    assert mm.load_memory()["preferences"]["editor"]["value"] == "VS Code"


def test_fallbacks_only_reference_registered_tools():
    assert set(EMPTY_RESULT_FALLBACKS) <= EXPECTED_TOOLS


def test_removed_v1_tools_stay_gone():
    removed = {"game_updater", "flight_finder", "cmd_control", "generated_code"}
    assert not removed & set(get_registry().names())
    assert not any(name in planner.planner_prompt() for name in removed)
