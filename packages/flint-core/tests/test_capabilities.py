"""Capabilities: tools and instructions ship together, or not at all."""

from __future__ import annotations

import pytest

from flint_core.capabilities import Capability, CapabilitySet


def tool_adder(name: str):
    def register(registry):
        registry.tool(name, description=f"Does {name}.")(lambda: name)

    return register


def music(available=True, **kw):
    return Capability(
        name="music", summary="Play and skip songs.",
        prompt="MUSIC CONTROL: call play_music, never just say you did.",
        register=tool_adder("play_music"), available=available, **kw)


def chess(available=True, **kw):
    return Capability(
        name="chess", summary="Play chess by voice.",
        prompt="CHESS: the engine owns the board, never invent moves.",
        register=tool_adder("play_chess_move"), available=available, **kw)


# ── the core promise ────────────────────────────────────────────────────────
def test_an_active_capability_contributes_both_tools_and_prompt():
    caps = CapabilitySet([music()])
    assert caps.build_registry().names() == ["play_music"]
    assert "MUSIC CONTROL" in caps.render_prompt()


def test_an_unavailable_capability_contributes_neither():
    """The whole point: no TV configured means no TV tools AND no TV prompt."""
    caps = CapabilitySet([music(available=False)])
    assert caps.build_registry().names() == []
    assert caps.render_prompt() == ""


def test_a_mixed_set_ships_only_what_is_available():
    caps = CapabilitySet([music(), chess(available=False)])
    assert caps.build_registry().names() == ["play_music"]
    prompt = caps.render_prompt()
    assert "MUSIC CONTROL" in prompt
    assert "CHESS" not in prompt


# ── prompt composition ──────────────────────────────────────────────────────
def test_prompt_order_is_explicit_and_stable():
    caps = CapabilitySet([
        Capability(name="late", summary="s", prompt="LATE", order=90),
        Capability(name="early", summary="s", prompt="EARLY", order=10),
        Capability(name="middle", summary="s", prompt="MIDDLE", order=50),
    ])
    assert caps.render_prompt() == "EARLY\n\nMIDDLE\n\nLATE"


def test_equal_order_keeps_insertion_order():
    """A byte-stable prompt matters for caching and for diffing what she was told."""
    caps = CapabilitySet([
        Capability(name="first", summary="s", prompt="FIRST"),
        Capability(name="second", summary="s", prompt="SECOND"),
    ])
    assert caps.render_prompt() == "FIRST\n\nSECOND"


def test_a_tools_only_capability_adds_no_blank_paragraph():
    caps = CapabilitySet([
        Capability(name="quiet", summary="s", register=tool_adder("quiet_tool")),
        music(),
    ])
    assert caps.render_prompt().count("\n\n") == 0
    assert "quiet_tool" in caps.build_registry().names()


def test_an_instruction_only_capability_needs_no_tools():
    caps = CapabilitySet([
        Capability(name="manners", summary="How to behave.", prompt="BE KIND.")])
    assert caps.render_prompt() == "BE KIND."
    assert caps.build_registry().names() == []


def test_placeholders_are_substituted():
    caps = CapabilitySet([
        Capability(name="greet", summary="s",
                   prompt="Call him {user_name}, never 'user'.")])
    assert caps.render_prompt(user_name="Tushar") == "Call him Tushar, never 'user'."


def test_unsubstituted_placeholders_are_left_alone():
    caps = CapabilitySet([Capability(name="x", summary="s", prompt="{unknown}")])
    assert caps.render_prompt(user_name="Tushar") == "{unknown}"


# ── bookkeeping ─────────────────────────────────────────────────────────────
def test_duplicate_names_are_rejected():
    caps = CapabilitySet([music()])
    with pytest.raises(ValueError, match="duplicate capability"):
        caps.add(music())


def test_a_capability_needs_a_name_and_a_summary():
    with pytest.raises(ValueError, match="needs a name"):
        Capability(name="  ", summary="s")
    with pytest.raises(ValueError, match="needs a summary"):
        Capability(name="x", summary=" ")


def test_names_lists_only_the_active_ones():
    caps = CapabilitySet([music(), chess(available=False)])
    assert caps.names() == ["music"]
    assert [c.name for c in caps.inactive()] == ["chess"]


def test_membership_and_length_cover_everything_declared():
    caps = CapabilitySet([music(), chess(available=False)])
    assert "chess" in caps and len(caps) == 2


def test_permissions_are_collected_from_active_capabilities_only():
    caps = CapabilitySet([
        music(permissions=("audio",)),
        chess(available=False, permissions=("nothing",)),
        Capability(name="shell", summary="s", permissions=("shell", "files")),
    ])
    assert caps.permissions() == ("audio", "files", "shell")


def test_describe_marks_what_is_on_and_off():
    described = CapabilitySet([music(), chess(available=False)]).describe()
    assert "[on ] music — Play and skip songs." in described
    assert "[off] chess — Play chess by voice." in described


def test_two_capabilities_cannot_register_the_same_tool():
    """A real collision should fail loudly at boot, not silently shadow."""
    caps = CapabilitySet([
        Capability(name="a", summary="s", register=tool_adder("shared")),
        Capability(name="b", summary="s", register=tool_adder("shared")),
    ])
    with pytest.raises(Exception, match="duplicate tool"):
        caps.build_registry()


def test_the_registry_carries_the_platform_through():
    caps = CapabilitySet([music()])
    assert caps.build_registry(platform="linux").names() == ["play_music"]
