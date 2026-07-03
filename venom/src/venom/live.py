"""One spoken conversation: Pi ↔ Gemini Live, tools dispatched locally.

The Pi streams raw mic PCM up and plays reply PCM down. Gemini Live owns
in-conversation VAD, interruptions, and turn-taking; Venom owns the tool
belt, memory, timers, and the decision to end the session after silence.
"""

from __future__ import annotations

import asyncio
import logging
import time

from flint_core.memory import MemoryStore
from flint_core.tools import ToolRegistry
from venom.audio.streams import SpeakerStream, chime
from venom.config import VenomConfig
from venom.tools_pi import TimerBoard
from venom.wake import InactivityTimer

log = logging.getLogger("venom.live")

PERSONA = (
    "You are Venom, a voice assistant living in a small wearable device on "
    "{user_name}'s body. You speak through their headset. Be warm, direct, "
    "and brief — you are a voice, not a document: no lists, no markdown, "
    "short sentences, natural spoken language. Use the provided tools for "
    "anything factual or actionable; never pretend to have done something. "
    "When {user_name} says 'play <something>', call play_music with it. "
    "If {user_name} speaks another language, reply in that language.\n"
)


def is_normal_closure(exc: BaseException) -> bool:
    """Websocket close 1000/1001 surfaces as APIError('1000 None') or a
    ConnectionClosed — an orderly goodbye, not a failure."""
    if type(exc).__name__ in ("ConnectionClosedOK", "ConnectionClosed"):
        return True
    try:
        return int(getattr(exc, "code", -1)) in (1000, 1001)
    except (TypeError, ValueError):
        return False


def build_system_instruction(config: VenomConfig, memory: MemoryStore) -> str:
    parts = [PERSONA.replace("{user_name}", config.voice.user_name)]
    parts.append("[CURRENT DATE & TIME]\n" + time.strftime("%A, %B %d, %Y — %I:%M %p") + "\n")
    rendered = memory.render_for_prompt()
    if rendered:
        parts.append(rendered)
    return "\n".join(parts)


class LiveSession:
    """Runs one conversation until end_conversation, silence, or error."""

    def __init__(self, config: VenomConfig, registry: ToolRegistry,
                 memory: MemoryStore, timers: TimerBoard,
                 mic_frames: asyncio.Queue, speaker: SpeakerStream):
        self.config = config
        self.registry = registry
        self.memory = memory
        self.timers = timers
        self.mic_frames = mic_frames
        self.speaker = speaker
        self._idle = InactivityTimer(config.voice.inactivity_timeout)
        self._ended = asyncio.Event()

    # ── session config ────────────────────────────────────────────────────────
    def _connect_config(self):
        from google.genai import types

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=build_system_instruction(self.config, self.memory),
            tools=[{"function_declarations":
                    self.registry.gemini_declarations(uppercase_types=True)}],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.config.voice.voice_name))),
        )

    # ── run ───────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        from google import genai

        client = genai.Client(api_key=self.config.gemini_api_key,
                              http_options={"api_version": "v1beta"})
        log.info("live session connecting (%s)", self.config.voice.live_model)
        async with (
            client.aio.live.connect(model=self.config.voice.live_model,
                                    config=self._connect_config()) as session,
            asyncio.TaskGroup() as group,
        ):
            self._session = session
            log.info("live session open")
            group.create_task(self._uplink())
            group.create_task(self._downlink())
            group.create_task(self._housekeeping())

    async def _uplink(self) -> None:
        while not self._ended.is_set():
            try:
                frame = await asyncio.wait_for(self.mic_frames.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._session.send_realtime_input(
                    media={"data": frame, "mime_type": "audio/pcm"})
            except Exception as exc:
                # The socket closing under an in-flight send is part of every
                # intentional session end — not an error, no error chime.
                if self._ended.is_set() or is_normal_closure(exc):
                    return
                raise

    async def _downlink(self) -> None:
        try:
            while not self._ended.is_set():
                async for response in self._session.receive():
                    if response.data:
                        self.speaker.play(response.data)
                        self._idle.touch()

                    content = response.server_content
                    if content:
                        if getattr(content, "interrupted", None):
                            self.speaker.flush()
                        if content.input_transcription and content.input_transcription.text:
                            self._idle.touch()
                        if content.output_transcription and content.output_transcription.text:
                            log.info("venom: %s", content.output_transcription.text.strip())

                    if response.tool_call:
                        await self._handle_tools(response.tool_call)
        except Exception as exc:
            if not (self._ended.is_set() or is_normal_closure(exc)):
                raise
            log.info("live session closed (%s)", type(exc).__name__)
        finally:
            self._ended.set()

    async def _handle_tools(self, tool_call) -> None:
        from google.genai import types

        responses = []
        for call in tool_call.function_calls:
            log.info("tool: %s %s", call.name, dict(call.args or {}))
            self._idle.touch()
            if call.name == "end_conversation":
                responses.append(types.FunctionResponse(
                    id=call.id, name=call.name, response={"result": "Goodbye."}))
                self._ended.set()
                continue
            try:
                result = await asyncio.to_thread(
                    self.registry.dispatch, call.name, dict(call.args or {}))
                result = result or "Done."
            except Exception as exc:
                log.warning("tool %s failed: %s", call.name, exc)
                result = f"Tool failed: {exc}"
            responses.append(types.FunctionResponse(
                id=call.id, name=call.name, response={"result": str(result)}))
        await self._session.send_tool_response(function_responses=responses)

    async def _housekeeping(self) -> None:
        """Fire due timers into the conversation; end the session on silence."""
        while not self._ended.is_set():
            for timer in self.timers.pop_due():
                chime(self.speaker)
                chime(self.speaker, frequency=1100.0)
                await self._session.send_client_content(
                    turns={"parts": [{"text":
                        f"[SYSTEM] The timer '{timer.label}' just finished. "
                        f"Tell {self.config.voice.user_name} now, briefly."}]},
                    turn_complete=True,
                )
                self._idle.touch()
            if self._idle.expired and not self.speaker.playing:
                log.info("session idle %.0fs — closing", self._idle.idle_for)
                self._ended.set()
                break
            await asyncio.sleep(0.5)
        # unblock the TaskGroup: cancel siblings by raising in one task is
        # messy — instead close the session, which ends receive()/uplink.
        try:
            await self._session.close()
        except Exception:
            pass
