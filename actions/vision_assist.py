"""Deeper screen / vision features for FLINT.

Where ``screen_processor`` streams an image into the live audio session for a
quick conversational "what's on my screen", this module adds the *focused*
vision skills people actually reach for while working:

    read   — transcribe on-screen text verbatim and copy it to the clipboard
    explain— explain the active window / chart / diagram in plain terms
    error  — find an on-screen error or exception and suggest a concrete fix
    find   — locate a UI element and say where it is (and its coordinates)
    diff   — describe what changed on screen since the last vision look
    watch  — poll the screen and announce out loud when it changes
    stop_watch — stop an active watch

Everything routes through the shared capture engine (frame-diff cached, so
back-to-back looks at an unchanged screen are nearly free) and the shared
``or_client`` vision call (provider-agnostic, with model fallback).  No new
third-party dependencies are introduced — OCR-style reading is done by the
vision model itself, so it works on any OS the rest of FLINT runs on.
"""

from __future__ import annotations

import base64
import re
import threading
import time

from core.capture_engine import get_engine


# ── module state ─────────────────────────────────────────────────────────────
# Remembered so `diff` can compare "now" against "the last time you looked".
_last_look:  dict | None = None          # {"b64", "mime", "summary", "at"}
_look_lock = threading.Lock()

# Background watcher (one at a time).
_watch_thread: threading.Thread | None = None
_watch_stop:   threading.Event | None  = None


def _vision(prompt: str, b64: str, mime: str, system: str, max_tokens: int = 700) -> str:
    """One-shot vision call via the shared, provider-agnostic client."""
    from or_client import client
    text = client.vision(prompt, image_b64=b64, mime=mime, system=system,
                          max_tokens=max_tokens)
    return (text or "").strip()


def _screen_frame():
    """Cached, downscaled screen frame — cheap for repeated looks."""
    return get_engine().capture_screen()


def _fullres_b64() -> tuple[str, str]:
    """Full-resolution PNG of the screen — used for verbatim text reading."""
    png = get_engine().capture_screen_fullres_png()
    return base64.b64encode(png).decode("ascii"), "image/png"


def _remember_look(b64: str, mime: str, summary: str) -> None:
    global _last_look
    with _look_lock:
        _last_look = {"b64": b64, "mime": mime, "summary": summary, "at": time.time()}


def _log(player, msg: str) -> None:
    if player:
        try:
            player.write_log(msg)
        except Exception:
            pass


# ── individual skills ────────────────────────────────────────────────────────
def _read(player) -> str:
    """Transcribe on-screen text exactly and copy it to the clipboard."""
    b64, mime = _fullres_b64()
    text = _vision(
        "Transcribe ALL readable text in this screenshot exactly as it appears, "
        "preserving line breaks and reading order. Do not summarise, translate, "
        "or add commentary. If there is no text, reply with the single word NONE.",
        b64, mime,
        system="You are a precise OCR engine. Output only the verbatim text.",
        max_tokens=1500,
    )
    if not text or text.strip().upper() == "NONE":
        return "Sir, I could not find any readable text on the screen."

    copied = False
    try:
        import pyperclip
        pyperclip.copy(text)
        copied = True
    except Exception as e:
        print(f"[VisionAssist] clipboard copy failed: {e}")

    _remember_look(b64, mime, "screen text read")
    lines = text.count("\n") + 1
    tail = " It is on your clipboard." if copied else ""
    _log(player, f"[Vision] read {len(text)} chars from screen")
    # Returned text is spoken/acted on by the main model; keep it whole so the
    # user can ask follow-ups ("translate that", "what does line 3 mean").
    return f"On-screen text ({lines} line(s)).{tail}\n\n{text}"


def _explain(player, focus: str) -> str:
    """Explain what's on screen, optionally narrowed by `focus`."""
    frame = _screen_frame()
    extra = f" Focus specifically on: {focus}." if focus else ""
    text = _vision(
        "Look at this screen and explain what the user is looking at: which app "
        "or page it is, what it's showing, and anything notable (charts, diagrams, "
        "dialogs, selections)." + extra + " Be concise and practical — 2 to 4 "
        "short sentences.",
        frame.b64, frame.mime,
        system="You explain on-screen content to a user clearly and briefly.",
    )
    _remember_look(frame.b64, frame.mime, text)
    return text or "Sir, I couldn't make sense of the screen."


def _error(player) -> str:
    """Find an on-screen error / exception and propose a fix."""
    frame = _screen_frame()
    text = _vision(
        "There may be an error, exception, stack trace, or failure message on "
        "this screen. If you find one: (1) state the error in one line, (2) give "
        "the most likely cause, (3) give a concrete fix or next step. If there is "
        "no error visible, say so plainly. Keep it tight.",
        frame.b64, frame.mime,
        system="You are a senior engineer diagnosing an on-screen error.",
        max_tokens=600,
    )
    _remember_look(frame.b64, frame.mime, "error diagnosis")
    return text or "Sir, I don't see an error on the screen right now."


