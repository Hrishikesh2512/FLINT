"""The voice loop: sleep-listening for the wake word, then a live session.

    ┌────────────┐  wake word   ┌──────────────┐  silence / goodbye
    │ WAKE       │ ───────────► │ CONVERSATION │ ─────────────────┐
    │ (oww ~3%   │   (chime)    │ (Gemini Live │                  │
    │  CPU)      │ ◄─────────── │  + tools)    │ ◄────────────────┘
    └────────────┘              └──────────────┘

Timers keep working while asleep: a due timer chimes immediately and is
announced at the start of the next conversation.
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


class VoiceOrchestrator:
    def __init__(self, config: VenomConfig):
        self.config = config
        self.state = "starting"
        self.memory = MemoryStore(config.memory_path)
        self.timers = TimerBoard()
        self.registry = build_pi_registry(config, self.memory, self.timers)
        self._missed_timers: list[str] = []

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        pick = current_devices()
        log.info("audio devices — mic: %s, speaker: %s", pick.input_name, pick.output_name)

        speaker = SpeakerStream(pick)
        speaker.start()
        mic = MicStream(pick, loop)
        mic.start()

        detector = WakeWordDetector(self.config.voice.wake_word,
                                    self.config.voice.wake_threshold)
        await asyncio.to_thread(detector.load)

        chime(speaker)  # audible "Venom is up" on boot
        try:
            while True:
                self.state = "wake"
                await self._wake_phase(mic, speaker, detector)
                self.state = "conversation"
                chime(speaker)
                chime(speaker, frequency=1320.0)
                await self._conversation_phase(mic, speaker)
                detector.reset()
        finally:
            self.state = "stopped"
            mic.stop()
            speaker.stop()

    async def _wake_phase(self, mic: MicStream, speaker: SpeakerStream,
                          detector: WakeWordDetector) -> None:
        while True:
            for timer in self.timers.pop_due():
                chime(speaker)
                chime(speaker, frequency=1100.0)
                self._missed_timers.append(timer.label)
                log.info("timer fired while asleep: %s", timer.label)
            try:
                frame = await asyncio.wait_for(mic.frames.get(), timeout=1.0)
            except TimeoutError:
                continue
            if await asyncio.to_thread(detector.feed, frame):
                log.info("wake word detected")
                self._drain(mic)
                return

    async def _conversation_phase(self, mic: MicStream, speaker: SpeakerStream) -> None:
        session = LiveSession(self.config, self.registry, self.memory,
                              self.timers, mic.frames, speaker)
        if self._missed_timers:
            labels = ", ".join(self._missed_timers)
            self._missed_timers.clear()
            for label in labels.split(", "):
                self.timers.add(0, f"(already finished) {label}")
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
    while True:
        orchestrator = VoiceOrchestrator(config)
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
