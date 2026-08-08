"""Typed settings + the standard gateway factory.

Key resolution order: environment variables win, then the legacy
config/api_keys.json (the v1 Windows app's per-machine file), then nothing.
A missing key simply means that provider isn't offered.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from flint_core.llm.base import Transport
from flint_core.llm.gateway import LLMGateway
from flint_core.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatProvider,
)
from flint_core.llm.routing import ModelSpec, Router, Task, Tier
from flint_core.llm.transport import requests_transport

# Free-tier pool used when only an OpenRouter key is configured. Order matters:
# strongest first, small fast ones last as a final safety net.
OPENROUTER_FREE_MODELS: tuple[str, ...] = (
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-coder:free",
    "google/gemma-3-27b-it:free",
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "google/gemma-3-4b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
)

OPENROUTER_FREE_VISION_MODELS: tuple[str, ...] = (
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-3-27b-it:free",
)

_ENV_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
}

_LEGACY_JSON_KEYS = {
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "groq": "groq_api_key",
}


@dataclass(frozen=True)
class FlintSettings:
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    @property
    def configured_providers(self) -> tuple[str, ...]:
        return tuple(
            name for name in _ENV_KEYS if getattr(self, f"{name}_api_key", "")
        )


def load_settings(legacy_json: Path | None = None) -> FlintSettings:
    legacy: dict = {}
    if legacy_json is not None and legacy_json.exists():
        try:
            legacy = json.loads(legacy_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            legacy = {}

    def resolve(name: str) -> str:
        env_value = os.environ.get(_ENV_KEYS[name], "").strip()
        if env_value:
            return env_value
        return str(legacy.get(_LEGACY_JSON_KEYS[name], "") or "").strip()

    return FlintSettings(**{f"{name}_api_key": resolve(name) for name in _ENV_KEYS})


#: Default routing hints for the models `build_gateway` actually wires up.
#:
#: Tiers are coarse and deliberately uncontroversial — the "lite"/"mini"/
#: "instant" variants are the cheap tier, the rest are standard. `good_at` is
#: a starting guess, not a benchmark result; getting one wrong costs ordering
#: and nothing else, because the router never removes a model from the field.
#: Override the whole thing by passing your own Router to build_gateway.
_DEFAULT_ROUTING: tuple[tuple[str, str, tuple[str, ...], str, bool, bool], ...] = (
    # provider,     model,                        good_at, tier, fast, vision
    ("gemini", "gemini-2.5-flash",
     (Task.CHAT, Task.VISION, Task.CODE), Tier.STANDARD, True, True),
    ("gemini", "gemini-2.5-flash-lite",
     (Task.CHAT, Task.BULK), Tier.CHEAP, True, False),
    ("groq", "llama-3.3-70b-versatile",
     (Task.REASONING, Task.CODE), Tier.STANDARD, True, False),
    ("groq", "llama-3.1-8b-instant",
     (Task.BULK,), Tier.CHEAP, True, False),
    ("openai", "gpt-4o-mini",
     (Task.CHAT, Task.BULK, Task.VISION), Tier.CHEAP, True, True),
    ("anthropic", "claude-haiku-4-5-20251001",
     (Task.CHAT, Task.CODE), Tier.STANDARD, True, False),
)


def build_router(settings: FlintSettings) -> Router:
    """Routing catalogue covering whichever providers are configured.

    Entries for absent providers are harmless — the gateway skips a routed
    model it has no provider for — but filtering keeps the table honest when
    it is logged or shown in the console.
    """
    configured = set(settings.configured_providers)
    return Router([
        ModelSpec(provider=provider, model=model,
                  good_at=frozenset(good_at), tier=tier, fast=fast, vision=vision)
        for provider, model, good_at, tier, fast, vision in _DEFAULT_ROUTING
        if provider in configured
    ])


def build_gateway(
    settings: FlintSettings,
    transport: Transport = requests_transport,
    rate_limit_cooldown: float = 60.0,
    router: Router | None = None,
) -> LLMGateway:
    """The standard provider chain: Gemini → Groq → OpenAI → Anthropic → OpenRouter.

    Only configured providers are included; at least one key is required.
    """
    providers = []
    if settings.gemini_api_key:
        providers.append(GeminiProvider(settings.gemini_api_key, transport=transport))
    if settings.groq_api_key:
        providers.append(
            OpenAICompatProvider(
                "groq",
                "https://api.groq.com/openai/v1",
                settings.groq_api_key,
                models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
                transport=transport,
            )
        )
    if settings.openai_api_key:
        providers.append(
            OpenAICompatProvider(
                "openai",
                "https://api.openai.com/v1",
                settings.openai_api_key,
                models=("gpt-4o-mini",),
                transport=transport,
            )
        )
    if settings.anthropic_api_key:
        providers.append(AnthropicProvider(settings.anthropic_api_key, transport=transport))
    if settings.openrouter_api_key:
        providers.append(
            OpenAICompatProvider(
                "openrouter",
                "https://openrouter.ai/api/v1",
                settings.openrouter_api_key,
                models=OPENROUTER_FREE_MODELS,
                vision_models=OPENROUTER_FREE_VISION_MODELS,
                supports_json_response=False,  # free models often reject response_format
                extra_headers={"X-Title": "FLINT"},
                transport=transport,
            )
        )
    if not providers:
        raise ValueError(
            "no LLM provider configured — set GEMINI_API_KEY (or another provider key) "
            "in the environment or config/api_keys.json"
        )
    return LLMGateway(providers, rate_limit_cooldown=rate_limit_cooldown,
                      router=router if router is not None else build_router(settings))
