"""The brain side of a live voice conversation on the phone.

Venom talks through `google-genai`'s Live client. That library leans on
compiled extensions with no Android wheels, so on the phone the real-time half
— microphone, speaker, Bluetooth routing and the websocket itself — is native
Kotlin (`LiveVoice.kt`), where audio belongs anyway. What stays here is
everything that makes her *her*, and it is the same Python the Pi runs:

    setup_message()   who she is and every tool she has, in the Live API's own
                      wire format, so Kotlin forwards it without understanding it
    handle(message)   a `toolCall` from the server in, a `toolResponse` out —
                      dispatched through the shared registry
    record()/finish() the transcript, filed into the shared archive when the
                      conversation ends, so the Pi can recall what was said here

The split is the point: Kotlin never learns what a tool is, and adding one
stays a Python change, exactly as it is for the text conversation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from flint_core.recall import archive_conversation

log = logging.getLogger("carnage.live")

#: The Live API's bidirectional endpoint. The key rides as a query parameter;
#: Kotlin appends it so the key never has to cross into a log line from here.
LIVE_URL = ("wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService."
            "BidiGenerateContent")

#: Matches Venom's tuning: high end-of-speech sensitivity and a short silence
#: window are what make a spoken reply start the moment he stops talking.
ENDPOINT_SILENCE_MS = 500

#: Handled here rather than by the registry: ending the call is the session's
#: business, not a skill's. Venom offers the model the same escape hatch.
END_CONVERSATION = {
    "name": "end_conversation",
    "description": ("End the voice conversation when he says goodbye, says "
                    "that's all, or clearly wants you to stop listening."),
}


class LiveBrain:
    """One voice conversation's worth of brain, over a `Carnage`."""

    def __init__(self, carnage):
        self.carnage = carnage
        self.turns: list[dict] = []

    # ── the opening message ─────────────────────────────────────────────────
    def setup_message(self) -> dict[str, Any]:
        """The `setup` frame, ready to send as the first websocket message."""
        config = self.carnage.config
        declarations = self.carnage.registry.gemini_declarations(
            uppercase_types=True)
        declarations.append(dict(END_CONVERSATION))
        return {"setup": {
            "model": config.live_model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                    "voiceName": config.voice_name}}},
            },
            "systemInstruction": {"parts": [
                {"text": self.carnage.system_instruction()}]},
            "tools": [{"functionDeclarations": declarations}],
            "realtimeInputConfig": {"automaticActivityDetection": {
                "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                "silenceDurationMs": ENDPOINT_SILENCE_MS,
            }},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            # Without it Google ends a call with video in it after about two
            # minutes; a sliding window keeps the call going as long as he
            # does, forgetting only the oldest of what was seen and said.
            "contextWindowCompression": {"slidingWindow": {}},
        }}

    # ── tool calls ──────────────────────────────────────────────────────────
    def handle(self, message: dict) -> dict[str, Any]:
        """Answer a server `toolCall` frame.

        Returns {"response": <toolResponse frame>, "end": bool}. Never raises:
        a tool that fails is reported to the model as a failure, which it can
        say out loud, rather than tearing down the call mid-sentence.
        """
        calls = (message.get("toolCall") or {}).get("functionCalls") or []
        responses = []
        end = False
        for call in calls:
            name = str(call.get("name", ""))
            args = call.get("args") or {}
            if name == END_CONVERSATION["name"]:
                end = True
                result = "Goodbye."
            else:
                log.info("tool: %s %s", name, args)
                try:
                    result = self.carnage.registry.dispatch(name, dict(args))
                    result = result or "Done."
                except Exception as exc:        # noqa: BLE001
                    log.warning("tool %s failed: %s", name, exc)
                    result = f"Tool failed: {exc}"
            responses.append({"id": call.get("id"), "name": name,
                              "response": {"result": str(result)}})
        return {"response": {"toolResponse": {"functionResponses": responses}},
                "end": end}

    # ── the transcript ──────────────────────────────────────────────────────
    def record(self, who: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.turns.append({"who": who, "text": text})

    def finish(self) -> int | None:
        """File the conversation in the shared archive; start a fresh one."""
        turns, self.turns = self.turns, []
        if not turns:
            return None
        try:
            return archive_conversation(self.carnage.archive, turns,
                                        subject="voice on the phone")
        except Exception:                       # noqa: BLE001
            log.exception("could not archive the voice conversation")
            return None


def handle_json(brain: LiveBrain, message_json: str) -> str:
    """`handle`, over JSON strings — the shape that crosses into Kotlin."""
    try:
        message = json.loads(message_json)
    except ValueError:
        message = {}
    return json.dumps(brain.handle(message if isinstance(message, dict) else {}))
