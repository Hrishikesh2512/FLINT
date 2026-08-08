import pytest
from conftest import FakeTransport, gemini_ok, http, openai_ok

from flint_core.llm.gateway import AllProvidersFailedError, LLMGateway
from flint_core.llm.providers import GeminiProvider, OpenAICompatProvider


def gateway_with(*providers, clock=None):
    kwargs = {"clock": clock} if clock else {}
    return LLMGateway(list(providers), **kwargs)


def test_first_provider_wins():
    gemini = GeminiProvider("k", transport=FakeTransport(gemini_ok("from gemini")))
    fallback = OpenAICompatProvider("or", "https://x", "k", models=("m",),
                                    transport=FakeTransport())
    response = gateway_with(gemini, fallback).chat("hi")
    assert response.text == "from gemini"
    assert response.provider == "gemini"


def test_falls_through_models_then_providers():
    # gemini: both models fail; openrouter answers.
    gemini = GeminiProvider(
        "k", models=("a", "b"), transport=FakeTransport(http(500), http(500))
    )
    router = OpenAICompatProvider(
        "openrouter", "https://x", "k", models=("m",),
        transport=FakeTransport(openai_ok("saved")),
    )
    response = gateway_with(gemini, router).chat("hi")
    assert (response.provider, response.text) == ("openrouter", "saved")


def test_model_hint_reorders_within_provider():
    transport = FakeTransport(gemini_ok("ok"))
    gemini = GeminiProvider("k", models=("flash", "lite"), transport=transport)
    gateway_with(gemini).chat("hi", model="lite")
    assert "lite:generateContent" in transport.requests[0]["url"]


def test_rate_limit_cooldown_and_recovery(fake_clock):
    transport = FakeTransport(http(429), gemini_ok("recovered"))
    gemini = GeminiProvider("k", models=("m",), transport=transport)
    backup = OpenAICompatProvider(
        "or", "https://x", "k", models=("bm",),
        transport=FakeTransport(openai_ok("backup1"), openai_ok("backup2")),
    )
    gateway = gateway_with(gemini, backup, clock=fake_clock)

    assert gateway.chat("1").provider == "or"      # gemini 429 -> cooldown, backup answers
    assert gateway.chat("2").provider == "or"      # gemini still cooling: not even probed
    fake_clock.advance(61)
    assert gateway.chat("3").text == "recovered"   # cooldown expired -> gemini again
    assert len(transport.requests) == 2            # gemini probed exactly twice


def test_all_failed_raises_with_details():
    gemini = GeminiProvider("k", models=("m",), transport=FakeTransport(http(500)))
    with pytest.raises(AllProvidersFailedError, match="HTTP 500"):
        gateway_with(gemini).chat("hi")


def test_prompt_xor_messages_enforced():
    gemini = GeminiProvider("k", transport=FakeTransport())
    gateway = gateway_with(gemini)
    with pytest.raises(ValueError):
        gateway.chat()
    with pytest.raises(ValueError):
        gateway.chat("hi", messages=[])


def test_chat_json_parses_fenced_output():
    text = '```json\n{"answer": 42}\n```'
    gemini = GeminiProvider("k", transport=FakeTransport(gemini_ok(text)))
    assert gateway_with(gemini).chat_json("q") == {"answer": 42}


def test_chat_json_raises_on_garbage():
    gemini = GeminiProvider("k", transport=FakeTransport(gemini_ok("not json at all")))
    with pytest.raises(ValueError, match="unparseable JSON"):
        gateway_with(gemini).chat_json("q")


def test_vision_routes_to_capable_provider():
    # openrouter has no vision models configured here; gemini answers.
    router = OpenAICompatProvider("or", "https://x", "k", models=("m",),
                                  transport=FakeTransport())
    transport = FakeTransport(gemini_ok("x=100,y=200"))
    gemini = GeminiProvider("k", transport=transport)
    response = gateway_with(router, gemini).vision("find the button", "aW1n")
    assert response.text == "x=100,y=200"
    parts = transport.requests[0]["payload"]["contents"][0]["parts"]
    assert parts[0]["inline_data"] == {"mime_type": "image/png", "data": "aW1n"}


def test_vision_fails_cleanly_without_capable_provider():
    router = OpenAICompatProvider("or", "https://x", "k", models=("m",),
                                  transport=FakeTransport())
    with pytest.raises(AllProvidersFailedError, match="no vision-capable"):
        gateway_with(router).vision("q", "aW1n")


def test_system_prompt_reaches_provider():
    transport = FakeTransport(gemini_ok("ok"))
    gemini = GeminiProvider("k", transport=transport)
    gateway_with(gemini).chat("hi", system="you are a test")
    payload = transport.requests[0]["payload"]
    assert payload["system_instruction"]["parts"][0]["text"] == "you are a test"


