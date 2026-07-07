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
                 max_queued_blocks: int = 32, suppressor=None):
        self._pick = pick
        self._loop = loop
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queued_blocks)
        self._stream = None
        self._drops = 0
        self.muted = False
        self._suppressor = suppressor  # optional NoiseSuppressor

    def _enqueue(self, data: bytes) -> None:
        # runs on the event loop via call_soon_threadsafe
        if self._suppressor is not None:
            data = self._suppressor.process(data)
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
    """24 kHz mono int16 playback with instant flush on interruption.

    Network audio arrives in bursts; playing the instant bytes land turns
    every jitter hiccup into an audible crackle. After an underrun the
    stream holds silence until ~PREBUFFER_MS has accumulated (or the data
    has waited MAX_HOLD_MS — short tails still play), then runs gapless.
    """

    PREBUFFER_MS = 120
    MAX_HOLD_MS = 250

    def __init__(self, pick: DevicePick):
        self._pick = pick
        self._buffer: queue_mod.Queue[bytes] = queue_mod.Queue()
        self._pending = b""
        self._stream = None
        self._starved = True   # start in "wait for prebuffer" state
        self._held_ms = 0.0

    def _fill(self, needed: int) -> bytes:
        """Next `needed` bytes for the device (silence-padded), advancing
        the jitter-buffer state. Runs in the audio callback."""
        chunk = self._pending
        while True:
            try:
                chunk += self._buffer.get_nowait()
            except queue_mod.Empty:
                break

        bytes_per_ms = (SPEAKER_SAMPLE_RATE * 2) / 1000
        if self._starved and chunk:
            self._held_ms += needed / bytes_per_ms
            if (len(chunk) >= needed + int(self.PREBUFFER_MS * bytes_per_ms)
                    or self._held_ms >= self.MAX_HOLD_MS):
                self._starved = False
                self._held_ms = 0.0
        if self._starved:
            self._pending = chunk
            return b"\x00" * needed

        out, self._pending = chunk[:needed], chunk[needed:]
        if not self._pending:
            self._starved = True  # drained — re-buffer before the next burst
            self._held_ms = 0.0
        return out.ljust(needed, b"\x00")

    def start(self) -> None:
        import sounddevice as sd

        def callback(outdata, frames, _time, status):
            if status:
                log.debug("speaker status: %s", status)
            outdata[:] = self._fill(frames * 2)  # int16 mono

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
        self._starved = True
        self._held_ms = 0.0
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
    """A short sine beep — wake acknowledgment and timer alarm, no TTS needed.

    Vectorised with numpy: the old per-sample Python loop synthesised ~4300
    samples on the event-loop thread, stalling the audio uplink/downlink for a
    few ms right at wake (two chimes) and on every timer/reminder. numpy makes
    it effectively instant; the waveform is byte-for-byte the same (int16
    truncation toward zero matches the original int() cast).
    """
    import numpy as np

    n = int(SPEAKER_SAMPLE_RATE * duration)
    amplitude = int(32767 * volume)
    i = np.arange(n)
    fade = np.minimum(1.0, (n - i) / (n * 0.3))  # quick fade-out, no click
    wave = amplitude * fade * np.sin(2 * np.pi * frequency * i / SPEAKER_SAMPLE_RATE)
    speaker.play(wave.astype("<i2").tobytes())
