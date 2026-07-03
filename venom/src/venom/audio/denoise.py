"""Lightweight microphone cleanup for a body-worn headset.

Two cheap, always-safe stages on 16 kHz int16 mono frames:

  1. A high-pass biquad (RBJ cookbook, Butterworth Q) that removes
     sub-cutoff energy — clothing rustle, wind, handling thumps, and DC
     offset, all of which sit below speech and none of which a narrowband
     mic should be spending its dynamic range on.
  2. A gentle downward expander that tracks the noise floor and pulls
     down frames that are essentially just hiss between words, without
     ever fully gating (min_gain floor) so soft speech onsets survive.

Pure and stateful per stream (no globals), so it is fully unit-testable
and — critically — degrades to an exact passthrough if numpy is missing
or anything goes wrong. It can never turn a live mic silent.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger("venom.denoise")


class NoiseSuppressor:
    def __init__(self, sample_rate: int = 16000, highpass_hz: float = 100.0,
                 expander: bool = True, min_gain: float = 0.5,
                 open_ratio: float = 4.0):
        self.sample_rate = sample_rate
        self.highpass_hz = highpass_hz
        self.expander = expander
        self.min_gain = min_gain
        self.open_ratio = open_ratio  # rms/floor at which gain reaches 1.0
        self._floor = 200.0   # rolling noise-floor RMS estimate
        self._gain = 1.0      # smoothed applied gain
        self._b, self._a = self._highpass_coeffs(highpass_hz, sample_rate)
        self._zi = None       # filter delay state, carried across frames

    @staticmethod
    def _highpass_coeffs(fc: float, fs: float):
        w0 = 2 * math.pi * fc / fs
        cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
        alpha = sin_w0 / (2 * 0.70710678)  # Butterworth
        a0 = 1 + alpha
        b = [(1 + cos_w0) / 2 / a0, -(1 + cos_w0) / a0, (1 + cos_w0) / 2 / a0]
        a = [1.0, -2 * cos_w0 / a0, (1 - alpha) / a0]
        return b, a

    def process(self, pcm: bytes) -> bytes:
        """Clean one mic frame. Returns the input unchanged on any failure."""
        try:
            import numpy as np
            from scipy.signal import lfilter, lfilter_zi
        except ImportError:
            return pcm
        try:
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
            if samples.size == 0:
                return pcm
            if self._zi is None:
                self._zi = lfilter_zi(self._b, self._a) * samples[0]
            out, self._zi = lfilter(self._b, self._a, samples, zi=self._zi)

            if self.expander:
                out = self._expand(out, np)

            return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
        except Exception as exc:  # never let cleanup kill the mic
            log.debug("denoise passthrough (%s)", exc)
            return pcm

    def _expand(self, samples, np):
        rms = float(np.sqrt(np.mean(samples * samples))) + 1e-9
        # Track the floor: fall fast toward quiet frames, rise slowly.
        if rms < self._floor:
            self._floor += (rms - self._floor) * 0.2
        else:
            self._floor += (rms - self._floor) * 0.02
        self._floor = max(1.0, self._floor)

        open_at = self._floor * self.open_ratio
        target = 1.0 if rms >= open_at else max(
            self.min_gain, self.min_gain + (1 - self.min_gain) * (rms / open_at))
        # Smooth gain changes (fast attack, slow release) to avoid pumping.
        coeff = 0.5 if target > self._gain else 0.15
        self._gain += (target - self._gain) * coeff
        return samples * self._gain
