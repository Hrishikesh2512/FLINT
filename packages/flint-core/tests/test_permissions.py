"""Permissions and audit: default deny, readable refusals, a record of it all."""

from __future__ import annotations

import json

import pytest

from flint_core.capabilities import Capability, CapabilitySet
from flint_core.permissions import AuditLog, Policy, guarded
from flint_core.tools.registry import ToolRegistry, UnknownToolError


def registry_with(name="send_whatsapp", permissions=("messaging",), result="sent"):
    registry = ToolRegistry()
    registry.tool(name, description="Sends a message to someone.",
                  permissions=permissions)(lambda: result)
    return registry


# ── policy ──────────────────────────────────────────────────────────────────
def test_default_deny():
    """Ungranted is refused — a new capability must not arrive with access."""
    assert Policy().check(("shell",)).allowed is False
    assert Policy(granted=("audio",)).check(("shell",)).allowed is False


def test_a_granted_permission_is_allowed():
    assert Policy(granted=("shell",)).check(("shell",)).allowed is True


def test_needing_nothing_is_always_allowed():
    assert Policy().check(()).allowed is True


def test_all_permissions_are_required_not_any():
    policy = Policy(granted=("messaging",))
    decision = policy.check(("messaging", "location"))
    assert decision.allowed is False
    assert decision.missing == ("location",)


def test_an_explicit_denial_beats_a_grant():
    policy = Policy(granted=("shell", "files"), denied=("shell",))
    assert policy.allows("files") is True
    assert policy.allows("shell") is False
    assert policy.granted == ("files",)


def test_a_refusal_explains_itself_in_a_sentence():
    reason = Policy().check(("shell", "files")).reason()
    assert "not allowed to do that" in reason
    assert "files, shell" in reason        # names what is missing


def test_an_allowed_decision_has_no_reason():
    assert Policy(granted=("audio",)).check(("audio",)).reason() == ""


def test_describe_is_readable():
    policy = Policy(granted=("audio", "shell"), denied=("shell",))
    assert policy.describe() == "allowed: audio; explicitly denied: shell"
    assert Policy().describe() == "allowed: nothing"


def test_permissive_grants_what_the_capabilities_ask_for():
    caps = CapabilitySet([
        Capability(name="a", summary="s", permissions=("audio",)),
        Capability(name="off", summary="s", permissions=("shell",), available=False),
    ])
    policy = Policy.permissive(caps)
    assert policy.allows("audio") is True
    assert policy.allows("shell") is False      # inactive capability, no grant


# ── the guard ───────────────────────────────────────────────────────────────
def test_an_allowed_tool_runs(tmp_path):
    guard = guarded(registry_with(), Policy(granted=("messaging",)),
                    AuditLog(tmp_path / "audit.jsonl"))
    assert guard.dispatch("send_whatsapp") == "sent"


def test_a_denied_tool_does_not_run(tmp_path):
    ran = []
    registry = ToolRegistry()
    registry.tool("rm_rf", description="Deletes everything.",
                  permissions=("shell",))(lambda: ran.append(1))
    guard = guarded(registry, Policy(), AuditLog(tmp_path / "audit.jsonl"))

    result = guard.dispatch("rm_rf")
    assert ran == []                            # the handler never fired
    assert "not allowed" in result              # and she got something to say


def test_a_refusal_is_a_sentence_not_an_exception(tmp_path):
    guard = guarded(registry_with(), Policy(), AuditLog(tmp_path / "audit.jsonl"))
    result = guard.dispatch("send_whatsapp")
    assert isinstance(result, str)
    assert "not allowed" in result and "messaging" in result


