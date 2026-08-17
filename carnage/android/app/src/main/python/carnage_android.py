"""The entry point the Android app calls into.

Thin on purpose. Everything below this line is the same `carnage` package the
Pi and the laptop run — this module only translates between Android's idea of
things and the package's: a files directory becomes a state directory, a Java
object becomes the callable `AndroidPhone` expects, and a config file is
created on first launch instead of being installed by hand.

The one piece of real logic is the bridge adaptation. Python cannot call a
Java object, so the object handed in from Kotlin is wrapped in a function with
the signature `AndroidPhone` was written against — `call(method, **kwargs)` —
and the keyword arguments become the Map the Kotlin side reads. Doing it here
rather than in Kotlin keeps the contract defined where it is consumed.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from pathlib import Path

log = logging.getLogger("carnage.android")

_carnage = None
_state: Path | None = None
_bridge = None
_lock = threading.Lock()


def _configure(state: Path) -> None:
    """Write a first config if there isn't one. Never overwrites."""
    path = state / "carnage.json"
    if path.exists():
        return
    token = secrets.token_hex(16)
    path.write_text(json.dumps({
        "device": "carnage",
        "user_name": "",
        "gemini_api_key": "",
        "hub": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 8790,
            "token": token,
            "peers": ["venom", "flint"],
        },
        # No page on this body: the app has its own screen, so serving a web
        # UI to itself would be two interfaces for one assistant.
        "web": {"enabled": False},
        "devices": [
            {"name": "venom", "body": "the wearable on his body",
             "can": ["listen on the walk", "the earphone",
                     "look around with the camera"]},
            {"name": "flint", "body": "on his desktop",
             "can": ["the screen", "his files and repos"]},
        ],
    }, indent=2), encoding="utf-8")
    log.info("wrote a first config to %s", path)


def start(files_dir: str, bridge):
    """Build the assistant. Returns a handle Kotlin keeps for the process."""
    global _carnage, _state, _bridge
    with _lock:
        _bridge = bridge
        if _carnage is not None:
            return _Handle()

        logging.basicConfig(level=logging.INFO)
        state = Path(files_dir) / "carnage"
        state.mkdir(parents=True, exist_ok=True)
        _state = state
        _configure(state)

        from carnage.config import load_config
        from carnage.platform import AndroidPhone
        from carnage.runtime import Carnage

        config = load_config(state / "carnage.json")
        # Android's files dir is app-private storage; the config points at it
        # so the stores land beside the config rather than in a home directory
        # that does not really exist here.
        from dataclasses import replace

        config = replace(config, state_dir=state)

        def call(method: str, **kwargs):
            # Java sees one Map; Python callers use keywords.
            return bridge.call(method, kwargs)

        _carnage = Carnage(config, phone=AndroidPhone(call))
        log.info("carnage up: %s", _carnage.describe())
        return _Handle()


def _settings() -> dict:
    if _state is None:
        return {}
    try:
        return json.loads((_state / "carnage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def needs_setup() -> bool:
    """True until she has a key to think with.

    The config lives in app-private storage, which Android 11 and later put
    out of reach of every file manager. So this is not a convenience: without
    a way to enter the key *in the app*, there is no way to enter it at all.
    """
    return not str(_settings().get("gemini_api_key", "") or "").strip()


def pairing() -> str:
    """What Venom needs to sync with this phone, as JSON for the screen."""
    settings = _settings()
    return json.dumps({
        "device": settings.get("device", "carnage"),
        "token": settings.get("hub", {}).get("token", ""),
        "port": settings.get("hub", {}).get("port", 8790),
        "user_name": settings.get("user_name", ""),
        "has_key": not needs_setup(),
    })


def configure(user_name: str, api_key: str) -> str:
    """Save what the setup screen collected and rebuild her around it.

    Rebuilt rather than patched: the key decides whether she can converse at
    all and which capabilities come up, and those are resolved once when the
    registry is built. Mutating the config underneath a live assistant would
    leave her holding a registry that no longer matches her configuration.
    """
    global _carnage
    with _lock:
        if _state is None:
            return "not started yet"
        path = _state / "carnage.json"
        settings = _settings()
        if user_name.strip():
            settings["user_name"] = user_name.strip()
        if api_key.strip():
            settings["gemini_api_key"] = api_key.strip()
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

        from dataclasses import replace

        from carnage.config import load_config
        from carnage.platform import AndroidPhone
        from carnage.runtime import Carnage

        config = replace(load_config(path), state_dir=_state)

        def call(method: str, **kwargs):
            return _bridge.call(method, kwargs)

        _carnage = Carnage(config, phone=AndroidPhone(call))
        log.info("reconfigured: %s", _carnage.describe())
        return _carnage.describe()


class _Handle:
    """What Kotlin holds for the life of the process.

    Deliberately holds no reference to the assistant. `configure` rebuilds her
    when a key is saved, and a handle that had captured the old instance would
    keep answering from it — so saving a key would appear to do nothing and she
    would go on insisting she has none. Looking her up per call costs a dict
    lookup and removes the whole class of bug.
    """

    def _live(self):
        if _carnage is None:
            raise RuntimeError("carnage is not started")
        return _carnage

    def describe(self) -> str:
        try:
            return self._live().describe()
        except Exception as exc:            # noqa: BLE001
            return f"not ready: {exc}"

    def needs_setup(self) -> bool:
        return needs_setup()

    def pairing(self) -> str:
        return pairing()

    def configure(self, user_name: str, api_key: str) -> str:
        return configure(user_name, api_key)

    def answer(self, said: str) -> str:
        try:
            return self._live().answer(said)
        except Exception as exc:            # noqa: BLE001
            log.exception("answer failed")
            return f"Something went wrong in my head: {exc}"

    def status(self) -> str:
        try:
            carnage = self._live()
        except Exception:                   # noqa: BLE001
            return json.dumps({"ready": False})
        return json.dumps({
            "ready": True,
            "device": carnage.config.device,
            "body": carnage.phone.name,
            "tools": len(list(carnage.registry)),
            "capabilities": carnage.capabilities.names(),
            "devices": [
                {"name": d.name, "body": d.body, "presence": d.presence(_now())}
                for d in carnage.roster.others()
            ],
        })


def _now() -> float:
    import time

    return time.time()
