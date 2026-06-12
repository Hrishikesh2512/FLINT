import time
import pyautogui

from actions.open_app import _say, _is_cloaked
from pathlib import Path

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.08

try:
    from actions.open_app import open_app as _open_app_module
    _HAS_OPEN_APP = True
except ImportError:
    _HAS_OPEN_APP = False


# ---------------------------------------------------------------------------
# Unicode-safe typing
# ---------------------------------------------------------------------------

def _type_text(text: str) -> None:
    """Type text into the focused control, unicode-safe.

    pyautogui.write silently drops emoji / Hindi / any non-ASCII characters,
    which mangles messages and contact names. Paste via the clipboard
    instead; fall back to write only if the clipboard is unavailable.
    """
    try:
        import pyperclip
        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        # let the target consume the paste before restoring the clipboard
        time.sleep(0.3)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
        return
    except Exception:
        pass
    pyautogui.write(text, interval=0.03)


# ---------------------------------------------------------------------------
# WhatsApp — keyed exactly on what the debug showed
# ---------------------------------------------------------------------------

# WhatsApp Desktop's process name differs by version/install channel:
# "WhatsApp.Root.exe" (current store build), "WhatsApp.exe" (older builds).
# Matching on the exact old name made FLINT think WhatsApp wasn't running
# when it was (and vice versa after an update), so match any process whose
# name contains "whatsapp" instead.
def _is_whatsapp_proc(name: str) -> bool:
    return "whatsapp" in (name or "").lower()


def _is_whatsapp_running() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                if _is_whatsapp_proc(p.info["name"]):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _get_whatsapp_hwnd():
    """
    Returns (hwnd, title) for the real WhatsApp Desktop window.
    Matches by:
      - process name contains "whatsapp" (WhatsApp.Root.exe / WhatsApp.exe)
      - window is visible, not cloaked, and has a non-empty title
    Excludes msedgewebview2 / chrome / browser tabs (process match only —
    a browser tab titled "WhatsApp" never qualifies).
    """
    try:
        import win32gui
        import win32process
        import psutil

        result = [None, ""]

        def _cb(hwnd, _):
            if result[0]:           # already found
                return
            if not win32gui.IsWindowVisible(hwnd):
                return
            if _is_cloaked(hwnd):   # suspended/tray ghost frame — not focusable
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return
            try:
                _, pid  = win32process.GetWindowThreadProcessId(hwnd)
                pname   = psutil.Process(pid).name().lower()
                if _is_whatsapp_proc(pname):
                    result[0] = hwnd
                    result[1] = title
            except Exception:
                pass

        win32gui.EnumWindows(_cb, None)
        return result[0], result[1]

    except ImportError:
        return None, ""


def _wait_for_whatsapp_hwnd(timeout: float = 2.0, poll: float = 0.3) -> tuple:
    """Poll until WhatsApp's window handle exists, or timeout. (hwnd, title)."""
    deadline = time.time() + timeout
    while True:
        hwnd, title = _get_whatsapp_hwnd()
        if hwnd:
            return hwnd, title
        if time.time() >= deadline:
            return None, ""
        time.sleep(poll)


def _focus_whatsapp(timeout: float = 2.0) -> tuple:
    """Force-focus the WhatsApp window with retry + validation.

    Waits up to `timeout` for the window to exist (fresh launches draw the
    window late), then forces foreground and confirms the handle is still
    valid AND actually frontmost before returning it.  Returns (hwnd, title);
    (None, "") only after the full timeout elapses.
    """
    try:
        import win32gui
        import win32con

        hwnd, title = _wait_for_whatsapp_hwnd(timeout)
        if not hwnd:
            return None, ""

        for attempt in range(4):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                if attempt > 0:
                    # transient ALT press releases Windows' foreground lock,
                    # which blocks SetForegroundWindow right after a launch
                    try:
                        import win32api
                        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                        win32api.keybd_event(win32con.VK_MENU, 0,
                                             win32con.KEYEVENTF_KEYUP, 0)
                    except Exception:
                        pass
                win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                _say(f"[SendMessage] focus attempt {attempt + 1} failed: {e}")
            time.sleep(0.5)

            if not win32gui.IsWindow(hwnd):
                # window died (e.g. splash screen closed) — re-acquire it
                hwnd, title = _wait_for_whatsapp_hwnd(timeout=1.0)
                if not hwnd:
                    return None, ""
                continue
            if win32gui.GetForegroundWindow() == hwnd:
                # re-read title after focus (chat title may now be visible)
                return hwnd, win32gui.GetWindowText(hwnd).strip()

        _say("[SendMessage] ⚠️ Window valid but foreground unconfirmed")
        return None, ""

    except Exception as e:
        _say(f"[SendMessage] focus error: {e}")
        return None, ""


