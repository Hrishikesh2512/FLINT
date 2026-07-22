"""Voice control for Tuya / Smart Life smart bulbs, over the LAN.

Tuya bulbs (Wipro, Syska, Halonix, Havells and most generic Wi-Fi bulbs) speak
an encrypted local protocol: given each bulb's device id + local key + address
you can drive it directly on the home network, with no cloud round-trip. That
suits Venom — the Pi is already on the same Wi-Fi, so 'lights warm karo' is a
LAN packet away and keeps working even if the internet drops.

The one-time cost is extracting the local keys (Tuya encrypts local control).
That's done off-device with `python -m tinytuya wizard` against a free Tuya IoT
account linked to the Smart Life app; it writes a devices.json we drop on the
Pi. See provisioning/venom.toml [lights] for the walkthrough.

Design mirrors the other tool clients: every call is short-lived and best
effort, failures degrade into a plain spoken sentence, and nothing here can
crash the voice loop. The bulb driver is injected (default: tinytuya) so this
is testable without hardware, and tinytuya is imported lazily so the dependency
is only needed when lights are actually configured.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("venom.lights")

# Spoken words that mean "every light", across the Hinglish the user speaks.
_ALL_WORDS = {
    "", "all", "everything", "every light", "all lights", "the lights",
    "sab", "sabhi", "saare", "sare", "sari", "house", "home", "ghar",
}

# Named colours → RGB. Kept small and obvious; the model maps a spoken colour
# onto the closest of these before calling.
_COLOURS = {
    "red": (255, 0, 0), "crimson": (220, 20, 60), "orange": (255, 90, 0),
    "amber": (255, 160, 0), "yellow": (255, 220, 0), "lime": (170, 255, 0),
    "green": (0, 255, 0), "teal": (0, 200, 160), "cyan": (0, 220, 255),
    "sky": (60, 170, 255), "blue": (0, 60, 255), "indigo": (60, 0, 200),
    "violet": (150, 0, 255), "purple": (170, 0, 220), "magenta": (255, 0, 200),
    "pink": (255, 90, 160), "rose": (255, 100, 130),
}

# White presets → colour-temperature percentage (0 = warmest, 100 = coolest).
_WHITES = {
    "warm": 0, "warm white": 0, "soft white": 15, "warm light": 0,
    "neutral": 50, "neutral white": 50, "white": 55, "plain white": 55,
    "cool": 100, "cool white": 100, "daylight": 100, "cold": 100,
}

# Small named scenes: (colour-or-white, brightness%). Colour is an RGB tuple, or
# a colour-temp int for a white scene. Purely a nicety over raw colour+dim.
_SCENES = {
    "relax":   ((255, 120, 40), 40),
    "reading": (55, 100),          # neutral-cool white, full
    "movie":   ((80, 0, 160), 20),
    "focus":   (85, 100),          # cool white, full
    "night":   ((255, 90, 20), 8),
    "party":   ((255, 0, 200), 90),
    "romantic": ((255, 40, 90), 25),
    "sunset":  ((255, 70, 20), 45),
}


class LightsController:
    """Loads a bulb registry and drives Tuya bulbs on the LAN.

    Registry file (JSON) is either a bare list or {"devices": [...]}, each entry
    tinytuya-shaped: {name, id, key|local_key, ip|address, version|ver, room?}.
    The file is re-read on each op so the user can edit it (add a bulb, fix an
    IP) without restarting Venom.
    """

    def __init__(self, registry_path: str | Path,
                 bulb_factory: Callable[..., Any] | None = None):
        self._path = Path(registry_path)
        self._factory = bulb_factory  # None → lazy-import tinytuya on first use

    # ── registry ────────────────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("lights registry unreadable (%s): %s", self._path, exc)
            return []
        raw = data.get("devices", []) if isinstance(data, dict) else data
        out = []
        for d in raw or []:
            key = d.get("key") or d.get("local_key")
            dev_id = d.get("id") or d.get("dev_id")
            if not (key and dev_id):
                continue  # a bulb with no key can't be controlled locally
            out.append({
                "name": str(d.get("name") or dev_id),
                "id": str(dev_id),
                "key": str(key),
                "ip": str(d.get("ip") or d.get("address") or "").strip() or "Auto",
                "version": float(d.get("version") or d.get("ver") or 3.3),
                "room": str(d.get("room") or "").strip(),
            })
        return out

    def has_devices(self) -> bool:
        return bool(self._load())

    def _resolve(self, where: str) -> list[dict]:
        """Pick the bulbs a `where` phrase refers to: a name, a room, or all."""
        devs = self._load()
        w = (where or "").strip().lower()
        if w in _ALL_WORDS:
            return devs
        hits = [d for d in devs
                if w in d["name"].lower() or (d["room"] and w in d["room"].lower())]
        if not hits:  # loosen to any-word overlap ("living room lamp")
            toks = [t for t in w.split() if t]
            hits = [d for d in devs if any(
                t in d["name"].lower() or (d["room"] and t in d["room"].lower())
                for t in toks)]
        return hits

    # ── driver ──────────────────────────────────────────────────────────────
    def _bulb(self, d: dict):
        factory = self._factory
        if factory is None:
            import tinytuya  # lazy: only needed once lights are configured
            factory = tinytuya.BulbDevice
        bulb = factory(d["id"], d["ip"], d["key"])
        # tinytuya needs the right protocol version or status/commands silently
        # fail; set it if the driver supports it (older/newer APIs both exist).
        setter = getattr(bulb, "set_version", None)
        if callable(setter):
            try:
                setter(d["version"])
            except Exception:  # noqa: BLE001 — never let a driver quirk escape
                pass
        return bulb

    def _apply(self, where: str, action: Callable[[Any], None],
               ) -> tuple[list[str], list[str]]:
        """Run `action` against every resolved bulb; return (done, failed) names."""
        done: list[str] = []
        failed: list[str] = []
        for d in self._resolve(where):
            try:
                action(self._bulb(d))
                done.append(d["name"])
            except Exception as exc:  # noqa: BLE001 — one dead bulb ≠ a crash
                log.warning("light '%s' command failed: %s", d["name"], exc)
                failed.append(d["name"])
        return done, failed

    # ── spoken helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _phrase(where: str, done: list[str], failed: list[str], did: str) -> str:
        if not done and not failed:
            hint = "" if (where or "").strip().lower() in _ALL_WORDS \
                else f' matching "{where.strip()}"'
            return (f"I couldn't find any light{hint}. "
                    "Say 'list my lights' to hear what I've got.")
        target = "all the lights" if not (where or "").strip() \
            or (where.strip().lower() in _ALL_WORDS) else where.strip()
        if done and not failed:
            return f"{did.capitalize()} {target}."
        if done and failed:
            return (f"{did.capitalize()} {', '.join(done)}, but "
                    f"{', '.join(failed)} didn't respond.")
        return f"I couldn't reach {', '.join(failed)} — is it powered and on Wi-Fi?"

    # ── public operations ─────────────────────────────────────────────────────
    def power(self, on: bool, where: str = "") -> str:
        done, failed = self._apply(
            where, lambda b: (b.turn_on() if on else b.turn_off()))
        return self._phrase(where, done, failed,
                            "turned on" if on else "turned off")

    def brightness(self, percent: int, where: str = "") -> str:
        pct = max(1, min(100, int(percent)))

        def act(b):
            b.turn_on()
            b.set_brightness_percentage(pct)

        done, failed = self._apply(where, act)
        return self._phrase(where, done, failed, f"set to {pct}%")

    def colour(self, colour: str, where: str = "") -> str:
        name = (colour or "").strip().lower()
        rgb = _COLOURS.get(name)
        temp = _WHITES.get(name)
        if rgb is None and temp is None:
            return (f"I don't know the colour '{colour}'. Try a colour like red, "
                    "blue, green, or a white like warm, neutral or cool.")

        def act(b):
            b.turn_on()
            if rgb is not None:
                b.set_colour(*rgb)
            else:
                mode = getattr(b, "set_colourtemp_percentage", None)
                if callable(mode):
                    mode(temp)
                else:  # very old drivers — best effort white mode
                    b.set_white()

        done, failed = self._apply(where, act)
        return self._phrase(where, done, failed, f"turned {name}")

    def scene(self, scene: str, where: str = "") -> str:
        name = (scene or "").strip().lower()
        preset = _SCENES.get(name)
        if preset is None:
            return (f"I don't have a '{scene}' scene. I know: "
                    + ", ".join(sorted(_SCENES)) + ".")
        colour, bright = preset

        def act(b):
            b.turn_on()
            if isinstance(colour, tuple):
                b.set_colour(*colour)
            else:
                mode = getattr(b, "set_colourtemp_percentage", None)
                if callable(mode):
                    mode(colour)
            b.set_brightness_percentage(bright)

        done, failed = self._apply(where, act)
        return self._phrase(where, done, failed, f"set the {name} scene on")

    def list_lights(self) -> str:
        devs = self._load()
        if not devs:
            return ("You have no lights set up yet. Add them to the lights "
                    "registry on the Pi — see the [lights] setup notes.")
        names = []
        for d in devs:
            names.append(f"{d['name']} ({d['room']})" if d["room"] else d["name"])
        return "Your lights: " + ", ".join(names) + "."
