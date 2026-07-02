import pytest

from flint_core.tools import (
    InvalidArgumentsError,
    ToolError,
    ToolRegistry,
    ToolSpec,
    UnknownToolError,
)

WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "description": "City name"},
        "days": {"type": "integer", "description": "Forecast days"},
    },
    "required": ["city"],
}


@pytest.fixture()
def registry():
    reg = ToolRegistry(platform="windows")

    @reg.tool(description="Current weather for a city.", parameters=WEATHER_SCHEMA)
    def weather_report(city: str, days: int = 1) -> str:
        return f"{city}:{days}"

    @reg.tool(description="Windows-only automation.", platforms=("windows",))
    def click_stuff() -> str:
        return "clicked"

    @reg.tool(description="Pi-only wake word control.", platforms=("linux",))
    def wake_word() -> str:
        return "listening"

    return reg


def test_dispatch_happy_path(registry):
    assert registry.dispatch("weather_report", {"city": "Pune"}) == "Pune:1"
    assert registry.dispatch("weather_report", {"city": "Pune", "days": 3}) == "Pune:3"


def test_unknown_tool(registry):
    with pytest.raises(UnknownToolError):
        registry.dispatch("nope", {})


def test_missing_required_argument(registry):
    with pytest.raises(InvalidArgumentsError, match="city"):
        registry.dispatch("weather_report", {})


def test_unknown_argument_dropped_not_fatal(registry):
    # LLMs invent extra args; those are dropped, the call still succeeds.
    assert registry.dispatch("weather_report", {"city": "Pune", "bogus": 1}) == "Pune:1"


def test_wrong_type_rejected(registry):
    with pytest.raises(InvalidArgumentsError, match="days"):
        registry.dispatch("weather_report", {"city": "Pune", "days": "three"})


def test_duplicate_registration_rejected(registry):
    with pytest.raises(ToolError, match="duplicate"):

        @registry.tool(description="again")
        def weather_report():  # noqa: F811
            pass


def test_platform_gating(registry):
    assert "click_stuff" in registry.names("windows")
    assert "wake_word" not in registry.names("windows")
    assert "wake_word" in registry.names("linux")
    assert "weather_report" in registry.names("linux")  # platforms=any


def test_extra_context_passed_only_if_accepted():
    reg = ToolRegistry()

    @reg.tool(description="wants context")
    def with_ctx(speak=None) -> str:
        return f"speak={speak is not None}"

    @reg.tool(description="plain")
    def without_ctx() -> str:
        return "plain"

    assert reg.dispatch("with_ctx", {}, speak=lambda t: t) == "speak=True"
    assert reg.dispatch("without_ctx", {}, speak=lambda t: t) == "plain"


def test_gemini_declarations(registry):
    decls = {d["name"]: d for d in registry.gemini_declarations()}
    assert decls["weather_report"]["parameters"] == WEATHER_SCHEMA
    assert "parameters" not in decls["click_stuff"]


def test_gemini_declarations_uppercase_for_live_api(registry):
    decls = {d["name"]: d for d in registry.gemini_declarations(uppercase_types=True)}
    schema = decls["weather_report"]["parameters"]
    assert schema["type"] == "OBJECT"
    assert schema["properties"]["city"]["type"] == "STRING"
    assert schema["properties"]["days"]["type"] == "INTEGER"
    # source schema untouched (deep copy)
    assert WEATHER_SCHEMA["type"] == "object"


def test_parameters_arg_style_bridges_legacy_actions():
    reg = ToolRegistry()

    @reg.tool(
        description="legacy-style action",
        parameters=WEATHER_SCHEMA,
        arg_style="parameters",
    )
    def legacy_action(parameters=None, player=None):
        return f"{parameters['city']}|player={player}"

    assert reg.dispatch("legacy_action", {"city": "Pune"}, player="UI") == "Pune|player=UI"


def test_planner_documentation_exclude():
    reg = ToolRegistry()

    @reg.tool(description="visible tool")
    def alpha():
        pass

    @reg.tool(description="hidden tool")
    def beta():
        pass

    doc = reg.planner_documentation(exclude=("beta",))
    assert "alpha" in doc and "beta" not in doc


def test_openai_tools_format(registry):
    tools = registry.openai_tools()
    entry = next(t for t in tools if t["function"]["name"] == "weather_report")
    assert entry["type"] == "function"
    assert entry["function"]["parameters"]["required"] == ["city"]


def test_planner_documentation_generated(registry):
    doc = registry.planner_documentation()
    assert "weather_report" in doc
    assert "city: string (required)" in doc
    assert "wake_word" not in doc  # platform=windows registry default


def test_spec_validation():
    with pytest.raises(ToolError, match="identifier"):
        ToolSpec("bad name", "desc", {}, lambda: None)
    with pytest.raises(ToolError, match="description"):
        ToolSpec("ok", "  ", {}, lambda: None)
    with pytest.raises(ToolError, match="type=object"):
        ToolSpec("ok", "desc", {"type": "string"}, lambda: None)
