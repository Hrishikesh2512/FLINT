"""Microphone capture and speaker playback for the voice loop.

Mic frames land in an asyncio queue with drop-oldest backpressure (the
live uplink must always carry the freshest audio). Playback runs a
RawOutputStream fed from a thread-safe buffer that can be flushed
instantly when the model is interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import queue as queue_mod

from venom.audio.devices import (
    CHANNELS,
    MIC_BLOCK,
    MIC_SAMPLE_RATE,
    SPEAKER_SAMPLE_RATE,
    DevicePick,
)

log = logging.getLogger("venom.audio")


class MicStream:
    """16 kHz mono int16 capture; frames arrive on an asyncio.Queue."""

    def __init__(self, pick: DevicePick, loop: asyncio.AbstractEventLoop,
                 max_queued_blocks: int = 32):
        self._pick = pick
        self._loop = loop
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queued_blocks)
        self._stream = None
        self._drops = 0
        self.muted = False

    def _enqueue(self, data: bytes) -> None:
        # runs on the event loop via call_soon_threadsafe
        if self.frames.full():
            try:
                self.frames.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._drops += 1
            if self._drops % 50 == 1:
                log.warning("mic uplink congested — dropped %d stale blocks", self._drops)
        self.frames.put_nowait(data)

    def start(self) -> None:
        import sounddevice as sd

        def callback(indata, _frames, _time, status):
            if status:
                log.debug("mic status: %s", status)
            if not self.muted:
                self._loop.call_soon_threadsafe(self._enqueue, bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=MIC_SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=MIC_BLOCK, device=self._pick.input_index, callback=callback,
        )
        self._stream.start()
        log.info("mic open: %s", self._pick.input_name)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class SpeakerStream:
    """24 kHz mono int16 playback with instant flush on interruption."""

    def __init__(self, pick: DevicePick):
        self._pick = pick
        self._buffer: queue_mod.Queue[bytes] = queue_mod.Queue()
        self._pending = b""
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd

        def callback(outdata, frames, _time, status):
            if status:
                log.debug("speaker status: %s", status)
            needed = frames * 2  # int16 mono
            chunk = self._pending
            while len(chunk) < needed:
                try:
                    chunk += self._buffer.get_nowait()
                except queue_mod.Empty:
                    break
            out, self._pending = chunk[:needed], chunk[needed:]
            out = out.ljust(needed, b"\x00")
            outdata[:] = out

        self._stream = sd.RawOutputStream(
            samplerate=SPEAKER_SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=MIC_BLOCK, device=self._pick.output_index, callback=callback,
        )
        self._stream.start()
        log.info("speaker open: %s", self._pick.output_name)

    def play(self, pcm: bytes) -> None:
        self._buffer.put_nowait(pcm)

    def flush(self) -> None:
        """Drop everything queued — the user interrupted the model."""
        self._pending = b""
        try:
            while True:
                self._buffer.get_nowait()
        except queue_mod.Empty:
            pass

    @property
    def playing(self) -> bool:
        return bool(self._pending) or not self._buffer.empty()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def chime(speaker: SpeakerStream, frequency: float = 880.0,
          duration: float = 0.18, volume: float = 0.3) -> None:
    """A short sine beep — wake acknowledgment and timer alarm, no TTS needed."""
    import math

    n = int(SPEAKER_SAMPLE_RATE * duration)
    amplitude = int(32767 * volume)
    samples = bytearray()
    for i in range(n):
        fade = min(1.0, (n - i) / (n * 0.3))  # quick fade-out, no click
        value = int(amplitude * fade * math.sin(
            2 * math.pi * frequency * i / SPEAKER_SAMPLE_RATE))
        samples += value.to_bytes(2, "little", signed=True)
    speaker.play(bytes(samples))
