"""Screen-text server for Venom — local OCR of your screen, served on the LAN.

Jarvis (the Pi's native-audio voice model) is blind to images but reads text
fine. So instead of shipping a picture, this OCRs your laptop's *active window*
locally and hands back only the extracted text when the Pi asks. You say
"Jarvis, look at my screen"; she calls her look_at_screen tool; it hits this
server; you get a spoken read/debug of whatever is focused.

Design goals: fast (warm model, active-window crop, tiny cache) and private
(nothing leaves the machine but the text, and only with the matching token).

Run it on the laptop:

    pip install -r requirements.txt
    python screen_server.py --token <same-token-as-venom.toml>

Then in the Pi's venom.toml:

    [screen]
    host  = "192.168.1.50"   # this laptop's LAN/Tailscale address
    port  = 8766
    token = "<same-token>"

Endpoints:
    GET /screen_text?token=... -> {"ok": true, "engine", "ms", "chars", "text"}
    GET /health                -> {"ok": true, "engine"}
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np  # hard dep (capture + OCR both need it)

log = logging.getLogger("screen_server")

# ── screen capture ───────────────────────────────────────────────────────────
# Two backends; whichever imports. mss is fast and cross-platform, PIL's
# ImageGrab is the fallback (Windows/macOS).
try:
    import mss  # type: ignore
    _MSS = True
except Exception:  # pragma: no cover - optional dep
    _MSS = False
try:
    from PIL import ImageGrab  # type: ignore
    _PIL = True
except Exception:  # pragma: no cover
    _PIL = False

IS_WINDOWS = sys.platform.startswith("win")


def _set_dpi_aware() -> None:
    """So window rects and captures are in real (physical) pixels on HiDPI."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _active_window_rect():
    """(left, top, right, bottom) of the focused window, or None for full screen."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    # DWM extended frame bounds excludes the drop shadow — a tighter crop.
    try:
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect))
    except Exception:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    if rect.right - rect.left < 40 or rect.bottom - rect.top < 40:
        return None  # minimised / bogus — fall back to full screen
    return (rect.left, rect.top, rect.right, rect.bottom)


def grab_screen():
    """Capture the active window (or full primary screen) as an RGB ndarray."""
    box = _active_window_rect()
    if _MSS:
        with mss.mss() as sct:
            if box is None:
                mon = sct.monitors[1]
            else:
                left, top, right, bottom = box
                mon = {"left": left, "top": top,
                       "width": right - left, "height": bottom - top}
            shot = sct.grab(mon)
            arr = np.asarray(shot)[:, :, :3]  # BGRA -> BGR
            return arr[:, :, ::-1]            # BGR -> RGB
    if _PIL:
        img = ImageGrab.grab(bbox=box)
        return np.asarray(img.convert("RGB"))
    raise RuntimeError("no capture backend — pip install mss (or Pillow)")


# ── OCR engines (warm, pluggable) ────────────────────────────────────────────
# Primary on Windows: the native Windows.Media.Ocr — hardware-accelerated,
# ~40-120ms, English-fast, and more accurate on crisp screen text (measured
# vs RapidOCR: 40ms vs ~700-5900ms, and no CJK false positives). RapidOCR is
# the cross-platform fallback (Linux/macOS, or if winsdk is missing).
class WindowsOcr:
    name = "windows-ocr"

    def __init__(self) -> None:
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine

        self._OcrEngine = OcrEngine
        self._engine = (OcrEngine.try_create_from_user_profile_languages()
                        or OcrEngine.try_create_from_language(Language("en-US")))
        if self._engine is None:
            raise RuntimeError("no Windows OCR language pack available")
        self._lock = threading.Lock()

    def read(self, rgb) -> str:
        import asyncio
        with self._lock:
            return asyncio.run(self._read_async(rgb))

    async def _read_async(self, rgb) -> str:
        import io

        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.storage.streams import (
            DataWriter, InMemoryRandomAccessStream)
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="PNG")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(buf.getvalue())
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bmp = await decoder.get_software_bitmap_async()
        result = await self._engine.recognize_async(bmp)
        return "\n".join(line.text for line in result.lines).strip()

    def warm(self) -> None:
        try:
            self.read(np.zeros((64, 240, 3), dtype=np.uint8))
        except Exception:
            pass


class RapidOcr:
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self._engine = RapidOCR()
        self._lock = threading.Lock()  # ORT session isn't reentrant-friendly

    def read(self, rgb) -> str:
        with self._lock:
            result, _ = self._engine(rgb)
        if not result:
            return ""
        return "\n".join(line[1] for line in result).strip()

    def warm(self) -> None:
        try:
            self.read(np.zeros((64, 240, 3), dtype=np.uint8))
        except Exception:
            pass


def build_ocr():
    """Best available OCR: native Windows first, then RapidOCR."""
    if IS_WINDOWS:
        try:
            return WindowsOcr()
        except Exception as exc:
            log.warning("Windows OCR unavailable (%s) — using RapidOCR", exc)
    return RapidOcr()


# ── HTTP server ──────────────────────────────────────────────────────────────
class State:
    ocr: object          # WindowsOcr | RapidOcr (duck-typed: .read/.warm/.name)
    token: str
    max_width: int = 1800   # downscale wider grabs to bound OCR time
    cache_ttl: float = 0.75  # dedupe a double tool-call within this window
    _cache: tuple[float, dict] | None = None
    _cache_lock = threading.Lock()


def _ocr_snapshot() -> dict:
    """Capture + OCR once, with a tiny time-boxed cache."""
    now = time.monotonic()
    with State._cache_lock:
        if State._cache and now - State._cache[0] < State.cache_ttl:
            return State._cache[1]
    t0 = time.monotonic()
    rgb = grab_screen()
    # Downscale very wide captures (4K/ultrawide) — screen text stays legible
    # well below native and OCR cost scales with pixel count.
    h, w = rgb.shape[:2]
    if w > State.max_width:
        scale = State.max_width / w
        try:
            from PIL import Image as _I
            rgb = np.asarray(_I.fromarray(rgb).resize(
                (State.max_width, int(h * scale))))
        except Exception:
            rgb = rgb[:, ::2, :] if w > 2 * State.max_width else rgb
    text = State.ocr.read(rgb)
    payload = {
        "ok": True,
        "engine": State.ocr.name,
        "ms": round((time.monotonic() - t0) * 1000),
        "chars": len(text),
        "text": text,
    }
    with State._cache_lock:
        State._cache = (time.monotonic(), payload)
    return payload


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "engine": State.ocr.name})
            return
        if parsed.path != "/screen_text":
            self._send(404, {"ok": False, "error": "not found"})
            return
        token = (parse_qs(parsed.query).get("token", [""])[0])
        if State.token and token != State.token:
            self._send(403, {"ok": False, "error": "bad token"})
            return
        try:
            self._send(200, _ocr_snapshot())
        except Exception as exc:
            log.exception("OCR failed")
            self._send(500, {"ok": False, "error": str(exc)})

    def log_message(self, *_args) -> None:  # quiet; we log our own line
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Venom screen-text (OCR) server")
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default all interfaces)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--token", default=os.environ.get("VENOM_SCREEN_TOKEN", ""),
                    help="shared secret; must match venom.toml [screen].token")
    ap.add_argument("--max-width", type=int, default=1800)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _set_dpi_aware()

    log.info("loading OCR engine…")
    State.ocr = build_ocr()
    State.ocr.warm()
    State.token = args.token.strip()
    State.max_width = args.max_width
    if not State.token:
        log.warning("no --token: anyone on the LAN can read your screen text")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("screen server ready on http://%s:%d  (engine=%s)",
             args.host, args.port, State.ocr.name)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        httpd.shutdown()


if __name__ == "__main__":
    main()
