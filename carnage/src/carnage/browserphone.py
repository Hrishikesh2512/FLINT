"""A phone body made of a browser tab, so nothing has to be installed.

Termux gives the fullest phone — real SMS, the notification shade, a process
that keeps running. It also costs three apps from F-Droid and a terminal, and
that is a real price for something meant to live in a pocket.

A browser is already on the phone. Open a page, add it to the home screen, and
it looks and launches like an app. What matters here is that a modern mobile
browser is not a poor imitation of a phone — it *is* the phone for three of the
four things that made this body worth having:

    GPS          navigator.geolocation, the same fix a native app gets
    battery      the Battery Status API
    voice        getUserMedia, the same microphone

The fourth is where it stops, and the stop is hard rather than partial. **A web
page cannot send an SMS.** It can hand one to the messaging app with the
recipient and body pre-filled, and the person taps send. That is a genuinely
useful fallback and it is *not* the same thing, which is why `sends_directly`
is False here and why every message this body produces says "tap send" rather
than "sent". Getting that wrong would mean telling someone their father had
been alerted when the message was sitting in a composer.

Two more things the browser will not do: read the notification shade (no API
exists, and nor should one), and keep running with the screen off (a service
worker is woken for events, not kept alive). So this body is what she is when
he is *holding* the phone, and Termux is what she is when she needs to act
without him.

The class is fed by `carnage.web`: the page posts readings in, tools read them
out. Everything is a cached reading with an age, because a browser tab that
was closed an hour ago must not be able to report where he was then as where
he is now.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from carnage.platform import Battery, Fix, Notification

log = logging.getLogger("carnage.browser")

#: A reading older than this is not served. Chosen to be a little longer than
#: the page's own refresh interval, so one missed post does not blind her,
#: while a closed tab goes quiet quickly. Stale location is worse than none:
#: acting on where he was an hour ago is a confident, specific mistake.
FRESH_SECONDS = 90.0


@dataclass
class _Reading:
    value: object = None
    at: float = 0.0

    def fresh(self, now: float, window: float = FRESH_SECONDS) -> bool:
        return self.value is not None and (now - self.at) <= window


@dataclass
class Outbox:
    """One message waiting for the page to open the SMS composer."""

    to: str
    text: str
    at: float


class BrowserPhone:
    """The phone, as seen through a page it is displaying."""

    name = "browser"
    #: The whole reason this attribute exists. See the module docstring.
    sends_directly = False

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = Lock()
        self._fix = _Reading()
        self._battery = _Reading()
        self._seen_at = 0.0
        self._outbox: list[Outbox] = []

    # ── fed by the page ─────────────────────────────────────────────────────
    def report(self, reading: dict) -> None:
        """Take a batch of readings the page just posted. Never raises."""
        now = self._clock()
        with self._lock:
            self._seen_at = now
            fix = reading.get("location")
            if isinstance(fix, dict) and "latitude" in fix:
                try:
                    self._fix = _Reading(Fix(
                        latitude=float(fix["latitude"]),
                        longitude=float(fix["longitude"]),
                        accuracy=float(fix.get("accuracy", 0) or 0),
                        provider="browser"), now)
                except (TypeError, ValueError, KeyError):
                    log.info("browser sent a location that would not parse")
            power = reading.get("battery")
            if isinstance(power, dict) and "percent" in power:
                try:
                    self._battery = _Reading(Battery(
                        percent=int(round(float(power["percent"]))),
                        charging=bool(power.get("charging", False))), now)
                except (TypeError, ValueError):
                    log.info("browser sent a battery reading that would not parse")

    def connected(self) -> bool:
        """True while a page is actually open and posting."""
        return (self._clock() - self._seen_at) <= FRESH_SECONDS

    # ── the Phone protocol ──────────────────────────────────────────────────
    def available(self) -> bool:
        """True: this device *is* browser-bodied, whether or not a page is open.

        The tempting alternative — available only while a page is posting —
        looks more honest and is worse. Capabilities are resolved once, when
        the registry is built, so tying them to a tab that comes and goes means
        the phone skills exist or not depending on whether he happened to have
        the app open at start-up. He would ask where he is and be told there is
        no such tool.

        The honesty belongs one level down instead, and is already there: every
        reading returns None when it is stale, so the tools say "I can't see
        that right now" — which is true, specific, and recoverable the moment
        he opens the page.
        """
        return True

    def battery(self) -> Battery | None:
        now = self._clock()
        with self._lock:
            return self._battery.value if self._battery.fresh(now) else None

    def locate(self) -> Fix | None:
        now = self._clock()
        with self._lock:
            return self._fix.value if self._fix.fresh(now) else None

    def send_sms(self, to: str, text: str) -> bool:
        """Queue a message for the page to open in the SMS app.

        Returns True for "handed over", never for "delivered" — the caller is
        expected to check `sends_directly` and say which of those happened.
        """
        if not to.strip() or not text.strip():
            return False
        if not self.connected():
            # Nothing is holding the phone, so nothing will tap send. Saying so
            # is the point: a queue that fills up silently is a queue that
            # fails silently.
            log.info("sms not queued: no page connected")
            return False
        with self._lock:
            self._outbox.append(Outbox(to.strip(), text.strip(), self._clock()))
        return True

    def notifications(self) -> list[Notification]:
        # No web API exposes the notification shade, and none should.
        return []

    def vibrate(self, milliseconds: int = 400) -> bool:
        # The page vibrates on its own when it takes a turn; there is nothing
        # useful to queue here, and pretending otherwise would be a lie.
        return False

    # ── drained by the page ─────────────────────────────────────────────────
    def take_outbox(self) -> list[dict]:
        """Messages for the page to open, oldest first. Drains the queue."""
        with self._lock:
            waiting, self._outbox = self._outbox, []
        return [{"to": m.to, "text": m.text, "at": m.at} for m in waiting]

    def pending(self) -> int:
        return len(self._outbox)
