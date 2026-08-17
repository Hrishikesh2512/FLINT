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
    global _carnage
    with _lock:
        if _carnage is not None:
            return _Handle(_carnage)

        logging.basicConfig(level=logging.INFO)
        state = Path(files_dir) / "carnage"
        state.mkdir(parents=True, exist_ok=True)
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
        return _Handle(_carnage)


class _Handle:
    """What Kotlin holds. Every method is safe to call from any thread."""

    def __init__(self, carnage):
        self._carnage = carnage

    def describe(self) -> str:
        return self._carnage.describe()

    def answer(self, said: str) -> str:
        try:
            return self._carnage.answer(said)
        except Exception as exc:            # noqa: BLE001
            log.exception("answer failed")
            return f"Something went wrong in my head: {exc}"

    def status(self) -> str:
        phone = self._carnage.phone
        return json.dumps({
            "device": self._carnage.config.device,
            "body": phone.name,
            "tools": len(list(self._carnage.registry)),
            "capabilities": self._carnage.capabilities.names(),
            "devices": [
                {"name": d.name, "body": d.body, "presence": d.presence(_now())}
                for d in self._carnage.roster.others()
            ],
        })


def _now() -> float:
    import time

    return time.time()
