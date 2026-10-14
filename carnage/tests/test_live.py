"""The brain side of a live voice call: what Kotlin sends, and what it gets back."""

from __future__ import annotations

import json

import pytest

from carnage.config import CarnageConfig, HubConfig, load_config
from carnage.live import LiveBrain, handle_json
from carnage.platform import AbsentPhone
from carnage.runtime import Carnage


@pytest.fixture()
def brain(tmp_path):
    carnage = Carnage(CarnageConfig(device="carnage", user_name="Hrishikesh",
                                    state_dir=tmp_path / "state",
                                    hub=HubConfig(enabled=False)),
                      phone=AbsentPhone())
    return LiveBrain(carnage)


def _tool_call(name, args=None, call_id="c1"):
    return {"toolCall": {"functionCalls": [
        {"id": call_id, "name": name, "args": args or {}}]}}


def test_setup_is_the_live_wire_format_with_her_voice_and_prompt(brain):
    setup = brain.setup_message()["setup"]
    assert setup["model"] == "models/gemini-2.5-flash-native-audio-preview-12-2025"
    voice = setup["generationConfig"]["speechConfig"]["voiceConfig"]
    assert voice["prebuiltVoiceConfig"]["voiceName"] == "Leda"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    prompt = setup["systemInstruction"]["parts"][0]["text"]
    assert prompt == brain.carnage.system_instruction()
    assert "inputAudioTranscription" in setup and "outputAudioTranscription" in setup
    json.dumps(setup)                       # must survive the trip to Kotlin


def test_setup_offers_every_registry_tool_and_a_way_to_hang_up(brain):
    names = {d["name"] for d in
             brain.setup_message()["setup"]["tools"][0]["functionDeclarations"]}
    assert {spec.name for spec in brain.carnage.registry} <= names
    assert "end_conversation" in names


def test_a_tool_call_is_dispatched_through_the_shared_registry(brain):
    name = next(iter(brain.carnage.registry)).name
    out = brain.handle(_tool_call(name, call_id="abc"))
    [response] = out["response"]["toolResponse"]["functionResponses"]
    assert response["id"] == "abc" and response["name"] == name
    assert isinstance(response["response"]["result"], str)
    assert out["end"] is False


def test_a_failing_tool_is_reported_not_raised(brain):
    out = brain.handle(_tool_call("no_such_tool"))
    [response] = out["response"]["toolResponse"]["functionResponses"]
    assert response["response"]["result"].startswith("Tool failed")


def test_end_conversation_ends_the_call(brain):
    out = brain.handle(_tool_call("end_conversation"))
    assert out["end"] is True


def test_json_in_json_out_and_garbage_is_harmless(brain):
    out = json.loads(handle_json(brain, json.dumps(_tool_call("end_conversation"))))
    assert out["end"] is True
    empty = json.loads(handle_json(brain, "not json"))
    assert empty["response"]["toolResponse"]["functionResponses"] == []


def test_a_finished_call_lands_in_the_shared_archive(brain):
    brain.record("you", "remind me to call Ma")
    brain.record("jarvis", "Done, I'll remind you at six.")
    brain.record("you", "   ")
    assert brain.finish() is not None
    assert brain.turns == []
    assert brain.carnage.archive.search("Ma")
    assert brain.finish() is None           # nothing said, nothing filed


def test_voice_settings_are_read_from_the_config_file(tmp_path):
    path = tmp_path / "carnage.json"
    path.write_text(json.dumps({"live_model": "models/x", "voice_name": "Kore"}))
    config = load_config(path)
    assert (config.live_model, config.voice_name) == ("models/x", "Kore")
