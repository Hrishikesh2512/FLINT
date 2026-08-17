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
from flint_core.persona import render_persona
from flint_core.tools import ToolRegistry
from venom.audio.streams import SpeakerStream, chime
from venom.config import VenomConfig
from venom.tools_pi import TimerBoard
from venom.wake import InactivityTimer

log = logging.getLogger("venom.live")

# Who she is lives in flint_core.persona — the same text on every body, so the
# Pi and the phone are one person rather than two with a shared filing cabinet.
# Only the one-sentence body description differs, and this is Venom's.
#
# Per-skill instructions ship with the skills, in venom/capabilities.py, so a
# device that cannot do a thing is never told how to do it.
BODY = "wearable"

#: The persona with the name left unresolved. Kept because tests and the
#: console read it, and because rendering it per session would re-do the same
#: substitution on every reconnect for a string that never changes.
PERSONA = render_persona("{user_name}", BODY)


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


def collapse_doubled(text: str) -> str:
    """'Skip kar diya.Skip kar diya.' -> 'Skip kar diya.'

    When the model speaks a confirmation, receives the tool result, and then
    repeats itself, the turn's transcript arrives as the same sentence twice
    back-to-back. Collapse an exact doubling so the conversation journal (and
    the prompts it feeds) stay clean; anything else passes through untouched."""
    t = text.strip()
    half, rem = divmod(len(t), 2)
    if rem == 0 and half and t[:half].strip() == t[half:].strip():
        return t[:half].strip()
    return t


# Tools whose entire point is that something CHANGED in the world. Every call
# to one is journaled into the conversation log, so the history she reads back
# carries proof the action happened instead of only her claim about it.
#
# Why this exists: the journal used to store speech alone. She would then read
# back turn after turn of "play X" -> "haan yaar, laga diya maine" with no tool
# anywhere in sight, and learn from her own transcript that saying it IS doing
# it. Observed live — two byte-identical "laga diya maine" replies seven
# minutes apart, no play_music call either time, song never changed, and the
# user re-asking for the same song because nothing happened. The prompt already
# forbade it; forty turns of contrary example beat one line of instruction.
#
# Read-only tools stay out on purpose: they would crowd real conversation out
# of the rendered window. Add new state-changing tools here.
ACTION_TOOLS = frozenset({
    "play_music", "stop_music", "pause_music", "resume_music", "next_song",
    "restart_song", "previous_song", "add_favourite", "remove_favourite",
    "play_favourites", "play_favourite", "download_favourites",
    "set_volume", "change_volume", "send_whatsapp", "auto_reply_mode",
    "laptop_task", "set_timer", "set_reminder", "cancel_reminder",
    "add_note", "clear_notes", "add_to_list", "remove_from_list", "clear_list",
    "save_memory", "save_connection", "forget_connection",
    "emergency_sos", "end_emergency", "emergency_contacts",
    "start_chess_game", "play_chess_move", "resign_chess",
    "pair_bluetooth_device", "disconnect_bluetooth_audio",
    "translation_mode", "find_my_phone", "power_off",
    "research_in_background", "cancel_background_job",
    "watch_for", "stop_watching",
})

# A journal line is context, not prose — keep it to a glance.
ACTION_NOTE_CHARS = 160


def action_note(name: str, args, result: str) -> str:
    """One compact journal line recording that a tool really ran.

    Includes the outcome, failures included: a tool that errored must show up
    as an error, or she will read the bare call as a success and confirm
    something that never happened.
    """
    shown = ", ".join(f"{k}={v}" for k, v in dict(args or {}).items())
    outcome = " ".join(str(result).split())
    return f"{name}({shown}) -> {outcome}"[:ACTION_NOTE_CHARS]


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
                             location=None, convlog=None,
                             capabilities=None, context=None,
                             learned=None, roster=None) -> str:
    """Who she is, then what this particular device can do, then right now.

    `capabilities` is a CapabilitySet (venom/capabilities.py); its active
    members contribute the per-skill instructions that used to be hard-coded
    into PERSONA. Passing None yields the persona alone — correct for a device
    with no skills wired up, and what the older tests expect.
    """
    parts = [render_persona(config.voice.user_name, BODY)]
    if capabilities is not None:
        skills = capabilities.render_prompt(user_name=config.voice.user_name)
        if skills:
            parts.append(skills + "\n")
    parts.append("[CURRENT DATE & TIME]\n" + time.strftime("%A, %B %d, %Y — %I:%M %p") + "\n")
    parts.append("[RIGHT NOW — match this vibe]\n" + tone_for_time() + "\n")
    if location is not None:
        where = location.describe_cached()  # non-blocking: warmed cache only
        if where:
            parts.append(f"[APPROXIMATE LOCATION — from network, city-level]\n"
                         f"{where}\n")
    if context is not None:
        # What he's actually doing — the repo, the files he just touched.
        # Empty string when nothing is known, which is most of the time on a
        # Pi with no project configured.
        now_doing = context.render_for_prompt()
        if now_doing:
            parts.append(now_doing)
    if roster is not None:
        # Her other bodies. Goes in before the memory block because it is
        # about who she is rather than what she knows, and because the
        # instruction it carries — never refuse what another body can do —
        # has to be read as part of her character, not as trivia.
        others = roster.render_for_prompt()
        if others:
            parts.append(others)
    if learned is not None:
        # Only ever non-empty once there is real evidence behind it.
        lessons = learned.render_for_prompt()
        if lessons:
            parts.append(lessons)
    rendered = memory.render_for_prompt()
    if rendered:
        parts.append(rendered)
    if convlog is not None:
        recent = convlog.render_for_prompt()
        if recent:
            parts.append(recent)
    return "\n".join(parts)


