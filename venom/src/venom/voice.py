"""The voice loop: sleep-listening for the wake word, then a live session.

    ┌────────────┐  wake word   ┌──────────────┐  silence / goodbye
    │ WAKE       │ ───────────► │ CONVERSATION │ ─────────────────┐
    │ (oww ~3%   │   (chime)    │ (Gemini Live │                  │
    │  CPU)      │ ◄─────────── │  + tools)    │ ◄────────────────┘
    └────────────┘              └──────────────┘

Reliability model (learned on real hardware): the Bluetooth headset can
drop at any moment and never stores its bond; audio streams die silently
when the device vanishes. So the orchestrator wraps one full lifecycle —
connect headset → pin mic profile → open streams → chime → listen — and
any starvation or error tears the whole thing down and starts the cycle
again. Timers keep working across all of it.
"""

from __future__ import annotations

import asyncio
import logging

from flint_core.memory import MemoryStore
from venom.audio.devices import current_devices
from venom.audio.streams import MicStream, SpeakerStream, chime
from venom.config import VenomConfig
from venom.live import LiveSession
from venom.tools_pi import TimerBoard, build_pi_registry
from venom.wake import WakeWordDetector

log = logging.getLogger("venom.voice")

# Wake-phase frame starvation: mic callbacks deliver ~15 frames/s; this many
# consecutive empty seconds means the stream is dead or the headset is gone.
STARVATION_SECONDS = 12
# Frames that keep arriving but are all exactly zero: the capture path was
# silently rerouted (observed live: headset drops mid-lifecycle and PipeWire
# falls back to the built-in jack's sink monitor — Venom sits deaf forever).
SILENCE_REBUILD_SECONDS = 20


class StreamsDied(Exception):
    """Audio stopped flowing — rebuild the whole audio lifecycle."""


class SilenceTracker:
    """Detects a dead capture path: a real microphone always carries a noise
    floor, so sustained bit-exact silence means nobody is actually listening."""

    def __init__(self, limit_seconds: float = SILENCE_REBUILD_SECONDS,
                 sample_rate: int = 16000):
        self._limit = limit_seconds
        self._rate = sample_rate
        self._silent = 0.0

    def update(self, frame: bytes) -> bool:
        """Feed one mic frame; True when the silence limit is crossed."""
        if any(frame):
            self._silent = 0.0
        else:
            self._silent += len(frame) / 2 / self._rate
        return self._silent >= self._limit


