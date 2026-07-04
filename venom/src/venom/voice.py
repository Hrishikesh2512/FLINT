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
    def __init__(self, config: VenomConfig):
        from collections import deque

        self.config = config
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
        # Reminders that fired while asleep, awaiting spoken announcement.
        self.pending_reminders: list[str] = []
        from venom.music import MusicPlayer

        self.music = MusicPlayer()
        self.registry = build_pi_registry(config, self.memory, self.timers,
                                          music=self.music,
                                          reminders=self.reminders,
                                          notes=self.notes, lists=self.lists)
        self._detector: WakeWordDetector | None = None

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
        # garbage-collected watcher means the headset buttons silently die.
        self._buttons_task = asyncio.create_task(watch_buttons(self.music))

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
        try:
            chime(speaker)  # audible on every (re)connect: "Venom hears you"
            while True:
                self.state = "wake"
                await self._wake_phase(mic, speaker)
                self.state = "conversation"
                chime(speaker)
                chime(speaker, frequency=1320.0)
                await self._conversation_phase(mic, speaker)
                self._detector.reset()
        finally:
            mic.stop()
            speaker.stop()

    async def _wake_phase(self, mic: MicStream, speaker: SpeakerStream) -> None:
        starved = 0.0
        silence = SilenceTracker()
        while True:
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
            if await asyncio.to_thread(self._detector.feed, frame):
                log.info("wake word detected")
                self._drain(mic)
                return

    async def _conversation_phase(self, mic: MicStream, speaker: SpeakerStream) -> None:
        session = LiveSession(self.config, self.registry, self.memory,
                              self.timers, mic.frames, speaker,
                              inbox=self.inbox, transcript=self.transcript,
                              reminders=self.reminders,
                              pending_reminders=self.pending_reminders)
        try:
            await session.run()
        except Exception:
            log.exception("live session ended with error")
            chime(speaker, frequency=330.0, duration=0.4)
            await asyncio.sleep(2)
        finally:
            self._drain(mic)
        log.info("back to wake listening")

    @staticmethod
    def _drain(mic: MicStream) -> None:
        try:
            while True:
                mic.frames.get_nowait()
        except asyncio.QueueEmpty:
            pass


async def run_voice_forever(config: VenomConfig, set_state) -> None:
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
        orchestrator = VoiceOrchestrator(config)
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
