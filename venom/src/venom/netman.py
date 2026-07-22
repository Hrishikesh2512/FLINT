"""Wi-Fi network manager — add / edit / remove / prioritise the networks the
Pi will join, on top of NetworkManager.

Why this is safe by construction ("nothing breaks"): every network we create is
`autoconnect yes` with an `autoconnect-priority`, and NetworkManager always
brings up the highest-priority known network that's in range, falling back to
the next when one drops. So the phone hotspot is simply the highest priority —
whenever it's on, the Pi is on it; when it's off, the Pi roams to the next known
network on its own. We never delete the connection the Pi is currently using, so
an edit can't strand it, and a wrong password just falls back to the base.

All NetworkManager access goes through an injected `run(argv) -> (rc, output)`
callable (web.py runs it as root via the shell daemon). That keeps this module
pure and unit-testable with a fake nmcli, and free of any privilege concerns.
"""

from __future__ import annotations

import re
from collections.abc import Callable

Runner = Callable[[list[str]], "tuple[int, str]"]

WIFI_TYPE = "802-11-wireless"
# The priority we stamp on "the base" (phone hotspot) — comfortably above any
# hand-set value so it always wins when it's in range. Ordinary networks default
# to 0, so the base at ≥ this is preferred whenever it's available.
BASE_PRIORITY = 100


def _unescape(field: str) -> str:
    # nmcli -t escapes ':' and '\' in values with a backslash; undo that.
    return field.replace("\\:", ":").replace("\\\\", "\\")


def _split_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped ':' separators."""
    return [_unescape(f) for f in re.split(r"(?<!\\):", line)]


def _valid_name(name: str) -> bool:
    return bool(name) and "\n" not in name and "\r" not in name


# ── read side ────────────────────────────────────────────────────────────────
def _saved(run: Runner) -> tuple[bool, list[dict]]:
    rc, out = run(["nmcli", "-t", "-f",
                   "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,ACTIVE,DEVICE",
                   "connection", "show"])
    if rc != 0:
        return False, []
    nets = []
    for line in out.splitlines():
        if not line.strip():
            continue
        f = _split_terse(line)
        if len(f) < 6 or f[1] != WIFI_TYPE:
            continue
        try:
            prio = int(f[3] or 0)
        except ValueError:
            prio = 0
        nets.append({
            "name": f[0],
            "autoconnect": f[2] == "yes",
            "priority": prio,
            "active": f[4] == "yes",
            "device": f[5],
        })
    nets.sort(key=lambda n: (-n["priority"], n["name"].lower()))
    return True, nets


def _available(run: Runner) -> list[dict]:
    run(["nmcli", "dev", "wifi", "rescan"])  # best-effort; may rate-limit
    rc, out = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                   "dev", "wifi", "list"])
    if rc != 0:
        return []
    seen: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        f = _split_terse(line)
        if len(f) < 4 or not f[1]:  # skip hidden/blank SSIDs
            continue
        try:
            signal = int(f[2] or 0)
        except ValueError:
            signal = 0
        ssid = f[1]
        row = {"ssid": ssid, "signal": signal,
               "security": f[3] or "open", "active": f[0] == "*"}
        # Keep the strongest sighting of each SSID.
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = row
    return sorted(seen.values(), key=lambda r: -r["signal"])


def overview(run: Runner) -> dict:
    """Everything the console panel needs in one shot."""
    ok, saved = _saved(run)
    if not ok:
        return {"nm": False, "current": {}, "saved": [], "available": []}
    available = _available(run)
    known = {n["name"] for n in saved}
    for a in available:
        a["known"] = a["ssid"] in known
    active = next((n for n in saved if n["active"]), None)
    current: dict = {}
    if active:
        current = {"name": active["name"]}
        match = next((a for a in available if a["ssid"] == active["name"]), None)
        if match:
            current["signal"] = match["signal"]
    return {"nm": True, "current": current, "saved": saved, "available": available}


# ── write side ────────────────────────────────────────────────────────────────
def add_or_update(run: Runner, ssid: str, password: str = "",
                  priority: int | None = None) -> str:
    """Create the network if new, else update its password/priority. Never
    forces a switch — NetworkManager brings it up itself if/when it's the best
    available network, so adding a low-priority net can't knock you off."""
    ssid = (ssid or "").strip()
    if not _valid_name(ssid):
        return "Give the network a name (SSID)."
    password = password or ""
    if password and len(password) < 8:
        return "A Wi-Fi password must be at least 8 characters."

    _, saved = _saved(run)
    exists = any(n["name"] == ssid for n in saved)

    if not exists:
        argv = ["nmcli", "connection", "add", "type", "wifi",
                "con-name", ssid, "ssid", ssid,
                "connection.autoconnect", "yes"]
        if priority is not None:
            argv += ["connection.autoconnect-priority", str(int(priority))]
        if password:
            argv += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        rc, out = run(argv)
        if rc != 0:
            return f"Couldn't add {ssid}: {_tail(out)}"
        return f"Added {ssid}. It'll connect automatically when it's the best network in range."

    # Update in place — leave anything not specified untouched.
    argv = ["nmcli", "connection", "modify", ssid,
            "connection.autoconnect", "yes"]
    if priority is not None:
        argv += ["connection.autoconnect-priority", str(int(priority))]
    if password:
        argv += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    rc, out = run(argv)
    if rc != 0:
        return f"Couldn't update {ssid}: {_tail(out)}"
    return f"Updated {ssid}."


