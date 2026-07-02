"""The tool registry — single source of truth for every FLINT capability.

One @tool decoration produces everything that v1 kept in five hand-synced
places: the model-facing declaration (Gemini and OpenAI formats), the
dispatch table, the planner's tool documentation, and platform gating.

    registry = ToolRegistry()

    @registry.tool(
        description="Current weather for a city.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
        platforms=("windows",),
    )
    def weather_report(city: str) -> str:
        ...

    registry.dispatch("weather_report", {"city": "Pune"})
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

ANY_PLATFORM = "any"

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class ToolError(Exception):
    """Base for registry errors."""


class UnknownToolError(ToolError):
    pass


class InvalidArgumentsError(ToolError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema ({} => no parameters)
    handler: Callable[..., Any]
    platforms: tuple[str, ...] = (ANY_PLATFORM,)

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ToolError(f"tool name must be an identifier: {self.name!r}")
        if not self.description.strip():
            raise ToolError(f"tool {self.name!r} needs a description")
        if self.parameters and self.parameters.get("type") != "object":
            raise ToolError(f"tool {self.name!r}: parameters schema must be type=object")

    def available_on(self, platform: str) -> bool:
        return ANY_PLATFORM in self.platforms or platform in self.platforms

    def validate_args(self, args: dict[str, Any]) -> None:
        properties: dict = self.parameters.get("properties", {}) if self.parameters else {}
        required: list = self.parameters.get("required", []) if self.parameters else []

        missing = [key for key in required if args.get(key) in (None, "")]
        if missing:
            raise InvalidArgumentsError(f"{self.name}: missing required argument(s): {missing}")

        unknown = [key for key in args if key not in properties]
        if unknown:
            raise InvalidArgumentsError(f"{self.name}: unknown argument(s): {unknown}")

        for key, value in args.items():
            if value is None:
                continue
            expected = _JSON_TYPES.get(properties[key].get("type", ""))
            if expected and not isinstance(value, expected):
                raise InvalidArgumentsError(
                    f"{self.name}: argument {key!r} should be "
                    f"{properties[key]['type']}, got {type(value).__name__}"
                )


class ToolRegistry:
    def __init__(self, platform: str = ANY_PLATFORM):
        self._platform = platform
        self._tools: dict[str, ToolSpec] = {}

    # ── registration ─────────────────────────────────────────────────────────
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ToolError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def tool(
        self,
        name: str | None = None,
        *,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        platforms: tuple[str, ...] = (ANY_PLATFORM,),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                ToolSpec(
                    name=name or func.__name__,
                    description=(description or (func.__doc__ or "")).strip(),
                    parameters=parameters or {},
                    handler=func,
                    platforms=platforms,
                )
            )
            return func

        return decorator

    # ── lookup ───────────────────────────────────────────────────────────────
    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._tools.values())

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"unknown tool: {name!r}") from None

    def names(self, platform: str | None = None) -> list[str]:
        return [s.name for s in self._for_platform(platform)]

    def _for_platform(self, platform: str | None) -> list[ToolSpec]:
        platform = platform or self._platform
        return [s for s in self._tools.values() if s.available_on(platform)]

    # ── execution ────────────────────────────────────────────────────────────
    def dispatch(self, name: str, args: dict[str, Any] | None = None, **extra: Any) -> Any:
        """Validate args against the tool's schema and invoke its handler.

        `extra` kwargs (ui hooks, speak callbacks...) are passed through only
        if the handler accepts them — platform runtimes can offer context
        without every tool having to declare it.
        """
        spec = self.get(name)
        args = dict(args or {})
        spec.validate_args(args)
        if extra:
            import inspect

            accepted = inspect.signature(spec.handler).parameters
            has_var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values()
            )
            if not has_var_kw:
                extra = {k: v for k, v in extra.items() if k in accepted}
        return spec.handler(**args, **extra)

    # ── model-facing schemas ─────────────────────────────────────────────────
    def gemini_declarations(self, platform: str | None = None) -> list[dict[str, Any]]:
        """function_declarations for the Gemini (Live) API."""
        out = []
        for spec in self._for_platform(platform):
            decl: dict[str, Any] = {"name": spec.name, "description": spec.description}
            if spec.parameters:
                decl["parameters"] = spec.parameters
            out.append(decl)
        return out

    def openai_tools(self, platform: str | None = None) -> list[dict[str, Any]]:
        """tools=[...] entries for OpenAI-style chat completions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters
                    or {"type": "object", "properties": {}},
                },
            }
            for spec in self._for_platform(platform)
        ]

    def planner_documentation(self, platform: str | None = None) -> str:
        """Human/planner-readable tool list — generated, never hand-written."""
        blocks = []
        for spec in self._for_platform(platform):
            lines = [f"{spec.name}", f"  {spec.description}"]
            properties = spec.parameters.get("properties", {}) if spec.parameters else {}
            required = set(spec.parameters.get("required", [])) if spec.parameters else set()
            for key, schema in properties.items():
                marker = " (required)" if key in required else ""
                desc = schema.get("description", "")
                entry = f"  - {key}: {schema.get('type', 'any')}{marker} — {desc}"
                lines.append(entry.rstrip(" —"))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
