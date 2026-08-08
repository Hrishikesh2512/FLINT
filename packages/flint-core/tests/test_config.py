import json

import pytest

from flint_core.config import FlintSettings, build_gateway, load_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_env_wins_over_legacy_json(tmp_path, monkeypatch):
    legacy = tmp_path / "api_keys.json"
    legacy.write_text(json.dumps({"gemini_api_key": "from-json"}), encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    settings = load_settings(legacy_json=legacy)
    assert settings.gemini_api_key == "from-env"


def test_legacy_json_fallback(tmp_path):
    legacy = tmp_path / "api_keys.json"
    legacy.write_text(
        json.dumps({"gemini_api_key": "g", "openrouter_api_key": "o"}), encoding="utf-8"
    )
    settings = load_settings(legacy_json=legacy)
    assert settings.gemini_api_key == "g"
    assert settings.openrouter_api_key == "o"
    assert settings.configured_providers == ("gemini", "openrouter")


def test_missing_everything_is_empty(tmp_path):
    settings = load_settings(legacy_json=tmp_path / "nope.json")
    assert settings.configured_providers == ()


def test_corrupt_legacy_json_ignored(tmp_path):
    legacy = tmp_path / "api_keys.json"
    legacy.write_text("{broken", encoding="utf-8")
    assert load_settings(legacy_json=legacy).configured_providers == ()


def test_build_gateway_provider_order():
    settings = FlintSettings(
        gemini_api_key="g", openrouter_api_key="o", groq_api_key="q"
    )
    gateway = build_gateway(settings)
    names = [p.name for p in gateway._providers]
    assert names == ["gemini", "groq", "openrouter"]


def test_build_gateway_requires_a_key():
    with pytest.raises(ValueError, match="no LLM provider configured"):
        build_gateway(FlintSettings())


# ── routing catalogue ───────────────────────────────────────────────────────
def test_the_router_covers_only_configured_providers():
    from flint_core.config import FlintSettings, build_router

    router = build_router(FlintSettings(gemini_api_key="g"))
    assert {spec.provider for spec in router} == {"gemini"}


def test_routing_degrades_gracefully_to_one_provider():
    """A single-provider device still gets sensible chat/reasoning splits."""
    from flint_core.config import FlintSettings, build_router
    from flint_core.llm.routing import Task

    router = build_router(FlintSettings(gemini_api_key="g"))
    assert router.pick(Task.CHAT).model == "gemini-2.5-flash-lite"    # cheap+fast
    assert router.pick(Task.REASONING).model == "gemini-2.5-flash"    # strongest


def test_a_stronger_provider_takes_over_reasoning():
    from flint_core.config import FlintSettings, build_router
    from flint_core.llm.routing import Task

    router = build_router(FlintSettings(gemini_api_key="g", groq_api_key="q"))
    picked = router.pick(Task.REASONING)
    assert (picked.provider, picked.model) == ("groq", "llama-3.3-70b-versatile")


def test_no_providers_means_an_empty_router():
    from flint_core.config import FlintSettings, build_router

    assert len(build_router(FlintSettings())) == 0


def test_the_built_gateway_carries_a_router():
    from flint_core.config import FlintSettings, build_gateway

    gateway = build_gateway(FlintSettings(gemini_api_key="g"))
    assert gateway._router is not None
    assert len(gateway._router) > 0