def _whatsapp_still_focused(hwnd) -> bool:
    """True if hwnd is still a live window holding the foreground."""
    try:
        import win32gui
        return (bool(hwnd) and win32gui.IsWindow(hwnd)
                and win32gui.GetForegroundWindow() == hwnd)
    except Exception:
        return True            # can't verify — don't block the send


def _send_whatsapp(receiver: str, message: str) -> str:
    try:
        fresh_launch = not _is_whatsapp_running()
        if fresh_launch:
            _say("[SendMessage] WhatsApp not running → launching")
            if _HAS_OPEN_APP:
                _open_app_module({"app_name": "whatsapp"})
            else:
                pyautogui.press("win")
                time.sleep(0.4)
                pyautogui.write("WhatsApp", interval=0.04)
                time.sleep(0.5)
                pyautogui.press("enter")

        # Fresh UWP launches draw the window late — give them a longer
        # validation window before declaring failure.
        hwnd, title = _focus_whatsapp(timeout=8.0 if fresh_launch else 3.0)

        if not hwnd and not fresh_launch:
            # Process alive but no focusable window: WhatsApp is minimized
            # to tray / suspended with only a cloaked ghost frame. It IS
            # "running" to psutil but unusable — relaunch to surface a
            # real window instead of giving up.
            _say("[SendMessage] WhatsApp running but no usable window — relaunching")
            if _HAS_OPEN_APP:
                _open_app_module({"app_name": "whatsapp"})
            hwnd, title = _focus_whatsapp(timeout=8.0)

        _say(f"[SendMessage] HWND={hwnd} title='{title}'")

        if not hwnd:
            return ("Could not bring up the WhatsApp window — "
                    "the message was NOT sent.")

        # Determine if already in the right chat
        # Title is either "WhatsApp" (home) or "<Name> - WhatsApp" (in chat)
        if " - WhatsApp" in title:
            open_contact = title.replace(" - WhatsApp", "").strip()
        else:
            open_contact = ""

        # exact match only here — lenient matching could skip the search and
        # message a similarly-named contact whose chat happens to be open
        already_in_chat = open_contact.lower() == receiver.strip().lower()

        if already_in_chat:
            _say(f"[SendMessage] Already in {receiver}'s chat — skipping search")
        else:
            _say(f"[SendMessage] Searching for: {receiver}")
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.5)
            pyautogui.hotkey("ctrl", "a")
            _type_text(receiver)
            time.sleep(1.2)            # let search results populate
            pyautogui.press("enter")   # open the top result
            time.sleep(1.0)            # let the chat load before typing

        # final guard: never type the message into the wrong window
        if not _whatsapp_still_focused(hwnd):
            hwnd, _ = _focus_whatsapp(timeout=2.0)
            if not hwnd:
                return ("Lost focus on the WhatsApp window before typing — "
                        "the message was NOT sent.")

        _type_text(message)
        time.sleep(0.2)
        if not _whatsapp_still_focused(hwnd):
            return ("WhatsApp lost focus while the message was being typed — "
                    "it was NOT sent. Please check the chat box.")
        pyautogui.press("enter")
        time.sleep(0.3)

        return f"Message sent to {receiver} via WhatsApp."

    except Exception as e:
        return f"WhatsApp error: {e} — the message may NOT have been sent."


# ---------------------------------------------------------------------------
# Other platforms
# ---------------------------------------------------------------------------

