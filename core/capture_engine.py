"""Smart capture engine: frame-difference detection + encoded-token caching.

Every screen/camera capture in FLINT should go through this module instead of
calling mss / cv2 / pyautogui directly.  The engine keeps the last raw frame
in memory; when a new capture is requested it compares a downscaled grayscale
version of both frames and, if the pixel delta is below the change threshold,
returns the previously encoded JPEG and base64 token without re-encoding.

That makes repeated vision calls (screen_find loops, watch-mode polling,
back-to-back "what's on my screen" requests) effectively free between actual
screen changes.

Usage:
    from core.capture_engine import get_engine

    frame = get_engine().capture_screen()
    frame.jpeg     # encoded JPEG bytes (cached when unchanged)
    frame.b64      # base64 token (lazily encoded, cached)
    frame.changed  # False -> pixels identical to previous capture
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
from dataclasses import dataclass, field

import numpy as np

try:
    import mss
    _MSS_OK = True
except ImportError:
    _MSS_OK = False

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    import PIL.Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False


# Encoding parameters — match what the vision models can usefully consume.
IMG_MAX_W = 640
IMG_MAX_H = 360
JPEG_Q    = 55

# Frame-difference parameters.  Frames are compared on a small grayscale
# thumbnail; a mean absolute delta below CHANGE_THRESHOLD counts as "same".
DIFF_W, DIFF_H   = 96, 54
CHANGE_THRESHOLD = 2.0      # 0-255 scale; ~blinking cursor stays below this
CACHE_TTL        = 30.0     # seconds before a cached frame is considered stale


@dataclass
class Frame:
    """One capture result.  ``jpeg`` is always populated; ``b64`` is lazy."""
    jpeg: bytes
    mime: str = "image/jpeg"
    changed: bool = True            # pixels differ from the previous capture
    cached: bool = False            # served from cache without re-encoding
    digest: str = ""                # sha1 of the jpeg payload
    captured_at: float = field(default_factory=time.time)
    _b64: str | None = None

    @property
    def b64(self) -> str:
        if self._b64 is None:
            self._b64 = base64.b64encode(self.jpeg).decode("ascii")
        return self._b64


class CaptureEngine:
    """Thread-safe capture front-end with per-source frame caches."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tls = threading.local()           # mss handles are not thread-safe
        self._last_small: dict[str, np.ndarray] = {}
        self._last_frame: dict[str, Frame] = {}
        self.stats = {
            "captures": 0,
            "cache_hits": 0,
            "encodes": 0,
            "bytes_saved": 0,
        }

    # ── raw grabs ────────────────────────────────────────────────────────────
    def _mss(self):
        sct = getattr(self._tls, "sct", None)
        if sct is None:
            if not _MSS_OK:
                raise RuntimeError("mss is not installed — cannot capture screen")
            sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
            self._tls.sct = sct
        return sct

    def _grab_screen_rgb(self) -> np.ndarray:
        sct = self._mss()
        shot = sct.grab(sct.monitors[1])
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return arr[:, :, 2::-1]                  # BGRA -> RGB view

    def _grab_camera_rgb(self, camera_index: int) -> np.ndarray:
        if not _CV2_OK:
            raise RuntimeError("opencv-python is not installed — cannot capture camera")
        cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"Camera could not be opened: index {camera_index}")
        try:
            for _ in range(5):                  # let auto-exposure settle
                cap.read()
            ret, frame = cap.read()
        finally:
            cap.release()
        if not ret or frame is None:
            raise RuntimeError("Could not capture camera frame.")
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    # ── frame differencing ───────────────────────────────────────────────────
    @staticmethod
    def _thumb_gray(rgb: np.ndarray) -> np.ndarray:
        """Downscale by strided sampling, then collapse to grayscale floats."""
        sy = max(1, rgb.shape[0] // DIFF_H)
        sx = max(1, rgb.shape[1] // DIFF_W)
        small = rgb[::sy, ::sx].astype(np.float32)
        return small.mean(axis=2)

    @staticmethod
    def frame_delta(a: np.ndarray | None, b: np.ndarray | None) -> float:
        """Mean absolute pixel delta between two thumbnails (0-255)."""
        if a is None or b is None or a.shape != b.shape:
            return 255.0
        return float(np.abs(a - b).mean())

    # ── encoding ─────────────────────────────────────────────────────────────
    def _encode(self, rgb: np.ndarray) -> bytes:
        self.stats["encodes"] += 1
        if _PIL_OK:
            img = PIL.Image.fromarray(rgb)
            img.thumbnail((IMG_MAX_W, IMG_MAX_H), PIL.Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_Q, optimize=False)
            return buf.getvalue()
        if _CV2_OK:
            h, w = rgb.shape[:2]
            scale = min(IMG_MAX_W / w, IMG_MAX_H / h, 1.0)
            if scale < 1.0:
                rgb = cv2.resize(rgb, (int(w * scale), int(h * scale)),
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                                   [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
            if not ok:
                raise RuntimeError("JPEG encode failed")
            return buf.tobytes()
        raise RuntimeError("Neither Pillow nor OpenCV available for encoding")

    # ── public API ───────────────────────────────────────────────────────────
    def _capture(self, source: str, grab, force: bool) -> Frame:
        rgb = grab()
        thumb = self._thumb_gray(rgb)

        with self._lock:
            self.stats["captures"] += 1
            prev_thumb = self._last_small.get(source)
            prev_frame = self._last_frame.get(source)
            delta = self.frame_delta(prev_thumb, thumb)
            fresh_enough = (prev_frame is not None
                            and time.time() - prev_frame.captured_at < CACHE_TTL)

            if not force and fresh_enough and delta < CHANGE_THRESHOLD:
                self.stats["cache_hits"] += 1
                self.stats["bytes_saved"] += len(prev_frame.jpeg)
                return Frame(
                    jpeg=prev_frame.jpeg,
                    mime=prev_frame.mime,
                    changed=False,
                    cached=True,
                    digest=prev_frame.digest,
                    captured_at=prev_frame.captured_at,
                    _b64=prev_frame._b64,       # reuse the encoded token too
                )

        jpeg = self._encode(rgb)
        frame = Frame(
            jpeg=jpeg,
            changed=True,
            cached=False,
            digest=hashlib.sha1(jpeg).hexdigest(),
        )
        with self._lock:
            self._last_small[source] = thumb
            self._last_frame[source] = frame
        return frame

    def capture_screen(self, force: bool = False) -> Frame:
        return self._capture("screen", self._grab_screen_rgb, force)

    def capture_camera(self, camera_index: int = 0, force: bool = False) -> Frame:
        return self._capture(f"camera{camera_index}",
                             lambda: self._grab_camera_rgb(camera_index), force)

    def capture_screen_fullres_png(self) -> bytes:
        """Full-resolution PNG for screenshots saved to disk (no cache)."""
        sct = self._mss()
        shot = sct.grab(sct.monitors[1])
        import mss.tools
        return mss.tools.to_png(shot.rgb, shot.size)

    def screen_changed(self) -> bool:
        """Cheap poll: grab + diff only, no encoding.  Updates the baseline."""
        thumb = self._thumb_gray(self._grab_screen_rgb())
        with self._lock:
            delta = self.frame_delta(self._last_small.get("screen"), thumb)
            self._last_small["screen"] = thumb
        return delta >= CHANGE_THRESHOLD

    def snapshot_stats(self) -> dict:
        with self._lock:
            s = dict(self.stats)
        total = max(1, s["captures"])
        s["hit_rate"] = round(100.0 * s["cache_hits"] / total, 1)
        return s


_engine: CaptureEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> CaptureEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = CaptureEngine()
        return _engine


if __name__ == "__main__":
    eng = get_engine()
    t0 = time.perf_counter()
    f1 = eng.capture_screen()
    t1 = time.perf_counter()
    f2 = eng.capture_screen()
    t2 = time.perf_counter()
    print(f"first  : {len(f1.jpeg):6d} B  changed={f1.changed}  {1000*(t1-t0):.1f} ms")
    print(f"second : {len(f2.jpeg):6d} B  changed={f2.changed}  cached={f2.cached}  {1000*(t2-t1):.1f} ms")
    print("stats  :", eng.snapshot_stats())
