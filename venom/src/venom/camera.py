"""Venom's eyes — the Raspberry Pi camera.

Jarvis (the Pi's native-audio voice model) is blind to images, so we can't just
hand her a picture the way we can with text. Instead, when the user asks "what's
in front of me" or "take a shot", we grab a frame off the CSI camera with
libcamera, then send it to Gemini (natively multimodal) for the actual seeing.
The description comes back as text, which Jarvis reads aloud.

Capture uses the `rpicam-still`/`libcamera-still` CLI so there's no Python
picamera dependency to install or break on kernel bumps — it's the tool that
ships with Raspberry Pi OS and talks straight to the camera stack.

Photos taken with `take_photo` are pushed to the user's phone over the same ntfy
channel find-my-phone uses (an image attachment this time), so the shot lands in
their pocket instantly. Nothing is archived on the Pi.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

from flint_core.llm.providers import GeminiProvider
from venom.config import VenomConfig

log = logging.getLogger("venom.camera")

# The libcamera stills tool, newest name first. Raspberry Pi OS renamed
# `libcamera-still` to `rpicam-still` in late 2023; support both.
_STILL_TOOLS = ("rpicam-still", "libcamera-still")
# systemd runs venom.service with a minimal env, so a bare name may not resolve
# even when the binary is installed — search these dirs explicitly too.
_BIN_DIRS = ("/usr/bin", "/usr/local/bin", "/bin")


def _resolve_still_tool() -> str | None:
    """Absolute path to the first available stills binary, or None. Checks PATH
    first (dev/laptop) then the known Pi install dirs (the service's stripped
    PATH can miss /usr/bin at exec time)."""
    for name in _STILL_TOOLS:
        found = shutil.which(name)
        if found:
            return found
        for d in _BIN_DIRS:
            cand = Path(d) / name
            if cand.exists():
                return str(cand)
    return None


class CameraError(RuntimeError):
    """Capture failed — no camera, ribbon unplugged, or libcamera missing."""


# One camera, one capture at a time. The model sometimes fires look_around
# twice in a turn; two rpicam-still processes contending for the sensor makes
# both hang, so the second caller waits for the first instead.
_capture_lock = threading.Lock()


def capture_jpeg(width: int = 1280, height: int = 720,
                 warmup_ms: int = 500, timeout: float = 30.0) -> bytes:
    """Grab a single JPEG frame from the Pi camera, returned as raw bytes.

    `-n` no preview, `-t` warm-up so auto-exposure/white-balance settle before
    the shot, `-o -` writes the JPEG to stdout. Tries each known tool name and
    raises CameraError with the last failure if none work.

    The shot happens mid-conversation, when the voice stack has the CPU pegged
    and pipeline startup can crawl — so pin a small sensor mode (full-res
    2592x1944 readout is the slow path), keep warm-up short, and allow a long
    wall-clock timeout rather than failing a capture that would've landed.
    """
    tool = _resolve_still_tool()
    if not tool:
        raise CameraError("no libcamera stills tool found (rpicam-still)")
    cmd = [tool, "-n", "-t", str(int(warmup_ms)),
           "--mode", "1296:972:10",
           "--width", str(int(width)), "--height", str(int(height)),
           "-q", "85", "-o", "-"]
    try:
        with _capture_lock:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CameraError(f"{tool} timed out — is the camera enabled?") from None
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200] \
        or f"{tool} exited {proc.returncode}"
    raise CameraError(err)


def describe_scene(config: VenomConfig, question: str = "") -> str:
    """Capture a frame and let Gemini describe what's in view. Returns a spoken-
    style sentence (or a graceful excuse Jarvis can read if something failed)."""
    try:
        jpeg = capture_jpeg()
    except CameraError as exc:
        log.warning("camera capture failed: %s", exc)
        return ("I couldn't get a picture from the camera — is it connected and "
                "enabled?")
    import base64

    prompt = (question.strip() or
              "Describe what you see in front of the camera, briefly and "
              "naturally, as if telling a friend what's there.")
    system = ("You are the eyes of a voice assistant. Answer in one or two "
              "short spoken sentences — no markdown, no lists, no preamble.")
    try:
        provider = GeminiProvider(config.gemini_api_key)
        return provider.complete_vision(
            prompt, base64.b64encode(jpeg).decode("ascii"), "image/jpeg",
            provider.vision_models[0], system=system, max_tokens=300)
    except Exception as exc:  # network / API — never crash the voice loop
        log.warning("vision describe failed: %s", exc)
        return "I took a look but couldn't make sense of it just now."


def push_photo(server: str, topic: str, jpeg: bytes, caption: str = "",
               timeout: float = 12.0) -> bool:
    """PUT the JPEG to an ntfy topic as an image attachment. The subscribed
    phone shows it as a picture. Returns True on success."""
    topic = (topic or "").strip()
    if not topic:
        return False
    url = f"{server.rstrip('/')}/{topic}"
    headers = {"Filename": "venom.jpg", "Content-Type": "image/jpeg"}
    if caption.strip():
        # Title rides along as the notification's text above the image.
        headers["Title"] = caption.strip()[:120]
    req = urllib.request.Request(url, data=jpeg, headers=headers, method="PUT")
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort push, log and move on
        log.warning("photo push failed: %s", exc)
        return False


def take_photo(config: VenomConfig, caption: str = "") -> str:
    """Snap a photo, push it to the phone, and describe it aloud. Returns the
    spoken confirmation Jarvis reads back."""
    try:
        jpeg = capture_jpeg()
    except CameraError as exc:
        log.warning("camera capture failed: %s", exc)
        return ("I couldn't take the photo — the camera didn't respond. Is it "
                "connected and enabled?")

    topic = (config.camera.photo_topic or config.phone.ntfy_topic).strip()
    pushed = push_photo(config.phone.ntfy_server, topic, jpeg, caption)

    import base64

    try:
        provider = GeminiProvider(config.gemini_api_key)
        described = provider.complete_vision(
            "Describe this photo in one short, natural spoken sentence.",
            base64.b64encode(jpeg).decode("ascii"), "image/jpeg",
            provider.vision_models[0], max_tokens=200)
    except Exception as exc:  # noqa: BLE001
        log.warning("photo describe failed: %s", exc)
        described = ""

    if pushed and described:
        return f"Got it — sent the photo to your phone. {described}"
    if pushed:
        return "Got it — I've sent the photo to your phone."
    if described:
        return f"Photo taken. {described} (I couldn't send it to your phone though.)"
    return "Photo taken, but I couldn't send it to your phone."