class VoiceOrchestrator:
    def __init__(self, config: VenomConfig, activity=None):
        from collections import deque

        self.config = config
        # FIXED (Fix 2): shared VoiceActivity flag (from the supervisor). While
        # a conversation is live we flip session_active True so the brain
        # switcher won't probe or flap mid-sentence. Optional so the voice loop
        # still runs standalone (tests, dev) with a private stand-in.
        if activity is None:
            from venom.supervisor import VoiceActivity

            activity = VoiceActivity()
        self.activity = activity
        # Consecutive pre-warm failures, for a gentle retry backoff (see
        # _wait_for_wake): a one-off blip recovers fast, a persistent outage
        # backs off instead of hammering the socket every 2s.
        self._prewarm_fails = 0
        self.state = "starting"
        # Web console: prompts in, transcript out (thread-safe via event loop).
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.transcript = deque(maxlen=60)
        self.memory = MemoryStore(config.memory_path)
        self.timers = TimerBoard()
        # Persistent productivity stores live beside memory in the state dir.
        from venom.stores import ListStore, NoteStore, ReminderStore

        state_dir = config.memory_path.parent
        self.reminders = ReminderStore(state_dir / "reminders.json")
        self.notes = NoteStore(state_dir / "notes.json")
        self.lists = ListStore(state_dir / "lists.json")
        from venom.session import SessionState

        self.session = SessionState(state_dir / "session.json")
        # Reminders that fired while asleep, awaiting spoken announcement.
        self.pending_reminders: list[str] = []
        # Approximate location (network geo) — warm the cache off-thread so the
        # first conversation has it without blocking on the lookup.
        from venom.location import LocationProvider

        self.location = LocationProvider()
        self.location.warm()
        from venom.music import MusicPlayer

        self.music = MusicPlayer()
        from venom.chess_game import ChessGame

        self.chess = ChessGame()
        from venom.notifications import NotificationHub

        self.notifications = NotificationHub(
            config.phone.ntfy_server, config.phone.notify_topic,
            is_dnd=lambda: self._dnd)
        self.registry = build_pi_registry(config, self.memory, self.timers,
                                          music=self.music,
                                          reminders=self.reminders,
                                          notes=self.notes, lists=self.lists,
                                          location=self.location,
                                          chess=self.chess,
                                          notifications=self.notifications)
        self._detector: WakeWordDetector | None = None
        # True while we've paused our own music for a live conversation, so we
        # only resume what *we* paused (not a track the user paused by hand).
        self._music_ducked = False
        # Physical buttons (set from the event loop, read by the wake loop):
        #   _manual_wake — the headset button asks to start a conversation.
        #   _dnd         — Do-Not-Disturb: ignore wake word + headset button and
        #                  hold proactive timer/reminder chimes until toggled off.
        #   _speaker     — the current lifecycle's speaker, so button handlers
        #                  can chime; None between lifecycles.
        self._manual_wake = asyncio.Event()
        self._dnd = False
        self._speaker: SpeakerStream | None = None
        # The live conversation, set while one is active — so a wake-button
        # press mid-reply becomes a barge-in (interrupt) instead of a queued
        # wake. None between conversations.
        self._session: LiveSession | None = None

    async def run(self) -> None:
        # The wake model takes minutes to load from slow flash — load it in
        # parallel with the (equally slow) first headset hunt. It loads once
        # and survives audio lifecycle rebuilds.
        self._detector = WakeWordDetector(self.config.voice.wake_word,
                                          self.config.voice.wake_threshold)
        self._detector_ready = asyncio.create_task(
            asyncio.to_thread(self._detector.load))

        from venom.buttons import watch_buttons

        # Keep a reference: asyncio only holds tasks weakly, and a
        # garbage-collected watcher means the buttons silently die.
        self._buttons_task = asyncio.create_task(watch_buttons(
            on_wake=self._on_wake_button,
            on_dnd=self._on_dnd_button,
            dnd_code=self.config.buttons.dnd_code,
            wake_code=self.config.buttons.wake_code))

        # Phone notifications (WhatsApp): chime on arrival, read on demand.
        self.notifications.start()

        first_cycle = True
        while True:
            try:
                await self._audio_lifecycle(first_cycle)
            except StreamsDied as exc:
                log.warning("audio lifecycle ended: %s — rebuilding", exc)
            except Exception:
                log.exception("audio lifecycle crashed — rebuilding")
            first_cycle = False
            self.state = "reconnecting"
            await asyncio.sleep(3)

    # ── one full audio lifecycle: headset → streams → listen loop ────────────
    async def _audio_lifecycle(self, first_cycle: bool) -> None:
        loop = asyncio.get_running_loop()

        if self.config.audio.use_bluetooth:
            from venom.audio.routing import pin_bluetooth_audio
            from venom.btaudio import BluetoothHeadset

            self.state = "connecting bluetooth headset"
            headset = BluetoothHeadset(self.config.audio.bluetooth_mac,
                                       self.config.audio.bluetooth_name)
            while not await asyncio.to_thread(headset.wait_for_connection):
                log.warning("headset not connected — put it in pairing mode; retrying")
                self.state = "waiting for headset (pairing mode)"
                await asyncio.sleep(10)

            # The mic only exists in the HFP profile — pin it every connect.
            # A lifecycle without a microphone is useless (the wake loop would
            # sit deaf on the sink monitor), so failure here restarts the cycle.
            self.state = "activating headset microphone"
            if not await asyncio.to_thread(pin_bluetooth_audio, 3.0, 6):
                raise StreamsDied("headset connected but no microphone appeared")
        else:
            # USB (or default) path: make the USB earphone PipeWire's default
            # so the resampling 'pipewire' device routes to it — re-asserted
            # every lifecycle, so a reconnecting Bluetooth device can't keep it.
            from venom.audio.routing import pin_usb_audio

            self.state = "selecting usb audio"
            await asyncio.to_thread(pin_usb_audio)

        # Streams only make sense once the wake model can consume them.
        self.state = "loading wake model"
        await self._detector_ready

        pick = current_devices(bluetooth=self.config.audio.use_bluetooth)
        log.info("audio devices — mic: %s, speaker: %s", pick.input_name, pick.output_name)

        suppressor = None
        if self.config.audio.noise_suppression:
            from venom.audio.denoise import NoiseSuppressor

            suppressor = NoiseSuppressor()

        speaker = SpeakerStream(pick)
        mic = MicStream(pick, loop, suppressor=suppressor)
        speaker.start()
        mic.start()
        self._speaker = speaker  # let button handlers chime this lifecycle
        try:
            chime(speaker)  # audible on every (re)connect: "Venom hears you"
            while True:
                # Pre-warm: open the Gemini Live session (socket + big-prompt
                # prefill, the ~4-5s cold cost) NOW, while we listen for the wake
                # word. It sits idle, off the mic, until we activate it — so the
                # first reply after "Hey Jarvis" is the warm ~1s path every time.
                session = self._build_session(mic, speaker)
                warm_task = asyncio.create_task(session.run())
                self.state = "wake"
                if not await self._wait_for_wake(mic, speaker, warm_task):
                    continue  # warm session dropped before wake — spin a new one
                self.state = "conversation"
                # The music and the mic share one headset, so anything playing
                # bleeds into the mic — Gemini never hears a clean end-of-speech
                # and never replies. Pause our own player for the whole turn;
                # the finally below resumes it when we go back to sleep.
                self._duck_music()
                self._prepare_opening(session)
                chime(speaker)
                chime(speaker, frequency=1320.0)
                session.activate()
                self._session = session  # a wake press now barges in, not wakes
                # FIXED (Fix 2): conversation is now live and speaking — freeze
                # the brain switcher until we're back to wake/pre-warm.
                self.activity.session_active = True
                # Gate the mic to true silence between/after words *only* while
                # talking, so Gemini's own turn detector hears a clean end-of-
                # speech and replies promptly (a body-mic's noise floor otherwise
                # reads as "still talking" and it waits). Off during wake, where
                # pure zeros would look like a dead capture path (see
                # SilenceTracker) and trigger needless stream rebuilds.
                if suppressor is not None:
                    suppressor.gate = True
                try:
                    await warm_task
                except Exception:
                    log.exception("live session ended with error")
                    chime(speaker, frequency=330.0, duration=0.4)
                    await asyncio.sleep(2)
                finally:
                    # FIXED (Fix 2): conversation over — let the brain switcher
                    # resume evaluating in the gap before the next session.
                    self._session = None
                    self.activity.session_active = False
                    if suppressor is not None:
                        suppressor.gate = False
                    self._unduck_music()
                    self._drain(mic)
                self._detector.reset()
                log.info("back to wake listening")
        finally:
            self._speaker = None
            mic.stop()
            speaker.stop()

    async def _wake_phase(self, mic: MicStream, speaker: SpeakerStream) -> None:
        starved = 0.0
        silence = SilenceTracker()
        self._manual_wake.clear()  # ignore any press queued from a past cycle
        while True:
            # Do-Not-Disturb holds proactive alerts: leave due timers/reminders
            # unpopped so they announce the moment DND is toggled back off.
            if not self._dnd:
                for timer in self.timers.pop_due():
                    chime(speaker)
                    chime(speaker, frequency=1100.0)
                    self.timers.add(0, f"(already finished) {timer.label}")
                    log.info("timer fired while asleep: %s", timer.label)
                for reminder in self.reminders.pop_due():
                    chime(speaker)
                    chime(speaker, frequency=880.0)
                    self.pending_reminders.append(reminder["text"])
                    log.info("reminder fired while asleep: %s", reminder["text"])
            if not self.inbox.empty():
                log.info("console prompt while asleep — starting a session")
                self._drain(mic)
                return  # the session's housekeeping delivers the prompt
            # Headset button: an explicit wake, checked every loop (frames arrive
            # ~15x/s) so it feels instant. Ignored under DND.
            if self._manual_wake.is_set():
                self._manual_wake.clear()
                if not self._dnd:
                    log.info("headset button — waking")
                    self._drain(mic)
                    return
            try:
                frame = await asyncio.wait_for(mic.frames.get(), timeout=1.0)
                starved = 0.0
            except TimeoutError:
                starved += 1.0
                if starved >= STARVATION_SECONDS:
                    raise StreamsDied(
                        f"no mic audio for {STARVATION_SECONDS}s (headset gone?)"
                    ) from None
                continue
            if silence.update(frame):
                raise StreamsDied(
                    f"mic delivered pure digital silence for "
                    f"{SILENCE_REBUILD_SECONDS}s (capture path rerouted?)"
                )
            if not self._dnd and await asyncio.to_thread(self._detector.feed, frame):
                log.info("wake word detected")
                self._drain(mic)
                return

    # ── physical button handlers (called on the event loop) ──────────────────
    def _on_wake_button(self) -> None:
        """Wake button (headset or shutter-2) — a toggle: it wakes her when
        she's asleep, and ends the conversation (back to sleep) when one is
        already live. Ignored under DND."""
        if self._dnd:
            log.info("wake button ignored — DND is on")
            return
        session = self._session
        if session is not None and not session.ended:
            log.info("wake button — ending conversation (sleep)")
            session.request_stop()
            return
        self._manual_wake.set()

    def _on_dnd_button(self) -> None:
        """Shutter button 1: toggle Do-Not-Disturb with a distinct two-tone
        chime — falling when going quiet, rising when coming back."""
        self._dnd = not self._dnd
        log.info("DND %s (shutter button)", "on" if self._dnd else "off")
        sp = self._speaker
        if sp is None:
            return
        if self._dnd:                       # entering: high → low (falling)
            chime(sp, frequency=587.0)
            chime(sp, frequency=440.0)
        else:                               # leaving: low → high (rising)
            chime(sp, frequency=440.0)
            chime(sp, frequency=587.0)

    def _duck_music(self) -> None:
        """Pause our own music while a conversation is live so the shared-headset
        mic hears you cleanly. No-op if nothing is playing or the user already
        paused it (so we don't 'resume' a track they stopped by hand)."""
        try:
            if self.music.playing and not self.music.paused:
                self.music.set_paused(True)
                self._music_ducked = True
                log.info("paused music for the conversation")
        except Exception:
            log.exception("could not pause music for the conversation")

    def _unduck_music(self) -> None:
        """Resume music we paused for the conversation, back to sleep."""
        if not self._music_ducked:
            return
        self._music_ducked = False
        try:
            self.music.set_paused(False)
            log.info("resumed music")
        except Exception:
            log.exception("could not resume music after the conversation")

    def _build_session(self, mic: MicStream, speaker: SpeakerStream) -> LiveSession:
        """A pre-warmable session; the opening briefing is decided at wake."""
        return LiveSession(self.config, self.registry, self.memory,
                           self.timers, mic.frames, speaker,
                           inbox=self.inbox, transcript=self.transcript,
                           reminders=self.reminders,
                           pending_reminders=self.pending_reminders,
                           location=self.location, opening=None)

    def _prepare_opening(self, session: LiveSession) -> None:
        """At the moment of waking, decide whether to lead with a briefing."""
        if self.session.should_brief():
            from venom.tools_pi import build_briefing

            session._opening = build_briefing(self.memory, self.timers,
                                              location=self.location,
                                              reminders=self.reminders)
            self.session.mark_briefed()
            log.info("delivering morning briefing")
        else:
            self.session.mark_interaction()

    async def _wait_for_wake(self, mic: MicStream, speaker: SpeakerStream,
                             warm_task: asyncio.Task) -> bool:
        """Listen for the wake word while the session warms in the background.
        True → woken, go converse. False → the warm session died first; the
        caller loops to spin up a fresh one."""
        from venom.live import is_normal_closure

        wake_task = asyncio.create_task(self._wake_phase(mic, speaker))
        done, _pending = await asyncio.wait(
            {wake_task, warm_task}, return_when=asyncio.FIRST_COMPLETED)
        if wake_task in done:
            exc = wake_task.exception()
            if exc is not None:
                raise exc  # StreamsDied → rebuild the whole audio lifecycle
            self._prewarm_fails = 0  # a warm session survived to wake — healthy
            return True
        # Warm session ended before the wake word (server idle-closed it, or it
        # failed to connect). Stop listening; the caller re-warms.
        wake_task.cancel()
        try:
            await wake_task
        except BaseException:
            pass
        exc = None if warm_task.cancelled() else warm_task.exception()
        if exc is not None and not is_normal_closure(exc):
            # Exponential backoff, capped: 0.5 → 1 → 2 → 4 → 5s. First retry is
            # 4x faster than the old fixed 2s (snappy recovery from a transient
            # blip); a sustained outage settles at 5s instead of hammering.
            delay = min(0.5 * 2 ** min(self._prewarm_fails, 4), 5.0)
            self._prewarm_fails += 1
            log.warning("pre-warm session failed: %s — retrying in %.1fs",
                        exc, delay)
            await asyncio.sleep(delay)
        return False

    @staticmethod
    def _drain(mic: MicStream) -> None:
        try:
            while True:
                mic.frames.get_nowait()
        except asyncio.QueueEmpty:
            pass


async def run_voice_forever(config: VenomConfig, set_state, activity=None) -> None:
    """Supervisor entry: keep the voice loop alive across crashes."""
    backoff = 2.0
    console = None
    if config.web_enabled:
        try:
            from venom.web import WebConsole

            console = WebConsole(config.web_port, token=config.web_token)
            console.start()
            if not config.web_token:
                log.warning("web console has NO token — open to anyone on the LAN")
        except Exception:
            log.exception("web console failed to start — continuing without it")
    while True:
        # FIXED (Fix 2): pass the shared activity flag through to every
        # orchestrator instance so brain-switch gating survives voice restarts.
        orchestrator = VoiceOrchestrator(config, activity)
        if console is not None:
            console.attach(orchestrator, asyncio.get_event_loop())
        try:
            set_state("voice: starting")
            started = asyncio.get_event_loop().time()
            task = asyncio.create_task(orchestrator.run())
            while not task.done():
                set_state(f"voice: {orchestrator.state}")
                await asyncio.sleep(1)
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("voice loop crashed: %s", exc)
            ran_for = asyncio.get_event_loop().time() - started
            backoff = 2.0 if ran_for > 60 else min(backoff * 2, 60.0)
            set_state("voice: restarting")
            await asyncio.sleep(backoff)
