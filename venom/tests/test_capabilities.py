"""Venom's capability set: she is told about a skill only when she has it.

The drift guard at the bottom is the reason this refactor was worth doing —
it makes "the instructions and the tools disagree" a test failure instead of
something you find out about on hardware.
"""

from __future__ import annotations

import re

import pytest

from flint_core.memory import MemoryStore
from venom.capabilities import build_capabilities
from venom.config import DevConfig, VenomConfig
from venom.live import PERSONA, build_system_instruction
from venom.tools_pi import TimerBoard, build_pi_registry


def config(tmp_path, **kw):
    return VenomConfig(memory_path=tmp_path / "memory.json",
                       gemini_api_key="test-key", **kw)


class Stub:
    """Stands in for any wired-up subsystem — presence is all that matters.

    Registration only ever asks whether a subsystem is there and switched on;
    nothing is actually called, because no tool runs during this test.
    """

    enabled = True


def everything(tmp_path):
    """A device with every optional skill wired up, config gates included."""
    from dataclasses import replace as _replace

    cfg = config(tmp_path)
    cfg = _replace(
        cfg,
        whatsapp=_replace(cfg.whatsapp, token="t"),
        laptop=_replace(cfg.laptop, host="laptop.local"),
        screen=_replace(cfg.screen, host="laptop.local"),
        phone=_replace(cfg.phone, ntfy_topic="topic"),
        documents_dir=str(tmp_path),
        dev=DevConfig(repos=(("flint", str(tmp_path)),),
                      deploy_targets=({"name": "vps", "host": "h"},)),
    )
    wired = dict(music=Stub(), chess=Stub(), sos=Stub(), calendar=Stub(),
                 mailbox=Stub(), receiver=Stub(), notifications=Stub(),
                 jobs=Stub(), watches=Stub(), lights=Stub(), tv=Stub(),
                 connections=Stub(), whatsapp=Stub(), reminders=Stub(),
                 notes=Stub(), lists=Stub(), location=Stub(),
                 projects=Stub(), outcomes=Stub(), archive=Stub())
    return cfg, build_capabilities(cfg, **wired), wired


def full_registry(tmp_path, cfg, wired, audit=None):
    return build_pi_registry(cfg, MemoryStore(tmp_path / "m.json"), TimerBoard(),
                             audit=audit or Stub(), **wired)


# ── the core promise ────────────────────────────────────────────────────────
def test_a_bare_device_is_not_told_about_skills_it_lacks(tmp_path):
    caps = build_capabilities(config(tmp_path))
    prompt = caps.render_prompt(user_name="Tushar")
    for absent in ("MUSIC CONTROL", "CHESS", "EMERGENCY", "BLUETOOTH",
                   "NOTIFICATIONS", "CALENDAR & MAIL"):
        assert absent not in prompt


def test_a_bare_device_still_gets_the_unconditional_rules(tmp_path):
    prompt = build_capabilities(config(tmp_path)).render_prompt(user_name="T")
    assert "TRANSLATION MODE" in prompt      # no hardware needed
    assert "SIGNING OFF" in prompt


def test_each_skill_brings_its_own_instructions(tmp_path):
    _, caps, _ = everything(tmp_path)
    prompt = caps.render_prompt(user_name="Tushar")
    for present in ("MUSIC CONTROL", "CHESS", "EMERGENCY", "CALENDAR & MAIL",
                    "BLUETOOTH HEADSET MODE", "NOTIFICATIONS",
                    "GOING AWAY AND DOING THE WORK", "WATCHING FOR HIM"):
        assert present in prompt


def test_the_prompt_is_substantially_shorter_without_the_hardware(tmp_path):
    """The measured problem: a bare Pi used to carry every skill's paragraph."""
    bare = build_capabilities(config(tmp_path)).render_prompt(user_name="T")
    _, full, _ = everything(tmp_path)
    assert len(bare) < len(full.render_prompt(user_name="T")) / 2


