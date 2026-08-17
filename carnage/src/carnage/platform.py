"""What a phone can do that a Raspberry Pi cannot, behind one seam.

Venom and Carnage share a mind and differ in body. Most of that difference is
already handled by capabilities — a Pi with no TV is never told about a TV —
but a handful of things exist on both and *work* differently: where you are,
how much charge is left, how a message actually leaves the device. Those
cannot be "on or off"; they need one name and two implementations.

Three of them matter enough to name the reason:

  * **Location.** The Pi does a network lookup and gets a city. A phone has
    GPS and gets a street. Same question, and the answers are different enough
    that ambient awareness can only use the second one: "you should leave now"
    needs to know you are still at home, and city-level never does.

  * **Messaging.** The Pi reaches people over WhatsApp, which needs the
    internet. A phone has a cellular radio and can send an SMS with no data at
    all. That is not a nicety — SOS is the one feature whose whole premise is
    that something has gone wrong, and it currently cannot run in the
    conditions most likely to produce it.

  * **Power.** A Pi reports its temperature and whether it has been throttled.
    A phone reports a battery percentage that determines whether it will still
    be alive in four hours. Both answer "are you all right?" and neither
    answer is the other.

**Every method degrades rather than raises.** This is the part that differs
most from the Pi, where a camera is either wired in or it is not, decided once
at boot. On a phone the user can revoke location permission while she is
speaking, and the honest answer to "where am I" becomes "I can't see that
right now" mid-sentence. So each call returns a value or None, records why,
and never propagates an exception into the voice loop.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("carnage.platform")

#: Long enough for a GPS fix to come back, short enough that a conversation
#: does not visibly stall waiting for one.
FIX_TIMEOUT = 12.0
QUICK_TIMEOUT = 5.0


@dataclass(frozen=True)
class Battery:
    percent: int
    charging: bool = False
    temperature: float = 0.0

    def spoken(self) -> str:
        state = "charging" if self.charging else "on battery"
        return f"{self.percent}% and {state}"


@dataclass(frozen=True)
class Fix:
    """Where the phone is, and how much that answer is worth.

    `accuracy` is carried because a 2 km fix and a 5 m fix are not the same
    fact and must not be spoken the same way. A wearable that says "you're at
    home" off a cell-tower fix is confidently wrong for a whole neighbourhood.
    """

    latitude: float
    longitude: float
    accuracy: float = 0.0
    provider: str = ""

    @property
    def coarse(self) -> bool:
        return self.accuracy > 500.0

    def spoken(self) -> str:
        where = f"{self.latitude:.5f}, {self.longitude:.5f}"
        if self.coarse:
            return f"roughly {where} — only a rough fix, within {int(self.accuracy)} m"
        return where


@dataclass(frozen=True)
class Notification:
    app: str
    title: str
    text: str
    when: float = 0.0


class Phone(Protocol):
    """The phone-shaped half of the device seam."""

    name: str

    def available(self) -> bool: ...

    def battery(self) -> Battery | None: ...

    def locate(self) -> Fix | None: ...

    def send_sms(self, to: str, text: str) -> bool: ...

    def notifications(self) -> list[Notification]: ...

    def vibrate(self, milliseconds: int = 400) -> bool: ...


class AbsentPhone:
    """No phone here — a laptop running the tests, or a dev box.

    Present so that everything above can be built and exercised without an
    Android device in the room. It answers honestly rather than pretending.
    """

    name = "absent"

    def available(self) -> bool:
        return False

    def battery(self) -> Battery | None:
        return None

    def locate(self) -> Fix | None:
        return None

    def send_sms(self, to: str, text: str) -> bool:
        log.info("sms not sent (no phone): to=%s %r", to, text[:60])
        return False

    def notifications(self) -> list[Notification]:
        return []

    def vibrate(self, milliseconds: int = 400) -> bool:
        return False


Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _run(command: Sequence[str], timeout: float):
    return subprocess.run(list(command), capture_output=True, text=True,
                          timeout=timeout, check=False)


class TermuxPhone:
    """Android through the Termux:API command line.

    This is the implementation that works *today*, with no app to build and no
    toolchain: install Termux and Termux:API and every one of these is a
    binary on PATH. It is slower than a real app (a process per call) and
    Android will eventually stop a background Termux session, so it is the way
    to run Carnage this week rather than the way to ship it. `AndroidPhone`
    below is the same seam with those costs removed.
    """

    name = "termux"

    def __init__(self, runner: Runner = _run,
                 has: Callable[[str], bool] = lambda tool: bool(shutil.which(tool))):
        self._run = runner
        self._has = has

    def available(self) -> bool:
        return self._has("termux-battery-status")

    def _json(self, command: Sequence[str], timeout: float) -> Any:
        try:
            done = self._run(command, timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("%s failed: %s", command[0], exc)
            return None
        if done.returncode != 0:
            # The overwhelmingly common cause is a permission the user has not
            # granted (or has revoked). Worth a log line, never an exception.
            log.info("%s refused: %s", command[0], (done.stderr or "").strip()[:120])
            return None
        try:
            return json.loads(done.stdout or "")
        except ValueError:
            log.warning("%s returned something that wasn't JSON", command[0])
            return None

    def battery(self) -> Battery | None:
        data = self._json(["termux-battery-status"], QUICK_TIMEOUT)
        if not isinstance(data, dict):
            return None
        try:
            return Battery(
                percent=int(data.get("percentage", 0)),
                charging=str(data.get("status", "")).upper() != "DISCHARGING",
                temperature=float(data.get("temperature", 0) or 0))
        except (TypeError, ValueError):
            return None

    def locate(self) -> Fix | None:
        # GPS first for accuracy, network as the fallback that at least
        # answers indoors. Asking only for GPS is how you get a wearable that
        # goes blind the moment its owner walks into a building.
        for provider in ("gps", "network"):
            data = self._json(["termux-location", "-p", provider], FIX_TIMEOUT)
            if isinstance(data, dict) and "latitude" in data:
                try:
                    return Fix(latitude=float(data["latitude"]),
                               longitude=float(data["longitude"]),
                               accuracy=float(data.get("accuracy", 0) or 0),
                               provider=provider)
                except (TypeError, ValueError, KeyError):
                    continue
        return None

    def send_sms(self, to: str, text: str) -> bool:
        if not to.strip() or not text.strip():
            return False
        try:
            done = self._run(["termux-sms-send", "-n", to, text], QUICK_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("sms send failed: %s", exc)
            return False
        return done.returncode == 0

    def notifications(self) -> list[Notification]:
        data = self._json(["termux-notification-list"], QUICK_TIMEOUT)
        if not isinstance(data, list):
            return []
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            out.append(Notification(
                app=str(row.get("packageName", "")),
                title=str(row.get("title", "")),
                text=str(row.get("content", "")),
                when=float(row.get("when", 0) or 0)))
        return out

    def vibrate(self, milliseconds: int = 400) -> bool:
        try:
            done = self._run(["termux-vibrate", "-d", str(int(milliseconds))],
                             QUICK_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0


class AndroidPhone:
    """Android through a host app that embeds this package (Chaquopy).

    The app registers one callable and everything routes through it, because
    the alternative — a binding method per capability — means a Kotlin change
    every time a tool is added, which puts the shared core back behind an app
    release. A single `call(name, **kwargs) -> Any` keeps new skills on the
    Python side where the rest of the assistant lives.

    Unlike Termux this survives being backgrounded: the host holds a
    foreground service, so the loop is not killed the moment the screen locks.
    """

    name = "android"

    def __init__(self, bridge: Callable[..., Any] | None = None):
        self._bridge = bridge

    def available(self) -> bool:
        return self._bridge is not None

    def _call(self, method: str, **kwargs: Any) -> Any:
        if self._bridge is None:
            return None
        try:
            return self._bridge(method, **kwargs)
        except Exception as exc:            # noqa: BLE001 — a Java exception
            # Anything can arrive across a JNI boundary, including errors that
            # are not Python exceptions in any useful sense.
            log.warning("android bridge %s failed: %s", method, exc)
            return None

    def battery(self) -> Battery | None:
        data = self._call("battery")
        if not isinstance(data, dict):
            return None
        return Battery(percent=int(data.get("percent", 0)),
                       charging=bool(data.get("charging", False)),
                       temperature=float(data.get("temperature", 0) or 0))

    def locate(self) -> Fix | None:
        data = self._call("locate")
        if not isinstance(data, dict) or "latitude" not in data:
            return None
        return Fix(latitude=float(data["latitude"]),
                   longitude=float(data["longitude"]),
                   accuracy=float(data.get("accuracy", 0) or 0),
                   provider=str(data.get("provider", "android")))

    def send_sms(self, to: str, text: str) -> bool:
        return bool(self._call("send_sms", to=to, text=text))

    def notifications(self) -> list[Notification]:
        rows = self._call("notifications")
        if not isinstance(rows, list):
            return []
        return [Notification(app=str(r.get("app", "")),
                             title=str(r.get("title", "")),
                             text=str(r.get("text", "")),
                             when=float(r.get("when", 0) or 0))
                for r in rows if isinstance(r, dict)]

    def vibrate(self, milliseconds: int = 400) -> bool:
        return bool(self._call("vibrate", milliseconds=int(milliseconds)))


def detect(bridge: Callable[..., Any] | None = None) -> Phone:
    """The best phone this process can actually reach.

    A host app passes its bridge and wins outright. Otherwise Termux is tried,
    and a dev box falls through to `AbsentPhone` — which is why every module
    above this one can be imported and tested on a laptop.
    """
    if bridge is not None:
        return AndroidPhone(bridge)
    termux = TermuxPhone()
    if termux.available():
        return termux
    log.info("no phone platform detected — running without phone skills")
    return AbsentPhone()
