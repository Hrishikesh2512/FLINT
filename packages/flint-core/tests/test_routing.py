"""Routing: the right model for the kind of work, without losing failover."""

from __future__ import annotations

import pytest

from flint_core.llm.routing import (
    ModelSpec,
    Router,
    Task,
    Tier,
    catalogue_from_config,
    classify,
)


def catalogue():
    """A realistic spread: a cheap fast one, a workhorse, a strong slow one."""
    return Router([
        ModelSpec("gemini", "flash-lite", good_at=frozenset({Task.CHAT, Task.BULK}),
                  tier=Tier.CHEAP, fast=True),
        ModelSpec("gemini", "flash", good_at=frozenset({Task.CHAT, Task.VISION}),
                  tier=Tier.STANDARD, fast=True, vision=True),
        ModelSpec("anthropic", "big", good_at=frozenset({Task.REASONING, Task.CODE}),
                  tier=Tier.PREMIUM),
    ])


# ── ordering by what the task optimises for ─────────────────────────────────
def test_chat_takes_the_fastest():
    assert catalogue().pick(Task.CHAT).model == "flash-lite"


def test_reasoning_takes_the_strongest():
    assert catalogue().pick(Task.REASONING).model == "big"


def test_code_takes_the_strongest():
    assert catalogue().pick(Task.CODE).model == "big"


def test_bulk_takes_the_cheapest():
    assert catalogue().pick(Task.BULK).model == "flash-lite"


def test_a_model_naming_the_task_beats_a_general_one():
    router = Router([
        ModelSpec("x", "general", tier=Tier.PREMIUM),          # good_at empty
        ModelSpec("x", "specialist", good_at=frozenset({Task.CODE}),
                  tier=Tier.CHEAP),
    ])
    assert router.pick(Task.CODE).model == "specialist"


def test_a_general_model_is_still_usable_for_anything():
    router = Router([ModelSpec("x", "general")])
    assert router.pick(Task.CODE).model == "general"
    assert router.pick(Task.BULK).model == "general"


# ── filters are preferences, never promises ─────────────────────────────────
def test_vision_needs_a_model_that_can_see():
    assert catalogue().pick(Task.VISION, need_vision=True).model == "flash"


def test_a_tier_ceiling_keeps_the_expensive_one_out():
    picked = catalogue().pick(Task.REASONING, max_tier=Tier.STANDARD)
    assert picked.model != "big"


def test_an_impossible_filter_falls_back_rather_than_failing():
    """A worse model beats no answer — this is a router, not a gate."""
    router = Router([ModelSpec("x", "only", tier=Tier.PREMIUM)])
    assert router.pick(Task.CHAT, max_tier=Tier.CHEAP).model == "only"
    assert router.pick(Task.VISION, need_vision=True).model == "only"


def test_an_empty_catalogue_picks_nothing():
    assert Router().pick(Task.CHAT) is None
    assert Router().candidates(Task.CHAT) == []


def test_candidates_never_drop_a_model():
    """Routing orders; it must not shrink the field the gateway can fall to."""
    router = catalogue()
    assert len(router.candidates(Task.CHAT)) == len(router)


def test_describe_shows_the_whole_routing_table():
    described = catalogue().describe()
    assert "chat" in described and "flash-lite" in described
    assert "reasoning" in described and "big" in described


# ── automatic classification ────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("haan yaar", Task.CHAT),
    ("kya scene hai", Task.CHAT),
    ("what's the time", Task.CHAT),
    ("why is this deadlocking when two threads hit it", Task.REASONING),
    ("explain the trade-offs between these two designs", Task.REASONING),
    ("fix this traceback in main.py", Task.CODE),
    ("write a function that parses the config", Task.CODE),
    ("classify this message as urgent or not", Task.BULK),
    ("return only json with the fields", Task.BULK),
])
def test_classification_of_typical_requests(text, expected):
    assert classify(text) == expected


def test_an_image_always_means_vision():
    assert classify("what is this", has_image=True) == Task.VISION
    assert classify("fix this traceback", has_image=True) == Task.VISION


def test_empty_input_is_chat():
    assert classify("") == Task.CHAT
    assert classify("   ") == Task.CHAT


def test_a_long_explanation_is_reasoning_even_without_keywords():
    long_problem = (
        "so the thing is when I plug the headset in it works for a bit and "
        "then the audio just stops, but only when the phone is also connected, "
        "and restarting the service brings it back for maybe ten minutes"
    )
    assert classify(long_problem) == Task.REASONING


def test_mechanical_markers_beat_code_markers():
    """Sending a premium model to label a JSON blob is the waste that matters."""
    assert classify("classify this json payload as valid or invalid") == Task.BULK


@pytest.mark.parametrize("text", [
    "why is this deadlocking when two threads hit it",
    "why is this happening every single morning without fail",
    "does this look right to you or should I change the approach",
])
def test_ordinary_questions_are_not_mistaken_for_mechanical_work(text):
    """Regression: bare "is this"/"does this" once sent real reasoning to the
    cheapest model on the shelf, because they appear in ordinary speech."""
    assert classify(text) != Task.BULK


# ── config ──────────────────────────────────────────────────────────────────
def test_a_catalogue_can_be_built_from_config():
    router = catalogue_from_config([
        {"provider": "gemini", "model": "flash", "good_at": ["chat"],
         "tier": "cheap", "fast": True},
        {"provider": "anthropic", "model": "big", "good_at": ["reasoning"],
         "tier": "premium"},
    ])
    assert len(router) == 2
    assert router.pick(Task.REASONING).model == "big"


def test_a_bad_config_entry_is_skipped_not_fatal():
    """A typo in the model table must not stop the assistant booting."""
    router = catalogue_from_config([
        {"provider": "", "model": "nameless"},               # no provider
        {"provider": "x", "model": "y", "good_at": ["nonsense"]},   # bad task
        {"provider": "gemini", "model": "good", "good_at": ["chat"]},
    ])
    assert [s.model for s in router] == ["good"]


def test_an_unknown_task_in_a_spec_is_rejected():
    with pytest.raises(ValueError, match="unknown task"):
        ModelSpec("x", "y", good_at=frozenset({"telepathy"}))


def test_a_spec_needs_a_provider_and_a_model():
    with pytest.raises(ValueError, match="needs a provider and a model"):
        ModelSpec("", "y")
