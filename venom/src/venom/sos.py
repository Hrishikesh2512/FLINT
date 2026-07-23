"""Emergency SOS — the one thing on this device that has to work.

Say the word and Venom WhatsApps every emergency contact with who needs help,
where they are, and when — then *stays in emergency mode*, resending the
location every few minutes until it's called off, so whoever is coming can
follow a moving dot instead of a single stale pin.

The contact book is deliberately its own thing, separate from Connections and
from whoever you last messaged: the people you want in an emergency are rarely
the people you WhatsApp most, and each one can have their own wording (what
you'd send your father is not what you'd send your flatmate).

Everything degrades instead of raising. A failed geolookup still sends the
alert; a contact whose message bounces is *reported as failed* rather than
quietly counted as delivered — an SOS that lies about who it reached is worse
than no SOS.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Same package, same file format: reuse the stores' atomic write + lock rather
# than growing a second copy of it here.
from venom.stores import _JsonStore

log = logging.getLogger("venom.sos")

# Alert text. {user}, {location}, {time} and {note} are filled in per send.
DEFAULT_MESSAGE = (
    "🚨 EMERGENCY — {user} needs help.\n"
    "Sent automatically by {user}'s assistant, Venom.\n"
    "{location}"
    "Time: {time}\n"
    "{note}"
    "Please try to reach {user} now."
)
DEFAULT_UPDATE = (
    "🚨 {user} is still in an emergency — location as of {time}:\n{location}"
)
DEFAULT_ALL_CLEAR = (
    "✅ All clear — {user} is safe and has ended the emergency at {time}.\n"
    "{note}"
)
TEST_BANNER = (
    "✅ THIS IS ONLY A TEST — no emergency. {user} is checking that the SOS "
    "alert reaches you. Nothing to do.\n\n"
)

DEFAULT_REPEAT_MINUTES = 10.0
MIN_REPEAT_MINUTES = 1.0


class SosStore(_JsonStore):
    """The emergency contact book plus SOS wording, on disk in the state dir.

    Document shape:
        {"contacts": [{"name", "to", "label", "message", "enabled"}],
         "message": "", "update": "", "all_clear": "",
         "repeat_minutes": 10, "include_location": true}

    A blank template field means "use the built-in wording", so upgrading the
    defaults doesn't require rewriting everyone's file.
    """

    @staticmethod
    def _clean(raw: dict) -> dict:
        name = str(raw.get("name", "")).strip()
        return {
            "name":    name,
            "to":      str(raw.get("to", "")).strip(),
            "label":   str(raw.get("label", "")).strip(),
            "message": str(raw.get("message", "")).strip(),
            "enabled": bool(raw.get("enabled", True)),
        }

    def config(self) -> dict:
        """The whole document, repaired. Never raises: a corrupt file reads as
        'no contacts' rather than exploding halfway through an emergency."""
        raw = self._load({})
        if not isinstance(raw, dict):
            raw = {}
        contacts = [self._clean(c) for c in (raw.get("contacts") or [])
                    if isinstance(c, dict) and str(c.get("name", "")).strip()]
        try:
            repeat = float(raw.get("repeat_minutes", DEFAULT_REPEAT_MINUTES))
        except (TypeError, ValueError):
            repeat = DEFAULT_REPEAT_MINUTES
        return {
            "contacts": contacts,
            "message": str(raw.get("message", "") or "").strip(),
            "update": str(raw.get("update", "") or "").strip(),
            "all_clear": str(raw.get("all_clear", "") or "").strip(),
            "repeat_minutes": max(0.0, repeat),
            "include_location": bool(raw.get("include_location", True)),
        }

    def contacts(self, enabled_only: bool = False) -> list[dict]:
        people = self.config()["contacts"]
        return [c for c in people if c["enabled"]] if enabled_only else people

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return a.strip().lower() == b.strip().lower()

    def add(self, name: str, to: str = "", label: str = "",
            message: str = "", enabled: bool = True) -> dict:
        """Add or update a contact, matched by name (case-insensitive), so
        'add papa' twice edits Papa instead of alerting him twice."""
        name = (name or "").strip()
        if not name:
            return {}
        with self._lock:
            data = self.config()
            entry = self._clean({"name": name, "to": to, "label": label,
                                 "message": message, "enabled": enabled})
            for i, existing in enumerate(data["contacts"]):
                if self._same(existing["name"], name):
                    # Only overwrite fields the caller actually supplied —
                    # "add papa nickname" must not wipe his number.
                    merged = dict(existing)
                    for key in ("to", "label", "message"):
                        if entry[key]:
                            merged[key] = entry[key]
                    merged["enabled"] = entry["enabled"]
                    data["contacts"][i] = merged
                    self._save(data)
                    return merged
            data["contacts"].append(entry)
            self._save(data)
        return entry

    def remove(self, name: str) -> bool:
        with self._lock:
            data = self.config()
            keep = [c for c in data["contacts"] if not self._same(c["name"], name)]
            if len(keep) == len(data["contacts"]):
                return False
            data["contacts"] = keep
            self._save(data)
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            data = self.config()
            hit = False
            for contact in data["contacts"]:
                if self._same(contact["name"], name):
                    contact["enabled"] = bool(enabled)
                    hit = True
            if hit:
                self._save(data)
        return hit

    def set_message(self, message: str, name: str = "") -> bool:
        """Set the alert wording for one contact, or the default for everyone.
        Blank clears it back to the built-in text. False = no such contact."""
        message = (message or "").strip()
        with self._lock:
            data = self.config()
            if not name:
                data["message"] = message
                self._save(data)
                return True
            for contact in data["contacts"]:
                if self._same(contact["name"], name):
                    contact["message"] = message
                    self._save(data)
                    return True
        return False

    def set_settings(self, repeat_minutes: float | None = None,
                     include_location: bool | None = None) -> dict:
        with self._lock:
            data = self.config()
            if repeat_minutes is not None:
                value = max(0.0, float(repeat_minutes))
                # A one-second repeat would spam contacts and burn the bridge's
                # rate limit; below the floor means "off".
                data["repeat_minutes"] = (0.0 if value == 0
                                          else max(MIN_REPEAT_MINUTES, value))
            if include_location is not None:
                data["include_location"] = bool(include_location)
            self._save(data)
        return data


def describe_contact(contact: dict) -> str:
    bits = [contact["name"]]
    if contact.get("label"):
        bits.append(f"({contact['label']})")
    if contact.get("to"):
        bits.append(f"→ {contact['to']}")
    if not contact.get("enabled", True):
        bits.append("[paused]")
    if contact.get("message"):
        bits.append("[own wording]")
    return " ".join(bits)


class EmergencySos:
    """Fires the alert and holds emergency mode open until it's called off."""

    def __init__(self, store: SosStore, whatsapp, location=None,
                 connections=None, user_name: str = "",
                 clock: Callable[[], float] = time.time,
                 spawn: Callable[[Callable[[], None]], None] | None = None):
        self._store = store
        self._whatsapp = whatsapp
        self._location = location
        self._connections = connections
        self._user = (user_name or "").strip() or "your friend"
        self._clock = clock
        self._spawn = spawn or self._thread
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.active = False
        self.started_at = 0.0
        self.last_sent = 0.0
        self.last_summary = ""

    @property
    def store(self) -> SosStore:
        return self._store

    @staticmethod
    def _thread(target: Callable[[], None]) -> None:
        threading.Thread(target=target, daemon=True, name="venom-sos").start()

    # ── message building ─────────────────────────────────────────────────────
    def _location_line(self, config: dict, force: bool = True) -> str:
        if not config["include_location"] or self._location is None:
            return ""
        try:
            loc = self._location.get(force=force)
        except Exception as exc:                 # geolookup must never block SOS
            log.warning("sos location lookup failed: %s", exc)
            loc = None
        if not loc:
            return "Location: unavailable.\n"
        where = ", ".join(p for p in (loc.get("city"), loc.get("region"),
                                      loc.get("country")) if p)
        line = f"Location: {where or 'unknown'}"
        lat, lon = loc.get("lat"), loc.get("lon")
        if lat is not None and lon is not None:
            line += f" — https://maps.google.com/?q={lat},{lon}"
        return line + "\n(approximate — from the network, not GPS)\n"

    def _render(self, template: str, location: str = "", note: str = "") -> str:
        return (template
                .replace("{user}", self._user)
                .replace("{location}", location)
                .replace("{note}", f"{note}\n" if note else "")
                .replace("{time}", time.strftime("%d %b, %I:%M %p",
                                                 time.localtime(self._clock()))))

    def _target(self, contact: dict) -> str:
        """Who the bridge should send to: the contact's own number/name if
        given, else their name resolved through Connections (a saved number
        beats WhatsApp's own contact matching), else the bare name."""
        to = contact.get("to", "").strip()
        if to:
            return to
        if self._connections is not None:
            try:
                number = self._connections.phone_for(contact["name"])
            except Exception:
                number = ""
            if number:
                return number
        return contact["name"]

    # ── sending ──────────────────────────────────────────────────────────────
    def _broadcast(self, contacts: list[dict], config: dict, *,
                   template_key: str, fallback: str, location: str = "",
                   note: str = "", per_contact: bool = False,
                   test: bool = False) -> tuple[list[str], list[str]]:
        sent: list[str] = []
        failed: list[str] = []
        for contact in contacts:
            template = ((contact.get("message") if per_contact else "")
                        or config.get(template_key) or fallback)
            text = self._render(template, location=location, note=note)
            if test:
                text = self._render(TEST_BANNER) + text
            try:
                ok, detail = self._whatsapp.send_detail(text, self._target(contact))
            except Exception as exc:              # a dead bridge is a failure,
                ok, detail = False, str(exc)      # not a crashed voice loop
            log.info("sos → %s: %s", contact["name"], detail)
            (sent if ok else failed).append(contact["name"])
        self.last_sent = self._clock()
        return sent, failed

    @staticmethod
    def _summarize(label: str, sent: list[str], failed: list[str],
                   total_fail: str = "") -> str:
        if sent and not failed:
            return f"{label} {_join(sent)}."
        if sent and failed:
            return (f"{label} {_join(sent)}, but it did NOT reach "
                    f"{_join(failed)} — reach them another way.")
        return total_fail or (
            f"It did NOT go through to {_join(failed)}. Nobody has been "
            f"alerted — call them directly, now.")

    # ── public API ───────────────────────────────────────────────────────────
    def start(self, note: str = "") -> str:
        """Alert everyone and hold emergency mode open."""
        config = self._store.config()
        contacts = [c for c in config["contacts"] if c["enabled"]]
        if not contacts:
            return ("There are no emergency contacts saved, so I couldn't alert "
                    "anyone. Tell me who to add — name and number — and I'll set "
                    "them up right now.")

        with self._lock:
            already = self.active
            self.active = True
            if not already:
                self.started_at = self._clock()
                self._stop.clear()

        location = self._location_line(config)
        sent, failed = self._broadcast(
            contacts, config, template_key="message", fallback=DEFAULT_MESSAGE,
            location=location, note=note, per_contact=True)

        if not already and config["repeat_minutes"] > 0:
            self._spawn(self._repeat_loop)

        summary = self._summarize("Emergency alert sent to", sent, failed)
        if sent:
            minutes = config["repeat_minutes"]
            summary += (f" I'm staying in emergency mode and will resend your "
                        f"location every {minutes:g} minutes until you tell me "
                        f"it's over." if minutes > 0 else
                        " I'm staying in emergency mode until you tell me it's over.")
        self.last_summary = summary
        return summary

    def _repeat_loop(self) -> None:
        """Resend the location on a cadence until the emergency is called off."""
        while not self._stop.is_set():
            config = self._store.config()
            minutes = config["repeat_minutes"]
            if minutes <= 0:
                return
            if self._stop.wait(minutes * 60):
                return
            if not self.active:
                return
            contacts = [c for c in config["contacts"] if c["enabled"]]
            if not contacts:
                return
            try:
                self._broadcast(contacts, config, template_key="update",
                                fallback=DEFAULT_UPDATE,
                                location=self._location_line(config))
            except Exception as exc:      # a bad update must not end the mode
                log.warning("sos update failed: %s", exc)

    def stop(self, note: str = "") -> str:
        """End emergency mode and tell everyone who was alerted."""
        with self._lock:
            if not self.active:
                return "You're not in emergency mode — nothing to call off."
            self.active = False
            self._stop.set()

        config = self._store.config()
        contacts = [c for c in config["contacts"] if c["enabled"]]
        sent, failed = self._broadcast(
            contacts, config, template_key="all_clear",
            fallback=DEFAULT_ALL_CLEAR, note=note)
        summary = self._summarize(
            "Emergency mode off. I told", sent, failed,
            total_fail=(f"Emergency mode is off, but the all-clear did NOT "
                        f"reach {_join(failed)} — they still think you're in "
                        f"trouble, so message them yourself."))
        self.last_summary = summary
        return summary

    def test(self) -> str:
        """Send a clearly-marked drill so the wiring can be proven in advance —
        without a real emergency being the first time anyone finds out."""
        config = self._store.config()
        contacts = [c for c in config["contacts"] if c["enabled"]]
        if not contacts:
            return "There are no emergency contacts saved yet, so there's nothing to test."
        sent, failed = self._broadcast(
            contacts, config, template_key="message", fallback=DEFAULT_MESSAGE,
            location=self._location_line(config, force=False),
            note="", per_contact=True, test=True)
        return self._summarize("Test alert sent to", sent, failed)

    def status(self) -> str:
        contacts = self._store.contacts()
        enabled = [c for c in contacts if c["enabled"]]
        if not self.active:
            if not contacts:
                return ("Emergency mode is off, and no emergency contacts are "
                        "saved yet — worth setting one up.")
            return ("Emergency mode is off. On SOS I'd alert "
                    + _join([describe_contact(c) for c in enabled]) + ".")
        mins = max(0, int((self._clock() - self.started_at) // 60))
        return (f"Emergency mode has been on for {mins} minute"
                f"{'' if mins == 1 else 's'}, alerting "
                f"{_join([c['name'] for c in enabled])}. {self.last_summary}")

    def snapshot(self) -> dict:
        """State for the web console."""
        config = self._store.config()
        return {"active": self.active, "started_at": self.started_at,
                "last_sent": self.last_sent, "summary": self.last_summary,
                "contacts": config["contacts"],
                "repeat_minutes": config["repeat_minutes"],
                "include_location": config["include_location"]}


def manage_contacts(sos: EmergencySos, action: str, name: str = "",
                    to: str = "", label: str = "", message: str = "") -> str:
    """One entry point for editing the SOS book, shared by the voice tool and
    the web console so both behave identically."""
    act = (action or "").strip().lower()
    store = sos.store
    name = (name or "").strip()

    if act in ("add", "save", "set"):
        if not name:
            return "I need a name to add to your emergency contacts."
        entry = store.add(name, to=to, label=label, message=message)
        who = f" ({entry['label']})" if entry.get("label") else ""
        where = (f" I'll message {entry['to']}." if entry.get("to") else
                 " I'll message them by that name on WhatsApp — tell me their "
                 "number if it isn't an exact match.")
        return f"{entry['name']}{who} is on your emergency list.{where}"

    if act in ("remove", "delete", "del"):
        if not name:
            return "Which emergency contact should I remove?"
        return (f"{name} is off your emergency list." if store.remove(name)
                else f"There's no emergency contact called {name}.")

    if act in ("list", "show", ""):
        contacts = store.contacts()
        if not contacts:
            return "You have no emergency contacts saved yet."
        return "Emergency contacts: " + "; ".join(
            describe_contact(c) for c in contacts) + "."

    if act in ("enable", "disable", "pause", "resume"):
        if not name:
            return f"Which contact should I {act}?"
        on = act in ("enable", "resume")
        if not store.set_enabled(name, on):
            return f"There's no emergency contact called {name}."
        return (f"{name} will be alerted on SOS." if on
                else f"{name} is paused — they won't be alerted on SOS.")

    if act in ("set_message", "message", "wording"):
        if not store.set_message(message, name):
            return f"There's no emergency contact called {name}."
        if name:
            return (f"{name}'s SOS message updated." if message.strip()
                    else f"{name} goes back to the standard SOS message.")
        return ("Default SOS message updated." if message.strip()
                else "SOS message reset to the standard wording.")

    if act in ("test", "drill"):
        return sos.test()

    if act == "status":
        return sos.status()

    return (f"I don't know the SOS action '{action}'. I can add, remove, list, "
            f"enable, disable, set_message, or test.")


def _join(names: list[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return "nobody"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def build_sos(state_dir: Path, whatsapp, location=None, connections=None,
              user_name: str = "") -> EmergencySos:
    return EmergencySos(SosStore(Path(state_dir) / "sos.json"), whatsapp,
                        location=location, connections=connections,
                        user_name=user_name)