def _find(player, target: str) -> str:
    """Locate a UI element and describe where it is, with coordinates."""
    if not target:
        return "Sir, tell me what to look for on the screen."
    frame = _screen_frame()
    try:
        import pyautogui
        w, h = pyautogui.size()
    except Exception:
        w, h = 1920, 1080
    text = _vision(
        f"This is a downscaled screenshot of a {w}x{h} pixel screen. Find the UI "
        f"element best matching: '{target}'. If present, reply as: "
        f"FOUND | <region in words, e.g. top-right> | x,y (center in full-screen "
        f"pixels, 0,0 top-left) | <short note>. If absent reply exactly NOT_FOUND.",
        frame.b64, frame.mime,
        system="You locate UI elements precisely and answer in the given format.",
        max_tokens=200,
    )
    _remember_look(frame.b64, frame.mime, f"find {target}")
    if "NOT_FOUND" in text.upper() or not text:
        return f"Sir, I couldn't find '{target}' on the screen."
    m = re.search(r"(\d{1,5})\s*,\s*(\d{1,5})", text)
    coords = f" (around {m.group(1)},{m.group(2)})" if m else ""
    region = text.split("|")[1].strip() if "|" in text else text
    return f"Found '{target}': {region}{coords}."


def _diff(player) -> str:
    """Describe what changed since the last vision look."""
    with _look_lock:
        prev = dict(_last_look) if _last_look else None
    frame = _screen_frame()
    if not prev:
        _remember_look(frame.b64, frame.mime, "baseline")
        return ("Sir, I have nothing to compare against yet — I've taken a "
                "baseline of the screen, so ask me again after it changes.")
    text = _vision(
        "Two screenshots of the same screen are described to you. The PREVIOUS "
        "state was: \"" + prev["summary"][:600] + "\". The image attached is the "
        "CURRENT state. In 1-3 short sentences, describe what has CHANGED "
        "(new windows, updated content, progress, anything that appeared or "
        "vanished). If nothing meaningful changed, say so.",
        frame.b64, frame.mime,
        system="You compare screen states and report only what changed.",
        max_tokens=400,
    )
    _remember_look(frame.b64, frame.mime, text)
    return text or "Sir, the screen looks unchanged."


def _watch(player, speak, interval: float) -> str:
    """Start a background watcher that announces when the screen changes."""
    global _watch_thread, _watch_stop
    if _watch_thread and _watch_thread.is_alive():
        return "Sir, I'm already watching the screen. Say 'stop watching' to end it."

    interval = max(1.0, min(float(interval or 4.0), 60.0))
    _watch_stop = threading.Event()
    stop = _watch_stop
    engine = get_engine()
    engine.screen_changed()  # prime the baseline so we don't fire immediately

    def loop():
        idle = 0
        while not stop.is_set():
            if stop.wait(interval):
                break
            try:
                if engine.screen_changed():
                    frame = engine.capture_screen(force=True)
                    note = _vision(
                        "The screen just changed. In ONE short sentence say what is "
                        "now on screen or what changed. No preamble.",
                        frame.b64, frame.mime,
                        system="You give a one-line spoken update about a screen change.",
                        max_tokens=120,
                    )
                    if note:
                        _log(player, f"[Vision] watch: {note}")
                        if speak:
                            speak(f"Screen update: {note}")
                    idle = 0
                else:
                    idle += 1
            except Exception as e:
                print(f"[VisionAssist] watch error: {e}")
        _log(player, "[Vision] watch stopped")

    _watch_thread = threading.Thread(target=loop, daemon=True, name="VisionWatch")
    _watch_thread.start()
    _log(player, f"[Vision] watching screen every {interval:.0f}s")
    return (f"Watching your screen now, sir — I'll speak up when it changes "
            f"(checking every {interval:.0f} seconds). Say 'stop watching' to end.")


def _stop_watch(player) -> str:
    global _watch_stop
    if _watch_stop and _watch_thread and _watch_thread.is_alive():
        _watch_stop.set()
        return "Stopped watching the screen, sir."
    return "Sir, I wasn't watching the screen."


# ── dispatch ─────────────────────────────────────────────────────────────────
def vision_assist(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Entry point used by the tool dispatcher.

    parameters:
      action : read | explain | error | find | diff | watch | stop_watch
      target : element/text to find (find), or focus hint (explain)
      interval : seconds between checks (watch; default 4)
    """
    params = parameters or {}
    action = str(params.get("action", "explain")).lower().strip()
    target = (params.get("target") or params.get("text") or "").strip()
    print(f"[VisionAssist] ▶ {action}  target={target!r}")
    _log(player, f"[Vision] {action}")

    try:
        if action in ("read", "ocr", "read_screen", "copy_text"):
            return _read(player)
        if action in ("explain", "describe", "look", "what"):
            return _explain(player, target)
        if action in ("error", "diagnose", "fix", "debug"):
            return _error(player)
        if action in ("find", "locate", "where"):
            return _find(player, target)
        if action in ("diff", "changes", "what_changed", "compare"):
            return _diff(player)
        if action in ("watch", "monitor", "watch_screen"):
            return _watch(player, speak, params.get("interval", 4.0))
        if action in ("stop_watch", "stop_watching", "unwatch"):
            return _stop_watch(player)
        return (f"Unknown vision action '{action}'. Use read, explain, error, "
                f"find, diff, watch, or stop_watch.")
    except Exception as e:
        import traceback; traceback.print_exc()
        return f"Sir, the vision assist failed: {e}"


if __name__ == "__main__":
    import sys
    act = sys.argv[1] if len(sys.argv) > 1 else "explain"
    tgt = sys.argv[2] if len(sys.argv) > 2 else ""
    print(vision_assist({"action": act, "target": tgt}))
