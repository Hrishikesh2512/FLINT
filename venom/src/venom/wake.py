"""Wake word detection (openWakeWord) + session inactivity endpointing.

openWakeWord runs a small tflite/onnx model on 80 ms frames — a few
percent of one Pi core. The pretrained models ("hey_jarvis", "alexa",
"hey_mycroft") ship with the package; the model name comes from config.

InactivityTimer is the session endpointer: Gemini Live does in-turn VAD
and interruption handling itself, so the only local decision is "nobody
has said or heard anything for N seconds — close the session and go back
to wake listening". Pure logic, injectable clock, fully testable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

log = logging.getLogger("venom.wake")

WAKE_FRAME_SAMPLES = 1280  # 80 ms @ 16 kHz — what openWakeWord expects
WAKE_FRAME_BYTES = WAKE_FRAME_SAMPLES * 2


class WakeWordDetector:
    """Buffers arbitrary mic chunks into 80 ms frames and scores them."""

    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.6):
        self.model_name = model_name
        self.threshold = threshold
        self._buffer = b""
        self._model = None

    def load(self) -> None:
        import numpy as np  # noqa: F401  (openwakeword needs it at runtime)
        from openwakeword.model import Model

        self._model = Model(wakeword_models=[self.model_name], inference_framework="onnx")
        log.info("wake word model loaded: %s (threshold %.2f)",
                 self.model_name, self.threshold)

    def reset(self) -> None:
        self._buffer = b""
        if self._model is not None:
            self._model.reset()

    @staticmethod
    def _normalize(audio):
        """Boost quiet speech toward full scale before scoring.

        Narrowband Bluetooth mics (8 kHz CVSD) deliver soft, muffled audio
        that scores poorly; amplifying quiet-but-real signal recovers a lot
        of detection margin. Silence stays silent (no gain below the floor),
        loud audio is left untouched.
        """
        import numpy as np

        samples = audio.astype(np.float32)
        peak = float(np.abs(samples).max())
        if 300.0 < peak < 16000.0:
            samples *= min(8.0, 16000.0 / peak)
        return np.clip(samples, -32768, 32767).astype(np.int16)

    def feed(self, chunk: bytes) -> bool:
        """Add mic audio; True the moment the wake word is detected."""
        if self._model is None:
            raise RuntimeError("WakeWordDetector.load() not called")
        import numpy as np

        self._buffer += chunk
        detected = False
        while len(self._buffer) >= WAKE_FRAME_BYTES:
            frame, self._buffer = (
                self._buffer[:WAKE_FRAME_BYTES], self._buffer[WAKE_FRAME_BYTES:]
            )
            audio = self._normalize(np.frombuffer(frame, dtype=np.int16))
            scores = self._model.predict(audio)
            score = max(scores.values()) if scores else 0.0
            if score >= self.threshold:
                detected = True
        return detected


class InactivityTimer:
    """Tracks conversation liveliness; expires after `timeout` idle seconds."""

    def __init__(self, timeout: float, clock: Callable[[], float] = time.monotonic):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self._clock = clock
        self._last_activity = clock()

    def touch(self) -> None:
        self._last_activity = self._clock()

    @property
    def idle_for(self) -> float:
        return self._clock() - self._last_activity

    @property
    def expired(self) -> bool:
        return self.idle_for >= self.timeout
