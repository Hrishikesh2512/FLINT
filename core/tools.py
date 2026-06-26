"""Self-registering tool interface for FLINT.

This is the foundation for unifying tool definition, schema, and dispatch
(see issue #8). A tool module declares itself once with the ``@tool``
decorator and is discovered automatically, instead of being wired in three
separate places (``core/tool_registry.py``, ``actions/<tool>.py``, and the
dispatch map in ``main.py``).

This module is additive and changes no runtime behaviour on its own. Tools are
migrated onto it incrementally; the existing hand-written declarations and
dispatch map keep working until the last tool moves over.

Example:

    from core.tools import tool

    @tool(
        name="weather_report",
        description="Gives the weather report to the user.",
        parameters={"city": {"type": "STRING", "description": "City name"}},
        required=["city"],
    )
    def weather_report(args, ctx):
        return ctx.player.report(get_weather(args["city"]))

Run ``python -m core.tools`` to exercise the built-in self-test.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

# A handler takes the parsed arguments dict and a shared Ctx, and returns the
# string the assistant should speak (or None if it speaks through ctx itself).
Handler = Callable[[dict, "Ctx"], Any]


@dataclass
class Ctx:
    """Shared services passed to every tool handler.

    Replaces the ad hoc per-tool parameters (``player``, ``ui``, ``speak``,
    ...) that handlers take today. Fields are optional so handlers depend only
    on what they actually use, and tests can construct a minimal Ctx.
    """

    ui: Any = None
    player: Any = None
    config: Any = None
    pipeline: Any = None
    speak: Callable[[str], Any] | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """A fully described tool: schema plus handler, in one place."""

    name: str
    description: str
    parameters: dict
    required: list
    handler: Handler

    def declaration(self) -> dict:
        """Return the Gemini Live function declaration for this tool.

        Matches the shape used in ``core/tool_registry.py`` so the existing
        registry can be derived from registered tools without changes elsewhere.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "OBJECT",
                "properties": self.parameters,
                "required": list(self.required),
            },
        }


_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    description: str,
    parameters: dict | None = None,
    required: list | None = None,
) -> Callable[[Handler], Handler]:
    """Register a function as a FLINT tool.

    The schema is validated at import time so a malformed tool fails fast
    instead of breaking the Gemini session at runtime (see issue #8, open
    question 3). Returns the original function unchanged, so it stays directly
    callable and testable.
    """
    parameters = parameters or {}
    required = required or []

    if not isinstance(name, str) or not name.strip():
        raise ValueError("tool name must be a non-empty string")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"tool {name!r}: description must be a non-empty string")
    if not isinstance(parameters, dict):
        raise ValueError(f"tool {name!r}: parameters must be a dict")
    if not isinstance(required, (list, tuple)):
        raise ValueError(f"tool {name!r}: required must be a list")

    missing = [r for r in required if r not in parameters]
    if missing:
        raise ValueError(
            f"tool {name!r}: required names not in parameters: {missing}"
        )
    if name in _REGISTRY:
        raise ValueError(f"tool {name!r} is already registered")

    def decorate(func: Handler) -> Handler:
        _REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            required=list(required),
            handler=func,
        )
        return func

    return decorate


def get(name: str) -> ToolSpec | None:
    """Return the registered ToolSpec, or None."""
    return _REGISTRY.get(name)


def all_specs() -> list[ToolSpec]:
    """Every registered tool, in registration order."""
    return list(_REGISTRY.values())


def declarations() -> list[dict]:
    """Gemini function declarations for every registered tool."""
    return [spec.declaration() for spec in _REGISTRY.values()]


def dispatch(name: str, args: dict, ctx: Ctx) -> Any:
    """Run a registered tool's handler. Raises KeyError if it is unknown."""
    spec = _REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"no tool registered under {name!r}")
    return spec.handler(args, ctx)


def discover(package: str = "actions") -> int:
    """Import every submodule of ``package`` so their @tool decorators run.

    Returns the number of submodules imported. Import errors from optional
    dependencies (for example Playwright-backed tools) are surfaced to the
    caller rather than silently swallowed, so a broken tool is visible.
    """
    pkg = importlib.import_module(package)
    count = 0
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"{package}.{info.name}")
        count += 1
    return count


def _clear_for_tests() -> None:
    """Reset the registry. Test-only helper."""
    _REGISTRY.clear()


if __name__ == "__main__":
    # Standalone self-test: no app, no external deps.
    _clear_for_tests()

    @tool(
        name="echo",
        description="Echoes a message back.",
        parameters={"text": {"type": "STRING", "description": "What to echo"}},
        required=["text"],
    )
    def _echo(args, ctx):
        prefix = ctx.extra.get("prefix", "")
        return f"{prefix}{args['text']}"

    assert get("echo") is not None
    decl = get("echo").declaration()
    assert decl == {
        "name": "echo",
        "description": "Echoes a message back.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"text": {"type": "STRING", "description": "What to echo"}},
            "required": ["text"],
        },
    }, decl

    ctx = Ctx(extra={"prefix": "> "})
    assert dispatch("echo", {"text": "hi"}, ctx) == "> hi"

    # Duplicate registration fails fast.
    try:
        tool(name="echo", description="dup")(lambda a, c: None)
        raise AssertionError("expected duplicate registration to fail")
    except ValueError:
        pass

    # Malformed schema fails fast.
    try:
        tool(name="bad", description="x", parameters={"a": {}}, required=["b"])
        raise AssertionError("expected required-not-in-parameters to fail")
    except ValueError:
        pass

    print("core.tools self-test passed")