def remove(run: Runner, name: str) -> str:
    """Delete a saved network — but never the one currently carrying the Pi,
    so this can't cut its own connection out from under it."""
    name = (name or "").strip()
    if not _valid_name(name):
        return "Which network?"
    _, saved = _saved(run)
    entry = next((n for n in saved if n["name"] == name), None)
    if entry is None:
        return f"No saved network called {name}."
    if entry["active"]:
        return (f"{name} is the network you're connected through right now — "
                "connect to another one first, then remove it. (Kept it to "
                "avoid stranding the Pi.)")
    rc, out = run(["nmcli", "connection", "delete", name])
    if rc != 0:
        return f"Couldn't remove {name}: {_tail(out)}"
    return f"Removed {name}."


def connect(run: Runner, name: str) -> str:
    name = (name or "").strip()
    if not _valid_name(name):
        return "Which network?"
    rc, out = run(["nmcli", "connection", "up", name])
    if rc != 0:
        return f"Couldn't connect to {name}: {_tail(out)}"
    return f"Connected to {name}."


def set_priority(run: Runner, name: str, priority: int) -> str:
    name = (name or "").strip()
    if not _valid_name(name):
        return "Which network?"
    rc, out = run(["nmcli", "connection", "modify", name,
                   "connection.autoconnect", "yes",
                   "connection.autoconnect-priority", str(int(priority))])
    if rc != 0:
        return f"Couldn't set priority on {name}: {_tail(out)}"
    return f"Set {name} priority to {int(priority)}."


def set_base(run: Runner, name: str) -> str:
    """Make `name` the base network — the one the Pi prefers above all others
    whenever it's in range (your phone hotspot). Just the top priority."""
    name = (name or "").strip()
    if not _valid_name(name):
        return "Which network?"
    _, saved = _saved(run)
    if not any(n["name"] == name for n in saved):
        return f"No saved network called {name}. Add it first."
    others = [n["priority"] for n in saved if n["name"] != name]
    target = max([BASE_PRIORITY, *[p + 10 for p in others]])
    rc, out = run(["nmcli", "connection", "modify", name,
                   "connection.autoconnect", "yes",
                   "connection.autoconnect-priority", str(target)])
    if rc != 0:
        return f"Couldn't set {name} as base: {_tail(out)}"
    return (f"{name} is now your base network — the Pi will jump to it whenever "
            "it's on, and fall back to the others when it's off.")


def _tail(out: str) -> str:
    out = (out or "").strip().replace("\n", " ")
    return out[-160:] if out else "unknown error"