def _open_app(app_name: str):
    if _HAS_OPEN_APP:
        _open_app_module({"app_name": app_name})
    else:
        pyautogui.press("win")
        time.sleep(0.4)
        pyautogui.write(app_name, interval=0.04)
        time.sleep(0.5)
        pyautogui.press("enter")
        time.sleep(2.5)


def _open_and_focus(app_name: str, timeout: float = 2.0) -> bool:
    """Open (or surface) an app and confirm its window holds focus.

    Returns False only after the window failed to appear/focus within
    `timeout` — callers must NOT send keystrokes in that case.
    """
    _open_app(app_name)
    try:
        from actions.open_app import _focus_window_windows
        if _focus_window_windows(app_name, timeout=timeout):
            time.sleep(0.5)            # let the app finish drawing its UI
            return True
        return False
    except Exception:
        # non-Windows or helper unavailable — fall back to a settle delay
        time.sleep(1.5)
        return True


def _foreground_is(app_name: str) -> bool:
    """True if the current foreground window belongs to app_name (by process
    name or title). Used as a guard so keystrokes never spray into whatever
    stole focus mid-send."""
    try:
        import win32gui
        import win32process
        import psutil

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False
        app = app_name.lower().replace(".exe", "").strip()
        title = win32gui.GetWindowText(hwnd).strip().lower()
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pname  = psutil.Process(pid).name().lower().replace(".exe", "")
        except Exception:
            pname = ""
        return app in pname or pname in app or app in title
    except ImportError:
        return True            # can't verify — don't block the send
    except Exception:
        return True


def _send_instagram(receiver: str, message: str) -> str:
    try:
        import webbrowser
        webbrowser.open("https://www.instagram.com/direct/new/")
        time.sleep(3.5)
        _type_text(receiver)
        time.sleep(1.5)
        pyautogui.press("down")
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(0.5)
        for _ in range(3):
            pyautogui.press("tab")
            time.sleep(0.1)
        pyautogui.press("enter")
        time.sleep(1.5)
        _type_text(message)
        time.sleep(0.2)
        pyautogui.press("enter")
        return (f"Message to {receiver} sent via Instagram in the browser — "
                f"worth a quick glance to confirm it went through.")
    except Exception as e:
        return f"Instagram error: {e} — the message may NOT have been sent."


def _send_telegram(receiver: str, message: str) -> str:
    return _send_generic("telegram", receiver, message)


def _send_generic(platform: str, receiver: str, message: str) -> str:
    try:
        if not _open_and_focus(platform, timeout=4.0):
            return (f"Could not find or focus the {platform} window — "
                    f"the message was NOT sent.")
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.4)
        _type_text(receiver)
        time.sleep(1.0)
        pyautogui.press("enter")
        time.sleep(0.8)
        if not _foreground_is(platform):
            return (f"{platform} lost focus before the message could be "
                    f"typed — it was NOT sent.")
        _type_text(message)
        time.sleep(0.2)
        if not _foreground_is(platform):
            return (f"{platform} lost focus while typing — the message was "
                    f"NOT sent. Please check the chat box.")
        pyautogui.press("enter")
        return f"Message sent to {receiver} via {platform}."
    except Exception as e:
        return f"{platform} error: {e} — the message may NOT have been sent."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None
) -> str:
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip().lower()

    if not receiver:
        return "Please specify who to send the message to, sir."
    if not message_text:
        return "Please specify what message to send, sir."

    _say(f"[SendMessage] 📨 {platform} → {receiver}: {message_text[:40]}")
    if player:
        player.write_log(f"[msg] Sending to {receiver} via {platform}...")

    if "whatsapp" in platform or "wp" in platform or "wapp" in platform:
        result = _send_whatsapp(receiver, message_text)
    elif "instagram" in platform or "ig" in platform or "insta" in platform:
        result = _send_instagram(receiver, message_text)
    elif "telegram" in platform or "tg" in platform:
        result = _send_telegram(receiver, message_text)
    else:
        result = _send_generic(platform, receiver, message_text)

    _say(f"[SendMessage] ✅ {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result