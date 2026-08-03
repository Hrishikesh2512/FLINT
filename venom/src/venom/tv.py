"""Voice control for a Samsung (Tizen) smart TV, over the LAN.

Samsung TVs from 2016 on expose a WebSocket remote on port 8002: you send the
same key codes the physical remote sends, list and launch apps, and read basic
device state. The Pi is already on the home Wi-Fi, so 'TV band kar do' is one
packet away and keeps working when the internet is down.

Two quirks shape this module, and both are worth knowing before reading on:

  * **Power ON cannot go over WebSocket.** When the TV is off its WebSocket
    server is off too, so there is nothing to talk to. The only way back in is
    a Wake-on-LAN magic packet to the TV's MAC — hence `mac` in the config, and
    hence power-on being the one operation that silently does nothing if the
    MAC is missing or the TV has "Network Standby" disabled.

  * **Volume keys are relative.** The remote protocol only has up/down/mute,
    with no notion of "set it to 20". Absolute volume comes from a completely
    separate UPnP/DLNA endpoint on port 9197, which most (not all) models also
    serve. So `set_volume` speaks UPnP and degrades to a plain sentence where
    that endpoint is missing, while `nudge_volume` uses keys and always works.

The first connection makes the TV show an "Allow this device?" prompt; accept
it once and the returned token is written to `token_path`, so it never asks
again. Set the TV's prompt policy to "First Time Only" if it keeps asking.

Design mirrors the other tool clients: every call is short-lived and best
effort, failures degrade into a plain spoken sentence, and nothing here can
crash the voice loop. The client is injected (default: samsungtvws) so this is
testable without hardware, and samsungtvws is imported lazily so the dependency
is only needed once a TV is actually configured.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

log = logging.getLogger("venom.tv")

# Friendly words → Samsung remote key codes. The model maps whatever the user
# said onto one of these names; Hinglish spellings sit alongside the English so
# 'aage badhao' and 'fast forward' both land on KEY_FF.
_KEYS = {
    # navigation
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "ok": "KEY_ENTER", "select": "KEY_ENTER", "enter": "KEY_ENTER",
    "back": "KEY_RETURN", "return": "KEY_RETURN", "peeche": "KEY_RETURN",
    "home": "KEY_HOME", "menu": "KEY_MENU", "exit": "KEY_EXIT",
    "source": "KEY_SOURCE", "input": "KEY_SOURCE",
    "guide": "KEY_GUIDE", "info": "KEY_INFO", "tools": "KEY_TOOLS",
    # playback
    "play": "KEY_PLAY", "pause": "KEY_PAUSE", "stop": "KEY_STOP",
    "forward": "KEY_FF", "fast forward": "KEY_FF", "aage": "KEY_FF",
    "rewind": "KEY_REWIND", "back10": "KEY_REWIND",
    "next": "KEY_FF", "previous": "KEY_REWIND",
    # channels
    "channel up": "KEY_CHUP", "channel down": "KEY_CHDOWN",
    "channel list": "KEY_CH_LIST",
}

# Spoken app names → the substring to look for in the TV's own app list, plus
# fallback IDs to launch blind when that list is unavailable. Samsung changes
# these IDs between TV years, so the list is always tried first and the IDs are
# a last resort, newest-first (2020+ sets use the longer numbers).
#
# Only IDs from Samsung's published table are listed. The Indian services have
# no stable public ID, so they resolve by name off the TV's own app list — if
# that read fails they report "couldn't find it" rather than firing a guessed
# ID and opening something random.
_APPS = {
    "netflix":     ("netflix",  ["3201907018807", "11101200001"]),
    "youtube":     ("youtube",  ["111299001912"]),
    "prime video": ("prime",    ["3201910019365", "3201512006785"]),
    "disney+":     ("disney",   ["3202204027038", "3201901017640"]),
    "spotify":     ("spotify",  ["3201606009684"]),
    "hotstar":     ("hotstar",  []),
    "apple tv":    ("apple tv", []),
    "jiocinema":   ("jiocinema", []),
    "sonyliv":     ("sonyliv",  []),
    "zee5":        ("zee5",     []),
    "browser":     ("internet", ["org.tizen.browser"]),
}

# Spoken aliases that mean one of the canonical app names above.
_APP_ALIASES = {
    "prime": "prime video", "amazon": "prime video", "amazon prime": "prime video",
    "disney": "disney+", "disney plus": "disney+", "jio hotstar": "hotstar",
    "jiohotstar": "hotstar", "star": "hotstar", "yt": "youtube",
    "apple": "apple tv", "jio": "jiocinema", "jio cinema": "jiocinema",
    "sony liv": "sonyliv", "zee": "zee5", "internet": "browser", "web": "browser",
}


class TVController:
    """Drives one Samsung Tizen TV on the LAN.

    Every public method returns a sentence ready to be spoken. The TV is only
    contacted when a method is called — nothing is held open between commands,
    because a TV that goes to standby would drop the socket anyway.
    """

    def __init__(self, host: str, mac: str = "", name: str = "Venom",
                 token_path: str | Path = "/var/lib/venom/tv-token.txt",
                 port: int = 8002, timeout: float = 5.0,
                 client_factory: Callable[..., Any] | None = None,
                 wol_sender: Callable[[str], None] | None = None,
                 upnp_call: Callable[[str, str, dict], str | None] | None = None):
        self._host = (host or "").strip()
        self._mac = (mac or "").strip()
        self._name = name or "Venom"
        self._token_path = Path(token_path)
        self._port = int(port)
        self._timeout = float(timeout)
        self._factory = client_factory  # None → lazy-import samsungtvws
        self._wol = wol_sender or _send_magic_packet
        self._upnp = upnp_call or self._soap
        self._apps: list[dict] | None = None  # cached app list (per process)

    # ── client ──────────────────────────────────────────────────────────────
    def _client(self):
        factory = self._factory
        if factory is None:
            from samsungtvws import SamsungTVWS  # lazy: only once a TV exists

            factory = SamsungTVWS
        try:  # the token file lets the TV remember us, so it prompts only once
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return factory(host=self._host, port=self._port, name=self._name,
                       token_file=str(self._token_path), timeout=self._timeout)

    def _send(self, key: str) -> bool:
        """Press one remote key. False if the TV didn't take it."""
        try:
            self._client().send_key(key)
            return True
        except Exception as exc:  # noqa: BLE001 — a dead TV is not a crash
            log.warning("TV key %s failed: %s", key, exc)
            return False

    @staticmethod
    def _unreachable() -> str:
        return ("I couldn't reach the TV. It may be fully powered off at the "
                "wall, or off the Wi-Fi.")

    # ── power ───────────────────────────────────────────────────────────────
    def power(self, on: bool) -> str:
        return self._power_on() if on else self._power_off()

    def _power_on(self) -> str:
        # Wake-on-LAN is the only route in: with the panel off, so is the TV's
        # WebSocket server, so there is nothing to send a key to.
        if not self._mac:
            return ("I can't switch the TV on — that needs its MAC address for "
                    "Wake-on-LAN. Add `mac` under [tv] in Venom's config.")
        try:
            self._wol(self._mac)
        except Exception as exc:  # noqa: BLE001
            log.warning("wake-on-lan failed: %s", exc)
            return "I couldn't send the wake signal to the TV."
        return ("Waking the TV. If nothing happens, switch on 'Network Standby' "
                "in its power settings.")

    def _power_off(self) -> str:
        if self._send("KEY_POWER"):
            return "Turned the TV off."
        return self._unreachable()

    # ── volume ──────────────────────────────────────────────────────────────
    def nudge_volume(self, direction: str, steps: int = 3) -> str:
        """Relative volume — always available, since it's just remote keys."""
        d = (direction or "").strip().lower()
        if d in {"up", "louder", "raise", "tez", "increase"}:
            key, word = "KEY_VOLUP", "up"
        elif d in {"down", "quieter", "lower", "kam", "decrease"}:
            key, word = "KEY_VOLDOWN", "down"
        else:
            return "Say volume up or volume down."
        n = max(1, min(30, int(steps or 1)))
        for i in range(n):
            if not self._send(key):
                return self._unreachable()
            if i < n - 1:
                time.sleep(0.08)  # the TV drops keys sent faster than this
        return f"Volume {word}."

    def set_volume(self, percent: int) -> str:
        """Absolute volume, via the TV's UPnP rendering control on port 9197."""
        pct = max(0, min(100, int(percent)))
        body = {"InstanceID": 0, "Channel": "Master", "DesiredVolume": pct}
        if self._upnp("RenderingControl", "SetVolume", body) is None:
            return ("I couldn't set an exact volume — this TV doesn't answer on "
                    "that channel. Ask me to turn it up or down instead.")
        return f"TV volume set to {pct}."

    def mute(self, on: bool | None = None) -> str:
        """Mute. `on` given → set it explicitly via UPnP; omitted → toggle key."""
        if on is None:
            if self._send("KEY_MUTE"):
                return "Toggled mute on the TV."
            return self._unreachable()
        body = {"InstanceID": 0, "Channel": "Master", "DesiredMute": 1 if on else 0}
        if self._upnp("RenderingControl", "SetMute", body) is None:
            # No UPnP: the toggle key is the only lever we have, and we can't
            # read the current state, so say what we actually did.
            if self._send("KEY_MUTE"):
                return "I toggled mute — I can't tell this TV which way round."
            return self._unreachable()
        return "Muted the TV." if on else "Unmuted the TV."

    # ── navigation & playback ───────────────────────────────────────────────
    def press(self, key: str, times: int = 1) -> str:
        name = (key or "").strip().lower()
        code = _KEYS.get(name)
        if code is None:
            return (f"I don't know the '{key}' button. I can do up, down, left, "
                    "right, ok, back, home, play, pause, forward and rewind.")
        n = max(1, min(20, int(times or 1)))
        for i in range(n):
            if not self._send(code):
                return self._unreachable()
            if i < n - 1:
                time.sleep(0.08)
        return f"{name.capitalize()}." if n == 1 else f"{name.capitalize()} ×{n}."

    # ── apps ────────────────────────────────────────────────────────────────
    def _app_list(self) -> list[dict]:
        """The TV's own installed-app list, cached after the first good read."""
        if self._apps is not None:
            return self._apps
        try:
            raw = self._client().app_list() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("TV app list failed: %s", exc)
            return []
        apps = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            app_id = a.get("appId") or a.get("app_id") or a.get("id")
            if app_id:
                apps.append({"id": str(app_id), "name": str(a.get("name") or app_id)})
        if apps:
            self._apps = apps
        return apps

    def _resolve_app(self, spoken: str) -> tuple[str, list[str]]:
        """Spoken name → (display name, candidate app IDs, best first)."""
        want = (spoken or "").strip().lower()
        canon = _APP_ALIASES.get(want, want)
        needle, fallbacks = _APPS.get(canon, (canon, []))

        ids: list[str] = []
        for app in self._app_list():  # the TV's own list is authoritative
            if needle and needle in app["name"].lower():
                ids.append(app["id"])
        ids.extend(i for i in fallbacks if i not in ids)
        return canon.title() if canon else spoken, ids

    def launch_app(self, app: str) -> str:
        label, ids = self._resolve_app(app)
        if not ids:
            return (f"I couldn't find {label} on the TV. Say 'what apps are on "
                    "the TV' to hear what's installed.")
        for app_id in ids:  # IDs differ by TV year — try until one takes
            try:
                self._client().run_app(app_id)
                return f"Opening {label} on the TV."
            except Exception as exc:  # noqa: BLE001
                log.warning("TV run_app %s failed: %s", app_id, exc)
        return self._unreachable()

    def play_title(self, title: str, app: str = "netflix") -> str:
        """Best-effort 'play <title> on <app>'.

        Tizen has no reliable name-search entry point: deep links want a
        service-specific content id, which we have no way to look up without
        each service's API. So we try a deep link (some apps do accept a plain
        query) and otherwise open the app and hand the search back to the user,
        rather than pretending we started playback.
        """
        name = (title or "").strip()
        if not name:
            return "What should I play?"
        label, ids = self._resolve_app(app)
        if not ids:
            return f"I couldn't find {label} on the TV."

        for app_id in ids:
            try:
                client = self._client()
                client.run_app(app_id, "DEEP_LINK", name)
                return f"Playing {name} on {label}."
            except TypeError:
                break  # this samsungtvws build has no deep-link support at all
            except Exception as exc:  # noqa: BLE001
                log.warning("TV deep link %s failed: %s", app_id, exc)

        opened = self.launch_app(app)
        if opened.startswith("Opening"):
            return (f"I've opened {label} — I can't jump straight to {name}, so "
                    "search for it there and I'll drive the buttons.")
        return opened

    def list_apps(self) -> str:
        apps = self._app_list()
        if not apps:
            return ("The TV didn't give me its app list. I can still open the "
                    "usual ones — try 'open Netflix on the TV'.")
        names = [a["name"] for a in apps[:25]]
        return "On the TV: " + ", ".join(names) + "."

    # ── state ───────────────────────────────────────────────────────────────
    def status(self) -> str:
        try:
            info = self._client().rest_device_info() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("TV device info failed: %s", exc)
            return "The TV isn't responding — it's probably off."
        device = info.get("device", {}) if isinstance(info, dict) else {}
        name = device.get("name") or info.get("name") or "The TV"
        state = str(device.get("PowerState") or "").lower()
        if state == "standby":
            return f"{name} is in standby."
        if state == "on":
            return f"{name} is on."
        return f"{name} is reachable."

    # ── UPnP ────────────────────────────────────────────────────────────────
    def _soap(self, service: str, action: str, args: dict) -> str | None:
        """One SOAP call to the TV's UPnP renderer. None = not supported/failed.

        Kept dependency-free on purpose: it's a single POST, and pulling in a
        UPnP stack for two actions isn't worth it.
        """
        ns = f"urn:schemas-upnp-org:service:{service}:1"
        fields = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
        envelope = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{ns}">{fields}</u:{action}></s:Body>'
            "</s:Envelope>"
        ).encode()
        req = Request(
            f"http://{self._host}:9197/upnp/control/{service}1",
            data=envelope,
            headers={"Content-Type": 'text/xml; charset="utf-8"',
                     "SOAPAction": f'"{ns}#{action}"'},
        )
        try:
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 — many models have no UPnP
            log.info("TV UPnP %s.%s unavailable: %s", service, action, exc)
            return None


def _send_magic_packet(mac: str) -> None:
    """Broadcast a Wake-on-LAN magic packet: 6×0xFF then the MAC 16 times."""
    clean = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(clean) != 12:
        raise ValueError(f"bad MAC address: {mac!r}")
    packet = b"\xff" * 6 + bytes.fromhex(clean) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Port 9 is the WoL discard port; 7 is the legacy echo port. TVs vary
        # in which they listen on, so hit both — a stray packet costs nothing.
        for port in (9, 7):
            sock.sendto(packet, ("255.255.255.255", port))
