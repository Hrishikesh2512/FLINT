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
