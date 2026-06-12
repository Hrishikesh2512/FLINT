# actions/open_app.py
# MARK XXVI — Cross-Platform App Launcher

import time
import subprocess
import platform
import shutil

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False


def _say(msg: str) -> None:
    """print() that survives cp1252 consoles choking on emoji.

    A failed status print must never turn a successful action into a
    reported failure.
    """
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))

_APP_ALIASES = {
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "instagram"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "tiktok"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "terminal":           {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "gnome-terminal"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
}


def _normalize(raw: str) -> str:
    system = platform.system()
    key    = raw.lower().strip()
    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(system, raw)
    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(system, raw)
    return raw


def _is_running(app_name: str) -> bool:
    """Returns True if a process matching app_name is currently running."""
    if not _PSUTIL:
        return False          # can't check → assume not running, allow launch
    app_lower = app_name.lower().replace(" ", "").replace(".exe", "")
    if len(app_lower) < 3:
        return False          # too short to match reliably
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                proc_name = proc.info["name"].lower().replace(" ", "").replace(".exe", "")
                # substring match both ways, but never against a degenerate
                # short process name ("py", "sh", …) — those false-positive
                # against half the apps that could be requested
                if app_lower in proc_name or (len(proc_name) >= 4
                                              and proc_name in app_lower):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


_DWMWA_CLOAKED = 14


def _is_cloaked(hwnd) -> bool:
    """True if the window is DWM-cloaked (Windows 10/11).

    Suspended UWP/store apps (WhatsApp, Settings, …) keep an invisible
    "ghost" frame that still passes IsWindowVisible.  DWMWA_CLOAKED != 0
    marks those frames — they cannot be focused or receive input, so any
    match against one must be rejected.
    """
    try:
        import ctypes
        cloaked = ctypes.c_uint(0)
        r = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            int(hwnd), _DWMWA_CLOAKED,
            ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return r == 0 and cloaked.value != 0
    except Exception:
        return False               # pre-DWM / non-Windows: nothing is cloaked


def _hwnd_visible(hwnd) -> bool:
    """IsWindowVisible via ctypes — works even without pywin32."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindowVisible(int(hwnd)))
    except Exception:
        return True


def _is_real_window(hwnd, win32gui) -> bool:
    """A window FLINT may focus: visible, not cloaked, not a tray ghost.

    Plainly minimized (iconic) windows are acceptable — SW_RESTORE brings
    them back — but only when they are not cloaked; an iconic + cloaked
    frame is a store app minimized to the hidden tray.
    """
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if _is_cloaked(hwnd):
        return False
    return True


# Browsers (and the WebView host) render arbitrary page titles — a Chrome tab
# titled "WhatsApp - Google Chrome" must never be mistaken for the WhatsApp
# app.  Title-based matching is disabled for windows owned by these processes;
# they can still be matched by process name (e.g. when opening "chrome").
_BROWSER_PROCS = {"chrome", "msedge", "firefox", "brave", "opera",
                  "opera_gx", "vivaldi", "msedgewebview2"}


def _find_hwnd_windows(app_name: str):
    """Locate a focusable top-level window for app_name.  Returns hwnd or None."""
    app_lower = app_name.lower().replace(".exe", "").strip()
    try:
        import win32gui
        import win32process
        import psutil as _ps

        # process-name matches are authoritative; title matches are a
        # fallback (UWP apps hosted by ApplicationFrameHost only expose
        # the app name in the title)
        proc_hwnd  = None
        title_hwnd = None

        def _cb(hwnd, _):
            nonlocal proc_hwnd, title_hwnd
            if proc_hwnd:
                return
            if not _is_real_window(hwnd, win32gui):
                return
            title = win32gui.GetWindowText(hwnd).strip().lower()
            if not title:
                return
            try:
                _, pid   = win32process.GetWindowThreadProcessId(hwnd)
                pname    = _ps.Process(pid).name().lower().replace(".exe", "")
                if app_lower in pname or (len(pname) >= 4
                                          and pname in app_lower):
                    proc_hwnd = hwnd
                elif (app_lower in title and title_hwnd is None
                      and pname not in _BROWSER_PROCS):
                    title_hwnd = hwnd
            except Exception:
                pass

        win32gui.EnumWindows(_cb, None)
        return proc_hwnd or title_hwnd
    except ImportError:
        return None


def wait_for_window_windows(app_name: str, timeout: float = 2.0,
                            poll: float = 0.25):
    """Poll until a valid window handle for app_name appears, or timeout.

    Freshly launched apps need time to draw their first window — sending
    keystrokes before then sprays input into whatever currently has focus.
    """
    deadline = time.time() + timeout
    while True:
        hwnd = _find_hwnd_windows(app_name)
        if hwnd:
            return hwnd
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def _force_foreground(hwnd) -> bool:
    """Restore + SetForegroundWindow, defeating Windows' foreground lock.

    SetForegroundWindow is denied when the caller isn't the foreground
    process (typical right after launching an app); pressing a transient
    ALT releases the lock.  Returns True only when the handle is still
    valid and actually holds the foreground.
    """
    import win32gui
    import win32con

    for attempt in range(3):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            if attempt > 0:
                try:
                    import win32api
                    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                    win32api.keybd_event(win32con.VK_MENU, 0,
                                         win32con.KEYEVENTF_KEYUP, 0)
                except Exception:
                    pass
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.5)
        if not win32gui.IsWindow(hwnd):
            return False                     # window died mid-focus
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.IsWindow(hwnd) and win32gui.GetForegroundWindow() == hwnd


def _focus_window_windows(app_name: str, timeout: float = 2.0) -> bool:
    """
    Brings an already-running Windows app to the foreground.

    Retries until the window handle appears (up to `timeout`), then forces
    focus and validates the handle actually holds the foreground before
    reporting success.  Falls back to pygetwindow if pywin32 is unavailable.
    """
    # ── Strategy 1: win32 API (most reliable) ──
    try:
        import win32gui  # noqa: F401 — probe availability

        hwnd = wait_for_window_windows(app_name, timeout)
        if hwnd and _force_foreground(hwnd):
            _say(f"[open_app] 🎯 Focused via win32: HWND={hwnd}")
            return True
        if hwnd:
            _say(f"[open_app] ⚠️ Window found but foreground unconfirmed: "
                 f"HWND={hwnd}")
    except ImportError:
        pass  # pywin32 not installed, try next strategy
    except Exception as e:
        _say(f"[open_app] win32 focus failed: {e}")

    # ── Strategy 2: pygetwindow fallback (with retry) ──
    app_lower = app_name.lower().replace(".exe", "").strip()
    # don't let a browser tab titled "<app> - Google Chrome" pass for the app
    _browser_tab_marks = ("google chrome", "microsoft edge", "mozilla firefox",
                          "- brave", "- opera", "- vivaldi")
    deadline = time.time() + timeout
    while True:
        try:
            import pygetwindow as gw
            wins = [
                w for w in gw.getAllWindows()
                if app_lower in w.title.lower() and w.title.strip()
                and _hwnd_visible(w._hWnd) and not _is_cloaked(w._hWnd)
                and not any(m in w.title.lower() and app_lower not in m
                            for m in _browser_tab_marks)
            ]
            if wins:
                w = wins[0]
                if w.isMinimized:
                    w.restore()
                try:
                    w.activate()
                except Exception:
                    # pygetwindow raises "Error code from Windows: 0 -
                    # The operation completed successfully" even when
                    # activation worked — verify via isActive instead
                    pass
                time.sleep(0.5)
                if w.isActive:
                    _say(f"[open_app] 🎯 Focused via pygetwindow: '{w.title}'")
                    return True
        except Exception as e:
            _say(f"[open_app] pygetwindow focus failed: {e}")
            break
        if time.time() >= deadline:
            break
        time.sleep(0.25)

    return False


# ---------------------------------------------------------------------------
# Platform launchers
# ---------------------------------------------------------------------------

def _launch_windows(app_name: str) -> bool:
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(3.0)
        return True
    except Exception as e:
        _say(f"[open_app] ⚠️ Windows launch failed: {e}")
        return False


def _launch_macos(app_name: str) -> bool:
    try:
        result = subprocess.run(["open", "-a", app_name], capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass
    try:
        result = subprocess.run(["open", "-a", f"{app_name}.app"], capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass
    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        _say(f"[open_app] ⚠️ macOS Spotlight failed: {e}")
        return False


def _launch_linux(app_name: str) -> bool:
    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-"))
    )
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            return True
        except Exception:
            pass
    try:
        subprocess.run(["xdg-open", app_name], capture_output=True, timeout=5)
        return True
    except Exception:
        pass
    try:
        desktop_name = app_name.lower().replace(" ", "-")
        subprocess.run(["gtk-launch", desktop_name], capture_output=True, timeout=5)
        return True
    except Exception:
        pass
    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = (parameters or {}).get("app_name", "").strip()

    if not app_name:
        return "Please specify which application to open, sir."

    system   = platform.system()
    launcher = _OS_LAUNCHERS.get(system)

    if launcher is None:
        return f"Unsupported OS: {system}"

    normalized = _normalize(app_name)

    # ── Guard: already running → focus only, never spawn a new instance ──
    if _is_running(normalized) or _is_running(app_name):
        _say(f"[open_app] ✅ Process for {app_name} alive — trying to focus")

        focused = False
        if system == "Windows":
            focused = _focus_window_windows(normalized)
            if not focused:
                focused = _focus_window_windows(app_name)
        elif system == "Darwin":
            try:
                subprocess.run(["open", "-a", normalized], capture_output=True, timeout=5)
                focused = True
            except Exception:
                pass

        if focused:
            if player:
                player.write_log(f"[open_app] focused {app_name}")
            return f"{app_name} is already open, sir."

        # Process exists but no focusable window: a suspended UWP/store app
        # keeps a background process + cloaked ghost frame after its window
        # closes.  Treat it as NOT running and launch fresh.
        _say(f"[open_app] ⚠️ {app_name} process alive but no visible window "
             f"(suspended/tray ghost) — launching fresh")

    # ── Not running: launch fresh ──
    _say(f"[open_app] 🚀 Launching: {app_name} → {normalized} ({system})")
    if player:
        player.write_log(f"[open_app] launching {app_name}")

    try:
        success = launcher(normalized)

        if not success and normalized != app_name:
            success = launcher(app_name)

        if success:
            # Validate the launch: wait for an actual window before reporting
            # success, so chained actions (focus, keystrokes) land safely.
            if system == "Windows":
                # Store/UWP apps (WhatsApp, …) can take 5s+ to draw their
                # first window — a short wait here produced false "couldn't
                # confirm it launched" reports for apps that opened fine.
                hwnd = (wait_for_window_windows(normalized, timeout=6.0)
                        or wait_for_window_windows(app_name, timeout=3.0))
                if hwnd:
                    _say(f"[open_app] ✅ Window confirmed: HWND={hwnd}")
                    return f"Opened {app_name} successfully, sir."
                _say(f"[open_app] ⚠️ Launched but no window after wait")
                return (
                    f"Opened {app_name}, sir — it's still drawing its window, "
                    f"give it a moment before the next step."
                )
            return f"Opened {app_name} successfully, sir."

        return (
            f"I tried to open {app_name}, sir, but couldn't confirm it launched. "
            f"It may still be loading or might not be installed."
        )

    except Exception as e:
        _say(f"[open_app] ❌ {e}")
        return f"Failed to open {app_name}, sir: {e}"