# ── routing: the right model for the kind of work ───────────────────────────
def routed_gateway(*providers, catalogue=()):
    from flint_core.llm.routing import Router

    return LLMGateway(list(providers), router=Router(catalogue))


def test_routing_prefers_the_model_the_task_calls_for():
    from flint_core.llm.routing import ModelSpec, Task, Tier

    transport = FakeTransport(gemini_ok("thought hard"))
    gemini = GeminiProvider("k", models=("flash", "pro"), transport=transport)
    gateway = routed_gateway(gemini, catalogue=[
        ModelSpec("gemini", "flash", good_at=frozenset({Task.CHAT}),
                  tier=Tier.CHEAP, fast=True),
        ModelSpec("gemini", "pro", good_at=frozenset({Task.REASONING}),
                  tier=Tier.PREMIUM),
    ])
    gateway.chat("why", task=Task.REASONING)
    assert "pro:generateContent" in transport.requests[0]["url"]


def test_routing_picks_the_cheap_one_for_chat():
    from flint_core.llm.routing import ModelSpec, Task, Tier

    transport = FakeTransport(gemini_ok("haan"))
    gemini = GeminiProvider("k", models=("flash", "pro"), transport=transport)
    gateway = routed_gateway(gemini, catalogue=[
        ModelSpec("gemini", "pro", good_at=frozenset({Task.REASONING}),
                  tier=Tier.PREMIUM),
        ModelSpec("gemini", "flash", good_at=frozenset({Task.CHAT}),
                  tier=Tier.CHEAP, fast=True),
    ])
    gateway.chat("kya scene hai", task=Task.CHAT)
    assert "flash:generateContent" in transport.requests[0]["url"]


def test_auto_classifies_from_the_prompt():
    from flint_core.llm.routing import ModelSpec, Task, Tier

    transport = FakeTransport(gemini_ok("fixed"))
    gemini = GeminiProvider("k", models=("flash", "pro"), transport=transport)
    gateway = routed_gateway(gemini, catalogue=[
        ModelSpec("gemini", "flash", good_at=frozenset({Task.CHAT}),
                  tier=Tier.CHEAP, fast=True),
        ModelSpec("gemini", "pro", good_at=frozenset({Task.CODE}),
                  tier=Tier.PREMIUM),
    ])
    gateway.chat("fix this traceback in main.py", task="auto")
    assert "pro:generateContent" in transport.requests[0]["url"]


def test_routing_never_costs_availability():
    """The routed model is down: the request must still be answered."""
    from flint_core.llm.routing import ModelSpec, Task, Tier

    transport = FakeTransport(http(500), gemini_ok("fell back"))
    gemini = GeminiProvider("k", models=("pro", "flash"), transport=transport)
    gateway = routed_gateway(gemini, catalogue=[
        ModelSpec("gemini", "pro", good_at=frozenset({Task.REASONING}),
                  tier=Tier.PREMIUM),
        ModelSpec("gemini", "flash", good_at=frozenset({Task.CHAT}),
                  tier=Tier.CHEAP, fast=True),
    ])
    assert gateway.chat("why", task=Task.REASONING).text == "fell back"


def test_a_routed_model_the_host_does_not_have_is_skipped():
    """A catalogue entry for a provider that isn't configured must not break it."""
    from flint_core.llm.routing import ModelSpec, Task

    gemini = GeminiProvider("k", models=("flash",),
                            transport=FakeTransport(gemini_ok("still fine")))
    gateway = routed_gateway(gemini, catalogue=[
        ModelSpec("anthropic", "absent", good_at=frozenset({Task.REASONING})),
    ])
    assert gateway.chat("why", task=Task.REASONING).text == "still fine"


def test_without_a_task_behaviour_is_unchanged():
    transport = FakeTransport(gemini_ok("as before"))
    gemini = GeminiProvider("k", models=("flash", "pro"), transport=transport)
    from flint_core.llm.routing import ModelSpec, Task, Tier

    gateway = routed_gateway(gemini, catalogue=[
        ModelSpec("gemini", "pro", good_at=frozenset({Task.CHAT}),
                  tier=Tier.PREMIUM),
    ])
    gateway.chat("hi")                      # no task= at all
    assert "flash:generateContent" in transport.requests[0]["url"]


def test_a_gateway_without_a_router_ignores_the_task():
    transport = FakeTransport(gemini_ok("fine"))
    gemini = GeminiProvider("k", models=("flash",), transport=transport)
    assert gateway_with(gemini).chat("hi", task="reasoning").text == "fine"