def test_one_skill_at_a_time(tmp_path):
    cfg = config(tmp_path)
    only_music = build_capabilities(cfg, music=Stub()).render_prompt(user_name="T")
    assert "MUSIC CONTROL" in only_music
    assert "CHESS" not in only_music


def test_calendar_or_mail_alone_is_enough(tmp_path):
    cfg = config(tmp_path)
    assert "CALENDAR & MAIL" in build_capabilities(
        cfg, calendar=Stub()).render_prompt(user_name="T")
    assert "CALENDAR & MAIL" in build_capabilities(
        cfg, mailbox=Stub()).render_prompt(user_name="T")


def test_notifications_need_whatsapp_switched_on(tmp_path):
    from dataclasses import replace

    cfg = config(tmp_path)
    off = replace(cfg, whatsapp=replace(cfg.whatsapp, enabled=False))
    caps = build_capabilities(off, notifications=Stub())
    assert "NOTIFICATIONS" not in caps.render_prompt(user_name="T")


# ── composition into the real system prompt ─────────────────────────────────
def test_the_persona_no_longer_hard_codes_skill_instructions():
    for moved in ("MUSIC CONTROL", "CHESS:", "EMERGENCY:", "LAPTOP CONTROL",
                  "BLUETOOTH HEADSET MODE", "TRANSLATION MODE"):
        assert moved not in PERSONA


def test_the_persona_keeps_who_she_is():
    for kept in ("BE HUMAN", "CONFIDENTIALITY", "NEVER SOUND LIKE A HELPDESK",
                 "WORK MODE", "MEMORY:", "SELF-RESPECT"):
        assert kept in PERSONA


def test_the_system_prompt_carries_persona_then_skills(tmp_path):
    cfg, caps, _ = everything(tmp_path)
    prompt = build_system_instruction(cfg, MemoryStore(tmp_path / "m.json"),
                                      capabilities=caps)
    assert "Hinglish" in prompt                    # persona
    assert "MUSIC CONTROL" in prompt               # capability
    assert prompt.index("Hinglish") < prompt.index("MUSIC CONTROL")
    assert prompt.index("MUSIC CONTROL") < prompt.index("[CURRENT DATE & TIME]")


def test_the_system_prompt_still_works_with_no_capabilities(tmp_path):
    cfg = config(tmp_path)
    prompt = build_system_instruction(cfg, MemoryStore(tmp_path / "m.json"))
    assert "Hinglish" in prompt
    assert "MUSIC CONTROL" not in prompt


def test_the_user_name_reaches_the_capability_text(tmp_path):
    from dataclasses import replace

    cfg = config(tmp_path)
    cfg = replace(cfg, voice=replace(cfg.voice, user_name="Tushar"))
    caps = build_capabilities(cfg, sos=Stub())
    prompt = build_system_instruction(cfg, MemoryStore(tmp_path / "m.json"),
                                      capabilities=caps)
    assert "If Tushar asks for emergency help" in prompt
    assert "{user_name}" not in prompt


# ── bookkeeping ─────────────────────────────────────────────────────────────
def test_capabilities_report_what_is_on_and_off(tmp_path):
    caps = build_capabilities(config(tmp_path), music=Stub())
    assert "music" in caps.names()
    assert "chess" not in caps.names()
    assert "[on ] music" in caps.describe()
    assert "[off] chess" in caps.describe()


def test_permissions_are_declared_for_the_active_set(tmp_path):
    _, caps, _ = everything(tmp_path)
    permissions = caps.permissions()
    assert "emergency" in permissions
    assert "home_control" in permissions
    bare = build_capabilities(config(tmp_path)).permissions()
    assert "emergency" not in bare


# ── the drift guard ─────────────────────────────────────────────────────────
# A capability's prompt names the tools she should call. If a tool is renamed
# or dropped and the instruction is not, she is told to call something that
# does not exist — which is exactly the failure this refactor exists to make
# impossible. These identifiers appear in prompt text but are not tool names.
NOT_TOOLS = frozenset({"user_name", "hey_jarvis"})

IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def test_every_tool_named_in_an_instruction_actually_exists(tmp_path):
    cfg, caps, wired = everything(tmp_path)
    known = set(full_registry(tmp_path, cfg, wired).names())

    missing: dict[str, set[str]] = {}
    for capability in caps.active():
        named = set(IDENTIFIER.findall(capability.prompt)) - NOT_TOOLS
        absent = named - known
        if absent:
            missing[capability.name] = absent
    assert not missing, f"instructions name tools that do not exist: {missing}"


def test_the_drift_guard_would_actually_catch_a_rename(tmp_path):
    """Guard the guard: a bogus tool name in a prompt must be detected."""
    from flint_core.capabilities import Capability

    bogus = Capability(name="x", summary="s",
                       prompt="Call totally_made_up_tool when he asks.")
    named = set(IDENTIFIER.findall(bogus.prompt)) - NOT_TOOLS
    assert "totally_made_up_tool" in named


@pytest.mark.parametrize("name", ["music", "chess", "emergency", "jobs",
                                  "watches", "laptop", "notifications"])
def test_every_skill_with_instructions_is_declared(tmp_path, name):
    _, caps, _ = everything(tmp_path)
    assert name in caps


# ── permissions reach the tools ─────────────────────────────────────────────
def test_no_tool_is_left_unclaimed(tmp_path):
    """A tool no capability owns can never be permission-checked.

    If this fails you added a tool without giving it a home: put its name in
    the owning capability's `tools`, or in "core" if it genuinely needs no
    permission at all.
    """
    cfg, caps, wired = everything(tmp_path)
    orphans = caps.unclaimed_tools(full_registry(tmp_path, cfg, wired))
    assert orphans == [], f"tools with no capability: {orphans}"


def test_every_capability_names_only_real_tools(tmp_path):
    """The other direction: a renamed tool must not leave a dangling claim."""
    cfg, caps, wired = everything(tmp_path)
    known = set(full_registry(tmp_path, cfg, wired).names())
    dangling = {c.name: sorted(set(c.tools) - known)
                for c in caps if set(c.tools) - known}
    assert not dangling, f"capabilities claim tools that do not exist: {dangling}"


def test_applying_permissions_reaches_the_sensitive_tools(tmp_path):
    cfg, caps, wired = everything(tmp_path)
    registry = full_registry(tmp_path, cfg, wired)
    caps.apply_permissions(registry)

    assert registry.get("emergency_sos").permissions == (
        "messaging", "location", "emergency")
    assert registry.get("send_whatsapp").permissions == (
        "messaging", "personal_data")
    assert registry.get("laptop_task").permissions == ("remote_control",)
    assert registry.get("current_time").permissions == ()   # genuinely harmless


def test_a_denied_permission_actually_blocks_the_tool(tmp_path):
    """End to end: config says no messaging, so the SOS tool will not fire."""
    from flint_core.permissions import AuditLog, Policy, guarded

    cfg, caps, wired = everything(tmp_path)
    registry = full_registry(tmp_path, cfg, wired)
    caps.apply_permissions(registry)
    audit = AuditLog(tmp_path / "audit.jsonl")
    guard = guarded(registry, Policy(granted=caps.permissions(),
                                     denied=("messaging",)), audit)

    result = guard.dispatch("emergency_sos", {"note": "test"})
    assert "not allowed" in result
    assert "messaging" in result
    refused = audit.recent(refused_only=True)
    assert refused[0]["action"] == "emergency_sos"


def test_an_inactive_capability_leaves_its_permission_ungranted(tmp_path):
    """No SOS wired up means "emergency" is never granted to anything."""
    caps = build_capabilities(config(tmp_path))
    assert "emergency" not in caps.permissions()
    assert "home_control" not in caps.permissions()