class LiveSession:
    """Runs one conversation until end_conversation, silence, or error."""

    def __init__(self, config: VenomConfig, registry: ToolRegistry,
                 memory: MemoryStore, timers: TimerBoard,
                 mic_frames: asyncio.Queue, speaker: SpeakerStream,
                 inbox: asyncio.Queue | None = None, transcript=None,
                 reminders=None, pending_reminders=None, location=None,
                 opening=None, convlog=None, capabilities=None,
                 context=None, learned=None, roster=None):
        self.config = config
        self.capabilities = capabilities      # what this device can actually do
        self.context = context                # what he's doing right now
        self.learned = learned                # what past outcomes taught her
        self.roster = roster                  # her other bodies
        self.registry = registry
        self.memory = memory
        self.timers = timers
        self.reminders = reminders            # persistent wall-clock reminders
        self._pending_reminders = pending_reminders  # fired while asleep
        self.location = location              # approximate network location
        self._opening = opening               # morning briefing to lead with
        self._convlog = convlog               # persistent conversation journal
        self.mic_frames = mic_frames
        self.speaker = speaker
        self._inbox = inbox          # console prompts (text turns)
        self._transcript = transcript  # deque shared with the web console
        self._turn_in = ""
        self._turn_out = ""
        self._idle = InactivityTimer(config.voice.inactivity_timeout)
        self._ended = asyncio.Event()
        self._reply_clock: float | None = None  # set when a turn is committed
        # Button barge-in: when the user cuts in with a button, we flush the
        # queued reply AND drop the rest of this turn's audio as it keeps
        # arriving (the server doesn't know about the press). Cleared at the
        # next turn boundary (interruption / new user speech / turn_complete).
        self._suppress_output = False
        # Live interpreter mode (Hindi <-> Kannada/Telugu). Toggled by the
        # translation_mode tool; a distinct chime marks each switch.
        self._translation_mode = False
        # Pre-warming: the socket + big-prompt prefill are the real cold-start
        # cost (~4-5s). We connect ahead of time and sit idle — not touching the
        # mic, not counting idle-timeout — until the wake word activates us, so
        # the first reply after "Hey Jarvis" is the warm ~1s path, every time.
        self._connected = asyncio.Event()
        self._active = asyncio.Event()
        # FIXED (Fix 6): cache the rendered system instruction so a reconnect
        # doesn't re-render memory + rebuild the whole prompt every time.
        # Invalidated only when save_memory succeeds (see _handle_tools).
        self._cached_instruction: str | None = None
        # FIXED (Fix 4): serialise tool responses (tools now dispatch
        # concurrently off the receive loop) and keep strong refs to the
        # in-flight tool tasks so they aren't garbage-collected mid-run.
        self._tool_lock = asyncio.Lock()
        self._tool_tasks: set[asyncio.Task] = set()

    def activate(self) -> None:
        """Wake word fired: start streaming mic audio and counting silence."""
        # FIXED (Fix 5): drop any mic frames captured during the pre-warm wait
        # so stale pre-wake audio isn't streamed into the fresh conversation.
        while True:
            try:
                self.mic_frames.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._idle.touch()
        self._active.set()

    async def wait_connected(self) -> None:
        await self._connected.wait()

    def interrupt(self) -> None:
        """Button barge-in: silence the queued reply now and drop the rest of
        this turn as it streams in. The continuous mic uplink carries whatever
        the user says next, which the model picks up as a fresh turn."""
        self.speaker.flush()
        self._suppress_output = True

    def request_stop(self) -> None:
        """External stop (wake button pressed while a conversation is live):
        go silent now and end the session, back to wake-word listening."""
        self.speaker.flush()
        self._ended.set()

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    def _record(self, who: str, text: str) -> None:
        if not text.strip():
            return
        if self._transcript is not None:
            self._transcript.append((who, text.strip()))
        if self._convlog is not None:
            try:  # a disk hiccup must never break the conversation
                self._convlog.add(who, text)
            except OSError:
                log.warning("could not persist conversation turn")

    def _system_instruction(self) -> str:
        # FIXED (Fix 6): render once and reuse. Rebuilding the full persona +
        # memory prompt on every session/reconnect was pure waste; save_memory
        # clears this cache (in _handle_tools) so a new memory still lands.
        if self._cached_instruction is None:
            self._cached_instruction = build_system_instruction(
                self.config, self.memory, self.location, self._convlog,
                self.capabilities, self.context, self.learned,
                self.roster)
        return self._cached_instruction

    # ── session config ────────────────────────────────────────────────────────
    def _connect_config(self):
        from google.genai import types

        # thinking_budget < 0 means "leave the model's own default alone" —
        # forcing it off actually made the native-audio model reply *worse*.
        # Only pass a ThinkingConfig when a budget is explicitly requested.
        thinking_config = None
        if self.config.voice.thinking_budget >= 0:
            thinking_config = types.ThinkingConfig(
                thinking_budget=self.config.voice.thinking_budget)

        voice = self.config.voice
        # native-audio realism: react to the emotion in his voice, and (opt-in)
        # decide when not to reply instead of dutifully answering everything.
        proactivity = (types.ProactivityConfig(proactive_audio=True)
                       if voice.proactive_audio else None)

        # FIXED (Fix 1): only *include* enable_affective_dialog when it's on.
        # Passing it as False still serialises "enableAffectiveDialog": false
        # into the setup, and the fast v1beta endpoint rejects the field's mere
        # presence ("Unknown name enableAffectiveDialog"). Omitting it keeps the
        # default-off session on the clean, low-latency v1beta path.
        extra: dict = {}
        if voice.affective_dialog:
            extra["enable_affective_dialog"] = True

        # End-of-speech tuning: the spoken turn-around is dominated by how long
        # the server VAD waits before deciding the user is done. HIGH
        # sensitivity + a short silence window starts the reply the moment he
        # stops, which is what makes voice feel as fast as the console (text
        # turns skip VAD entirely — that's why typing always felt instant).
        realtime_config = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=voice.endpoint_silence_ms,
            ))

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            realtime_input_config=realtime_config,
            thinking_config=thinking_config,
            proactivity=proactivity,
            temperature=voice.temperature,
            system_instruction=self._system_instruction(),
            tools=[{"function_declarations":
                    self.registry.gemini_declarations(uppercase_types=True)}],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.config.voice.voice_name))),
            **extra,
        )

    # ── run ───────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        from google import genai

        # Affective dialog and proactive audio are v1alpha-only — v1beta rejects
        # the setup ("Unknown name enableAffectiveDialog"). Use v1alpha only when
        # one is explicitly opted in; otherwise stay on the faster v1beta path.
        # FIXED (Fix 1): with affective_dialog now defaulting False, the common
        # case is v1beta — the low-latency endpoint — instead of always v1alpha.
        voice = self.config.voice
        api_version = ("v1alpha" if (voice.affective_dialog or voice.proactive_audio)
                       else "v1beta")
        client = genai.Client(api_key=self.config.gemini_api_key,
                              http_options={"api_version": api_version})
        log.info("live session connecting (%s)", self.config.voice.live_model)
        async with (
            client.aio.live.connect(model=self.config.voice.live_model,
                                    config=self._connect_config()) as session,
            asyncio.TaskGroup() as group,
        ):
            self._session = session
            log.info("live session open")
            self._connected.set()   # warm and ready; waiting for activation
            group.create_task(self._uplink())
            group.create_task(self._downlink())
            group.create_task(self._housekeeping())

    async def _uplink(self) -> None:
        from google.genai import types

        # Stay off the mic while pre-warmed: the wake detector owns the mic
        # queue during standby, so we mustn't steal its frames until activated.
        await self._active.wait()

        # Newer Live models reject the legacy media_chunks path ("realtime_input.
        # media_chunks is deprecated"); the current audio=Blob form works across
        # native-audio and flash-live models alike.
        while not self._ended.is_set():
            try:
                frame = await asyncio.wait_for(self.mic_frames.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._session.send_realtime_input(
                    audio=types.Blob(data=frame,
                                     mime_type="audio/pcm;rate=16000"))
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
                        # After a button barge-in, drop the interrupted turn's
                        # audio as it keeps arriving so she stays silent.
                        if not self._suppress_output:
                            if self._reply_clock is not None:
                                log.info("first-audio %.2fs",
                                         time.monotonic() - self._reply_clock)
                                self._reply_clock = None
                            self.speaker.play(response.data)
                            self._idle.touch()

                    content = response.server_content
                    if content:
                        if getattr(content, "interrupted", None):
                            # Server-side (voice) interruption: user took the
                            # floor — flush and resume normal playback for the
                            # new turn.
                            self.speaker.flush()
                            self._suppress_output = False
                        if content.input_transcription and content.input_transcription.text:
                            self._turn_in += content.input_transcription.text
                            self._suppress_output = False  # user speaking → new turn
                            # Start the reply clock from the last speech we heard,
                            # so first-audio measures the real spoken turn-around
                            # (end-of-speech detection + model), not just text.
                            self._reply_clock = time.monotonic()
                            self._idle.touch()
                        if content.output_transcription and content.output_transcription.text:
                            self._turn_out += content.output_transcription.text
                        if getattr(content, "turn_complete", None):
                            self._suppress_output = False  # turn done → resume
                            self._record("you", self._turn_in)
                            said = collapse_doubled(self._turn_out)
                            if said:
                                log.info("jarvis: %s", said)
                            self._record("jarvis", said)
                            self._turn_in = self._turn_out = ""

                    if response.tool_call:
                        # FIXED (Fix 4): dispatch tools on their own task instead
                        # of awaiting here — a slow tool used to freeze the whole
                        # receive loop, so incoming audio stalled until it
                        # finished. The downlink now keeps flowing while tools run.
                        task = asyncio.ensure_future(
                            self._handle_tools(response.tool_call))
                        self._tool_tasks.add(task)
                        task.add_done_callback(self._on_tool_done)
        except Exception as exc:
            if not (self._ended.is_set() or is_normal_closure(exc)):
                raise
            log.info("live session closed (%s)", type(exc).__name__)
        finally:
            self._ended.set()

    def _on_tool_done(self, task: asyncio.Task) -> None:
        # FIXED (Fix 4): drop the strong ref and surface any real failure — a
        # backgrounded tool task must not swallow errors silently.
        self._tool_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not is_normal_closure(exc):
            log.warning("tool task failed: %s", exc)

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
            if call.name == "translation_mode":
                self._translation_mode = bool((call.args or {}).get("enable", True))
                if self._translation_mode:      # entering: rising two-tone
                    chime(self.speaker, frequency=784.0)
                    chime(self.speaker, frequency=988.0)
                else:                           # leaving: falling two-tone
                    chime(self.speaker, frequency=988.0)
                    chime(self.speaker, frequency=784.0)
            try:
                result = await asyncio.to_thread(
                    self.registry.dispatch, call.name, dict(call.args or {}))
                result = result or "Done."
                # FIXED (Fix 6): a successful save_memory changed the memory the
                # system prompt renders — drop the cache so the next build picks
                # it up. Only on success (an exception skips this).
                if call.name == "save_memory":
                    self._cached_instruction = None
            except Exception as exc:
                log.warning("tool %s failed: %s", call.name, exc)
                result = f"Tool failed: {exc}"
            if call.name in ACTION_TOOLS:
                self._record("action", action_note(call.name, call.args, result))
            responses.append(types.FunctionResponse(
                id=call.id, name=call.name, response={"result": str(result)}))
        # FIXED (Fix 4): serialise the reply — tools now run concurrently, so two
        # tool batches finishing together must not interleave on the socket.
        async with self._tool_lock:
            try:
                await self._session.send_tool_response(function_responses=responses)
            except Exception as exc:
                # The socket closing under an in-flight tool reply is part of a
                # normal session end, not an error.
                if not (self._ended.is_set() or is_normal_closure(exc)):
                    raise

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
        # Don't drive the conversation (briefing, inbox, idle-timeout) while the
        # session is only pre-warmed — wait until the wake word activates us.
        await self._active.wait()
        if self._opening:  # morning briefing: lead the conversation with it
            await self._session.send_client_content(
                turns={"parts": [{"text": "[SYSTEM] " + self._opening}]},
                turn_complete=True)
            self._opening = None
            self._idle.touch()
        while not self._ended.is_set():
            while self._inbox is not None and not self._inbox.empty():
                text = self._inbox.get_nowait()
                self._reply_clock = time.monotonic()
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
            # FIXED (Fix 7): 0.5s -> 0.1s. This poll gates timer firing, reminder
            # delivery, console prompts and the idle-timeout, so half a second
            # added up to 500ms of lag on each — 0.1s keeps them snappy.
            await asyncio.sleep(0.1)
        # unblock the TaskGroup: cancel siblings by raising in one task is
        # messy — instead close the session, which ends receive()/uplink.
        try:
            await self._session.close()
        except Exception:
            pass