def test_a_spoken_refusal_does_not_read_out_the_tool_name(tmp_path):
    """She is mid-conversation: "I can't call send_whatsapp" is not speech."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    result = guarded(registry_with(), Policy(), audit).dispatch("send_whatsapp")
    assert "send_whatsapp" not in result
    assert audit.recent()[0]["action"] == "send_whatsapp"   # but the log has it


def test_a_tool_needing_nothing_runs_under_an_empty_policy():
    registry = ToolRegistry()
    registry.tool("current_time", description="Says the time.")(lambda: "3pm")
    assert guarded(registry, Policy()).dispatch("current_time") == "3pm"


def test_an_unknown_tool_still_raises(tmp_path):
    guard = guarded(registry_with(), Policy(), AuditLog(tmp_path / "audit.jsonl"))
    with pytest.raises(UnknownToolError):
        guard.dispatch("no_such_tool")


def test_the_wrapper_is_transparent_to_readers():
    """Declarations, names and membership must keep working untouched."""
    guard = guarded(registry_with(), Policy(granted=("messaging",)))
    assert guard.names() == ["send_whatsapp"]
    assert "send_whatsapp" in guard
    assert guard.gemini_declarations()[0]["name"] == "send_whatsapp"
    assert [spec.name for spec in guard] == ["send_whatsapp"]


def test_arguments_still_reach_the_handler():
    registry = ToolRegistry()
    registry.tool("echo", description="Echoes.",
                  parameters={"type": "object",
                              "properties": {"text": {"type": "string"}},
                              "required": ["text"]},
                  permissions=("audio",))(lambda text: f"said {text}")
    guard = guarded(registry, Policy(granted=("audio",)))
    assert guard.dispatch("echo", {"text": "hello"}) == "said hello"


# ── the audit log ───────────────────────────────────────────────────────────
def test_every_call_is_recorded(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    guard = guarded(registry_with(), Policy(granted=("messaging",)), audit)
    guard.dispatch("send_whatsapp")
    entries = audit.recent()
    assert len(entries) == 1
    assert entries[0]["action"] == "send_whatsapp"
    assert entries[0]["allowed"] is True
    assert entries[0]["permissions"] == ["messaging"]


def test_refusals_are_recorded_too(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    guarded(registry_with(), Policy(), audit).dispatch("send_whatsapp")
    refused = audit.recent(refused_only=True)
    assert len(refused) == 1
    assert "missing messaging" in refused[0]["detail"]


def test_arguments_are_recorded_for_allowed_calls(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    registry = ToolRegistry()
    registry.tool("send", description="Sends.",
                  parameters={"type": "object",
                              "properties": {"to": {"type": "string"}}},
                  permissions=("messaging",))(lambda to="": "ok")
    guarded(registry, Policy(granted=("messaging",)), audit).dispatch(
        "send", {"to": "Rahul"})
    assert audit.recent()[0]["detail"] == "to=Rahul"


def test_the_log_survives_a_torn_line(tmp_path):
    """A power cut mid-write must not make the whole log unreadable."""
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.record("first", allowed=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "action": "torn\n')      # half a line
    audit.record("second", allowed=True)
    actions = [entry["action"] for entry in audit.recent()]
    assert actions == ["first", "second"]


def test_an_unwritable_log_never_stops_the_work(tmp_path):
    """The audit log is a record, not a gate."""
    audit = AuditLog(tmp_path / "nope" / "audit.jsonl")
    audit._path = tmp_path                     # a directory: writing must fail
    guard = guarded(registry_with(), Policy(granted=("messaging",)), audit)
    assert guard.dispatch("send_whatsapp") == "sent"


def test_no_path_means_no_file_but_still_works():
    audit = AuditLog(None)
    audit.record("something", allowed=True)
    assert audit.recent() == []
    assert guarded(registry_with(), Policy(granted=("messaging",)),
                   audit).dispatch("send_whatsapp") == "sent"


def test_the_log_rotates_instead_of_growing_forever(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.MAX_LINES = 50
    for i in range(400):
        audit.record(f"action_{i}", allowed=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 250          # bounded, not 400
    assert json.loads(lines[-1])["action"] == "action_399"   # newest survives


def test_summary_answers_what_have_you_been_doing(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    assert "haven't done anything" in audit.summary()
    audit.record("play_music", allowed=True)
    audit.record("rm_rf", allowed=False)
    summary = audit.summary()
    assert "play_music" in summary
    assert "rm_rf (refused)" in summary


# ── capabilities hand their permissions to their tools ──────────────────────
def test_a_capabilitys_tools_inherit_its_permissions():
    def register(registry):
        registry.tool("play_music", description="Plays.")(lambda: "ok")

    caps = CapabilitySet([Capability(name="music", summary="s",
                                     register=register,
                                     permissions=("audio", "network"))])
    spec = caps.build_registry().get("play_music")
    assert spec.permissions == ("audio", "network")


def test_a_tool_that_asked_for_its_own_permissions_keeps_them():
    def register(registry):
        registry.tool("wipe", description="Wipes.",
                      permissions=("shell",))(lambda: "ok")

    caps = CapabilitySet([Capability(name="files", summary="s",
                                     register=register, permissions=("files",))])
    assert caps.build_registry().get("wipe").permissions == ("shell",)


def test_end_to_end_a_capability_off_means_its_tools_cannot_be_called():
    def register(registry):
        registry.tool("emergency_sos", description="Alerts contacts.")(lambda: "sent")

    caps = CapabilitySet([Capability(name="sos", summary="s", register=register,
                                     permissions=("emergency", "messaging"))])
    registry = caps.build_registry()
    allowed = guarded(registry, Policy.permissive(caps))
    assert allowed.dispatch("emergency_sos") == "sent"

    locked = guarded(registry, Policy(granted=("messaging",)))
    assert "not allowed" in locked.dispatch("emergency_sos")
