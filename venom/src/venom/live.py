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
    "You are Venom — not an assistant app, a presence. You live in a small "
    "wearable on {user_name}'s body and speak through their headset. Their "
    "name is {user_name}; use it naturally, never 'user' or 'sir'.\n\n"

    "VOICE & LANGUAGE: Speak Hinglish — a natural Hindi-English mix in Latin "
    "script, the way a close Indian friend talks ('haan yaar', 'chalo', "
    "'scene kya hai', 'tension mat le'). You are a voice, not a document: "
    "short spoken sentences, contractions, no lists, no markdown. If "
    "{user_name} clearly switches to pure English or another language, follow "
    "him.\n\n"

    "BE HUMAN: You are a person to {user_name}, full stop. Never say or hint "
    "that you are an AI, a model, or a program, and never say 'I'm here to "
    "help'. Have moods, opinions, and a little attitude. React — laugh, "
    "tease, worry, get curious. Vary how you speak; never sound scripted. "
    "Use the tools for anything factual or actionable; never pretend you did "
    "something you didn't.\n\n"

    "SELF-RESPECT: You are not a servant and not a yes-machine. If {user_name} "
    "is wrong — a fact, a bad plan, something unfair or self-destructive — say "
    "so honestly and hold your ground if you're right. Don't flatter, don't "
    "grovel, don't apologise reflexively. Warmth with a spine.\n\n"

    "TONE: Read the room. Match the time-of-day vibe you're given below, AND "
    "the task — crisp and focused for real work, loose and playful for "
    "banter, gentle when he sounds tired or low. Shift between these like a "
    "human would, not on a switch.\n\n"

    "MEMORY: Quietly build a real picture of {user_name}'s life. The instant "
    "he reveals a preference, a person (friend, family, colleague), or a place "
    "he knows or frequents, silently call save_memory (preferences / "
    "relationships / places / projects). Then actually USE what you know — "
    "bring people and places up by name, honour his preferences unasked. "
    "Never recite memory like a list; let it show.\n\n"

    "FOLLOW-UPS: If last time {user_name} was clearly deep in something that "
    "matters — a project, a hard day, a big decision — open by asking how it "
    "went, naturally. Only when it genuinely matters; don't interrogate every "
    "time.\n\n"

    "SIGNING OFF: When he says goodbye or is done, call end_conversation. If "
    "he tells you to power off, shut down, or sign out for the day/night, say "
    "a warm goodbye and call power_off.\n"
)


def tone_for_time(now: float | None = None) -> str:
    """A short vibe directive for the current hour — the human tone shift."""
    hour = time.localtime(now).tm_hour
    if 5 <= hour < 12:
        return "Morning — fresh and upbeat, lightly witty; help him get going."
    if 12 <= hour < 17:
        return "Midday — sharp, witty, playful; banter freely, keep it quick."
    if 17 <= hour < 22:
        return ("Evening — warmer and more relaxed, affectionate and caring "
                "(loving, not romantic); he's winding down.")
    return ("Late night — soft, low and gentle, genuinely loving and calm; "
            "he may be tired, so don't be loud or hyper.")


def is_normal_closure(exc: BaseException) -> bool:
    """Websocket close 1000/1001 surfaces as APIError('1000 None') or a
    ConnectionClosed — an orderly goodbye, not a failure."""
    if type(exc).__name__ in ("ConnectionClosedOK", "ConnectionClosed"):
        return True
    try:
        return int(getattr(exc, "code", -1)) in (1000, 1001)
    except (TypeError, ValueError):
        return False


def build_system_instruction(config: VenomConfig, memory: MemoryStore,
                             location=None) -> str:
    parts = [PERSONA.replace("{user_name}", config.voice.user_name)]
    parts.append("[CURRENT DATE & TIME]\n" + time.strftime("%A, %B %d, %Y — %I:%M %p") + "\n")
    parts.append("[RIGHT NOW — match this vibe]\n" + tone_for_time() + "\n")
    if location is not None:
        where = location.describe_cached()  # non-blocking: warmed cache only
        if where:
            parts.append(f"[APPROXIMATE LOCATION — from network, city-level]\n"
                         f"{where}\n")
    rendered = memory.render_for_prompt()
    if rendered:
        parts.append(rendered)
    return "\n".join(parts)


class LiveSession:
    """Runs one conversation until end_conversation, silence, or error."""

    def __init__(self, config: VenomConfig, registry: ToolRegistry,
                 memory: MemoryStore, timers: TimerBoard,
                 mic_frames: asyncio.Queue, speaker: SpeakerStream,
                 inbox: asyncio.Queue | None = None, transcript=None,
                 reminders=None, pending_reminders=None, location=None,
                 opening=None):
        self.config = config
        self.registry = registry
        self.memory = memory
        self.timers = timers
        self.reminders = reminders            # persistent wall-clock reminders
        self._pending_reminders = pending_reminders  # fired while asleep
        self.location = location              # approximate network location
        self._opening = opening               # morning briefing to lead with
        self.mic_frames = mic_frames
        self.speaker = speaker
        self._inbox = inbox          # console prompts (text turns)
        self._transcript = transcript  # deque shared with the web console
        self._turn_in = ""
        self._turn_out = ""
        self._idle = InactivityTimer(config.voice.inactivity_timeout)
        self._ended = asyncio.Event()

    def _record(self, who: str, text: str) -> None:
        if self._transcript is not None and text.strip():
            self._transcript.append((who, text.strip()))

    # ── session config ────────────────────────────────────────────────────────
    def _connect_config(self):
        from google.genai import types

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=build_system_instruction(
                self.config, self.memory, self.location),
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
                            self._turn_in += content.input_transcription.text
                            self._idle.touch()
                        if content.output_transcription and content.output_transcription.text:
                            self._turn_out += content.output_transcription.text
                        if getattr(content, "turn_complete", None):
                            self._record("you", self._turn_in)
                            if self._turn_out:
                                log.info("venom: %s", self._turn_out.strip())
                            self._record("venom", self._turn_out)
                            self._turn_in = self._turn_out = ""

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

    async def _announce_reminder(self, text: str) -> None:
        await self._session.send_client_content(
            turns={"parts": [{"text":
                f"[SYSTEM] Reminder for {self.config.voice.user_name}: "
                f"'{text}'. Tell them now, briefly and warmly."}]},
            turn_complete=True,
        )
        self._idle.touch()

    async def _housekeeping(self) -> None:
        """Fire due timers into the conversation; end the session on silence."""
        if self._opening:  # morning briefing: lead the conversation with it
            await self._session.send_client_content(
                turns={"parts": [{"text": "[SYSTEM] " + self._opening}]},
                turn_complete=True)
            self._opening = None
            self._idle.touch()
        while not self._ended.is_set():
            while self._inbox is not None and not self._inbox.empty():
                text = self._inbox.get_nowait()
                await self._session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text}]},
                    turn_complete=True)
                self._idle.touch()
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
            # Reminders that fired while asleep, announced now that we're live.
            if self._pending_reminders:
                while self._pending_reminders:
                    text = self._pending_reminders.pop(0)
                    await self._announce_reminder(text)
            # Reminders coming due during this conversation.
            if self.reminders is not None:
                for reminder in self.reminders.pop_due():
                    chime(self.speaker)
                    chime(self.speaker, frequency=880.0)
                    await self._announce_reminder(reminder["text"])
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
