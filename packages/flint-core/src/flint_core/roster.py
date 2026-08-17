"""The other bodies she has, and what each of them can do.

Sync made the memory one. This makes the *self* one. Without it each device is
an amnesiac about its own siblings: ask the Pi to text someone and it says it
cannot, which is true of that body and false of her — the phone in his pocket
could do it in a second. Being told "I can't" by something that plainly could
is the single fastest way to stop believing you are talking to one assistant.

So each device carries a roster of the others: the name, the body, what that
body can do, and when it was last heard from. It renders into the system
prompt as a short block, and it is written in the first person on purpose —

    Your other bodies:
      - on his phone (carnage): send a text, GPS position, emergency SMS.
        Reachable now.
      - on his desktop (flint): the screen, the files, the repos. Last seen
        3 hours ago.

— because "your other bodies" is what they are. The instruction that goes with
it is the important half: never say you cannot do something that one of your
other bodies can. Say where it has to happen, or just do it there.

**Presence is a fact, not an assumption.** A device is "reachable now" only
because it synced within the freshness window; beyond that she says when it
was last seen instead. Claiming the laptop is available when it has been shut
for a day produces a confident, specific, wrong answer — the exact failure
`context.py` refuses to make about stale readings, for the same reason.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger("flint.roster")

#: A device that synced within this long is treated as reachable right now.
#: Generous relative to a five-minute sync interval, so one missed exchange on
#: a flaky hotspot does not make her announce a device as gone.
FRESH_SECONDS = 20 * 60

#: Beyond this, "last seen" stops being useful and the device is simply
#: described as away — nobody needs "last seen 43 days ago" in a prompt.
STALE_SECONDS = 7 * 86400


@dataclass(frozen=True)
class Device:
    """One body: what it is, what it can do, when it last checked in."""

    name: str
    body: str = ""                      # "on his phone", "on the Pi he wears"
    can: tuple[str, ...] = ()           # short phrases, spoken not technical
    last_seen: float = 0.0
    #: False for a device that is configured but has never synced. Kept
    #: separate from `last_seen == 0` so a roster entry can be written by hand
    #: without claiming the device has ever been heard from.
    known: bool = True

    def fresh(self, now: float, window: float = FRESH_SECONDS) -> bool:
        return self.last_seen > 0 and (now - self.last_seen) <= window

    def presence(self, now: float) -> str:
        if self.fresh(now):
            return "Reachable now."
        if self.last_seen <= 0:
            return "Not heard from yet."
        gap = now - self.last_seen
        if gap > STALE_SECONDS:
            return "Away."
        return f"Last seen {_ago(gap)}."


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


@dataclass
class Roster:
    """Who she is, and who else she is.

    `me` is excluded from every rendering: the prompt block is about the
    *other* bodies, and listing the one that is speaking reads as confusion
    rather than completeness.
    """

    me: str
    devices: dict[str, Device] = field(default_factory=dict)
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self._load()

    # ── building ────────────────────────────────────────────────────────────
    def add(self, device: Device) -> None:
        if device.name == self.me:
            return
        held = self.devices.get(device.name)
        if held is not None:
            # A hand-written entry describes the device; a check-in only
            # updates when it was last heard from. Merging rather than
            # replacing means presence never overwrites the description.
            device = replace(device, last_seen=max(device.last_seen,
                                                   held.last_seen))
        self.devices[device.name] = device

    def extend(self, devices: Iterable[Device]) -> None:
        for device in devices:
            self.add(device)

    def seen(self, name: str, now: float | None = None) -> None:
        """Record that `name` just synced. Called by the hub on every exchange."""
        if not name or name == self.me:
            return
        now = time.time() if now is None else now
        held = self.devices.get(name)
        if held is None:
            # A device that syncs but was never configured. Worth holding —
            # she should not be blind to a body that is demonstrably there —
            # but with no description, so nothing is claimed about it.
            held = Device(name=name, body=f"another of her devices ({name})")
        self.devices[name] = replace(held, last_seen=now)
        self._save()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("roster: could not read %s — starting empty", self.path)
            return
        for name, entry in (raw.get("devices") or {}).items():
            if not isinstance(entry, dict):
                continue
            self.devices[str(name)] = Device(
                name=str(name), body=str(entry.get("body", "")),
                can=tuple(str(c) for c in (entry.get("can") or ())),
                last_seen=float(entry.get("last_seen", 0) or 0))

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"devices": {
                name: {"body": d.body, "can": list(d.can),
                       "last_seen": d.last_seen}
                for name, d in self.devices.items()}}), encoding="utf-8")
        except OSError:
            log.warning("roster: could not write %s", self.path)

    # ── reading ─────────────────────────────────────────────────────────────
    def others(self) -> list[Device]:
        return sorted((d for d in self.devices.values() if d.name != self.me),
                      key=lambda d: d.name)

    def reachable(self, now: float | None = None) -> list[Device]:
        now = time.time() if now is None else now
        return [d for d in self.others() if d.fresh(now)]

    def find(self, query: str) -> Device | None:
        """A device by name, or by a word from its body description.

        Loose on purpose: the model will say "the phone" far more often than
        it says "carnage", and refusing that would make the delegation tools
        unusable by voice.
        """
        wanted = (query or "").strip().lower()
        if not wanted:
            return None
        for device in self.others():
            if device.name.lower() == wanted:
                return device
        for device in self.others():
            if wanted in device.name.lower() or wanted in device.body.lower():
                return device
        for device in self.others():
            if any(wanted in phrase.lower() for phrase in device.can):
                return device
        return None

    # ── output ──────────────────────────────────────────────────────────────
    def render_for_prompt(self, now: float | None = None) -> str:
        """The block that makes her one assistant instead of three."""
        others = self.others()
        if not others:
            return ""
        now = time.time() if now is None else now
        lines = ["[YOUR OTHER BODIES — you are one person in several places]"]
        for device in others:
            can = "; ".join(device.can)
            detail = f": {can}." if can else "."
            lines.append(f"  - {device.body or device.name} ({device.name})"
                         f"{detail} {device.presence(now)}")
        lines.append(
            "These are you, not other assistants — never refer to them as "
            "separate, never say 'my phone version'. Anything they can do, "
            "you can do; it just happens over there. So NEVER say you can't "
            "do something one of them can. Either do it there, or say plainly "
            "where it has to happen ('main phone se bhej deti hoon'). If the "
            "body that can do it is not reachable, say that specifically "
            "rather than claiming you cannot do it at all.")
        return "\n".join(lines) + "\n"


def build_roster(me: str, entries: Iterable[dict] = (),
                 path: Path | None = None) -> Roster:
    """A roster from plain config dicts: {name, body, can: [...]}."""
    roster = Roster(me=me, path=path)
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        roster.add(Device(
            name=name, body=str(entry.get("body", "")),
            can=tuple(str(c) for c in (entry.get("can") or ()))))
    return roster
