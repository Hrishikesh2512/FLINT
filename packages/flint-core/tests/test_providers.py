import pytest
from conftest import FakeTransport, anthropic_ok, gemini_ok, http, openai_ok

from flint_core.llm.base import ChatMessage, ProviderError, RateLimitedError
from flint_core.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatProvider,
)

MSGS = [ChatMessage("system", "be brief"), ChatMessage("user", "hi")]


def complete(provider, model):
    return provider.complete(MSGS, model, max_tokens=100, temperature=0.2, json_mode=False)


def test_gemini_payload_and_response():
    transport = FakeTransport(gemini_ok("hello"))
    provider = GeminiProvider("key123", transport=transport)
    assert complete(provider, "gemini-2.5-flash") == "hello"

    req = transport.requests[0]
    assert "gemini-2.5-flash:generateContent" in req["url"]
    assert req["headers"]["x-goog-api-key"] == "key123"
    assert req["payload"]["system_instruction"]["parts"][0]["text"] == "be brief"
    assert req["payload"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_gemini_thinking_disabled_by_default():
    transport = FakeTransport(gemini_ok("ok"))
    GeminiProvider("k", transport=transport).complete(
        MSGS, "m", max_tokens=10, temperature=0, json_mode=False
    )
    config = transport.requests[0]["payload"]["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingBudget": 0}

    transport2 = FakeTransport(gemini_ok("ok"))
    GeminiProvider("k", transport=transport2, thinking_budget=None).complete(
        MSGS, "m", max_tokens=10, temperature=0, json_mode=False
    )
    assert "thinkingConfig" not in transport2.requests[0]["payload"]["generationConfig"]


def test_gemini_json_mode_sets_mime_type():
    transport = FakeTransport(gemini_ok("{}"))
    provider = GeminiProvider("k", transport=transport)
    provider.complete(MSGS, "m", max_tokens=10, temperature=0, json_mode=True)
    config = transport.requests[0]["payload"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"


def test_gemini_assistant_role_maps_to_model():
    transport = FakeTransport(gemini_ok("ok"))
    provider = GeminiProvider("k", transport=transport)
    provider.complete(
        [ChatMessage("user", "a"), ChatMessage("assistant", "b"), ChatMessage("user", "c")],
        "m", max_tokens=10, temperature=0, json_mode=False,
    )
    roles = [c["role"] for c in transport.requests[0]["payload"]["contents"]]
    assert roles == ["user", "model", "user"]


def test_openai_compat_payload_and_response():
    transport = FakeTransport(openai_ok("yo"))
    provider = OpenAICompatProvider(
        "groq", "https://api.groq.com/openai/v1", "gk", models=("llama-x",),
        transport=transport,
    )
    assert complete(provider, "llama-x") == "yo"
    req = transport.requests[0]
    assert req["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer gk"
    assert req["payload"]["messages"][0] == {"role": "system", "content": "be brief"}


def test_openai_compat_json_mode_respects_capability():
    t1 = FakeTransport(openai_ok("{}"))
    yes = OpenAICompatProvider("a", "https://x", "k", models=("m",), transport=t1)
    yes.complete(MSGS, "m", max_tokens=10, temperature=0, json_mode=True)
    assert t1.requests[0]["payload"]["response_format"] == {"type": "json_object"}

    t2 = FakeTransport(openai_ok("{}"))
    no = OpenAICompatProvider(
        "b", "https://x", "k", models=("m",), supports_json_response=False, transport=t2
    )
    no.complete(MSGS, "m", max_tokens=10, temperature=0, json_mode=True)
    assert "response_format" not in t2.requests[0]["payload"]


def test_anthropic_payload_and_response():
    transport = FakeTransport(anthropic_ok("hey"))
    provider = AnthropicProvider("ak", transport=transport)
    assert complete(provider, "claude-haiku-4-5-20251001") == "hey"
    req = transport.requests[0]
    assert req["headers"]["x-api-key"] == "ak"
    assert req["payload"]["system"] == "be brief"
    assert req["payload"]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "make",
    [
        lambda t: GeminiProvider("k", transport=t),
        lambda t: OpenAICompatProvider("p", "https://x", "k", models=("m",), transport=t),
        lambda t: AnthropicProvider("k", transport=t),
    ],
)
def test_429_raises_rate_limited(make):
    provider = make(FakeTransport(http(429)))
    with pytest.raises(RateLimitedError):
        complete(provider, provider.models[0])


@pytest.mark.parametrize("status", [400, 401, 500, 503])
def test_http_errors_raise_provider_error(status):
    provider = GeminiProvider("k", transport=FakeTransport(http(status)))
    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        complete(provider, "m")


def test_empty_response_raises():
    transport = FakeTransport(gemini_ok(""))
    provider = GeminiProvider("k", transport=transport)
    with pytest.raises(ProviderError, match="empty"):
        complete(provider, "m")


def test_empty_api_key_rejected():
    with pytest.raises(ProviderError, match="empty"):
        GeminiProvider("")


def test_grounded_search_sends_google_search_tool():
    transport = FakeTransport(gemini_ok("grounded answer"))
    provider = GeminiProvider("k", transport=transport)
    assert provider.grounded_search("bitcoin price") == "grounded answer"
    payload = transport.requests[0]["payload"]
    assert payload["tools"] == [{"google_search": {}}]
    assert payload["contents"][0]["parts"][0]["text"] == "bitcoin price"
