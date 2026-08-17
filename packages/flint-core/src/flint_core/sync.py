"""Keeping two devices' memory in step, without either one losing work.

Venom and FLINT now keep the same kinds of state — memory, an archive,
projects, outcomes. Right now each keeps its own: file something away on the
Pi and the laptop has never heard of it. Sync closes that.

The whole design turns on one distinction, because it decides whether merging
is safe or merely convenient:

    append-only    the archive, the outcome log, the audit trail. Entries are
                   written once and never edited, so two devices can never
                   disagree about one — merging is just a union, and it is
                   correct rather than a compromise.

    keyed          memory facts, project tasks. The same key can be edited on
                   both sides, so merging means choosing, and choosing means
                   something is discarded.

For keyed data this uses last-write-wins, with the device id breaking ties so
both sides always reach the same answer. **That loses the older edit.** For a
single person's two devices, where genuine simultaneous edits of the same key
are rare and usually the same intent anyway, that is the right trade — but it
is a trade, and a `Conflict` is recorded every time it happens so it is
visible rather than silent.

Transport is deliberately not here. Changes are plain dicts; the existing
FLINT websocket carries them, and the engine never learns how.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("flint.sync")

APPEND_ONLY = "append"
KEYED = "keyed"

#: Enough to step below a float timestamp without landing on the previous
#: distinct one. Stores are asked for changes strictly after a mark, so getting
#: the ones *on* the mark back means asking from just under it.
_EPSILON = 1e-6


def _just_before(ts: float) -> float:
    return ts - _EPSILON if ts > 0 else 0.0

#: Changes carried in one exchange. A device that has been off for a month
#: syncs over several rounds rather than one enormous message.
BATCH = 500


@dataclass(frozen=True)
class Change:
    """One thing that happened on one device."""

    store: str
    key: str
    data: dict[str, Any]
    ts: float
    device: str
    deleted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> Change | None:
        try:
            return cls(
                store=str(raw["store"]), key=str(raw["key"]),
                data=dict(raw.get("data") or {}), ts=float(raw["ts"]),
                device=str(raw.get("device", "")),
                deleted=bool(raw.get("deleted", False)))
        except (KeyError, TypeError, ValueError):
            log.warning("sync: dropping malformed change %r", raw)
            return None

    def wins_against(self, other: Change) -> bool:
        """Last write wins; the device id breaks ties.

        The tie-break exists so both devices independently reach the *same*
        answer. Without it, two changes with identical timestamps resolve
        differently on each side and the stores silently diverge — which is
        worse than either outcome of the conflict.
        """
        if self.ts != other.ts:
            return self.ts > other.ts
        return self.device > other.device


@dataclass(frozen=True)
class Conflict:
    """A keyed change that overwrote a different edit. Recorded, not hidden."""

    store: str
    key: str
    kept: str          # device whose version survived
    discarded: str
    ts: float


class Syncable(Protocol):
    """What a store must offer to take part."""

    mode: str          # APPEND_ONLY or KEYED

    def changes_since(self, ts: float) -> list[Change]:
        ...

    def apply_change(self, change: Change) -> bool:
        ...


@dataclass
class SyncResult:
    sent: int = 0
    received: int = 0
    applied: int = 0
    conflicts: list[Conflict] = field(default_factory=list)
    rejected: int = 0

    def summary(self) -> str:
        parts = [f"sent {self.sent}", f"received {self.received}"]
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} conflict(s) resolved")
        if self.rejected:
            parts.append(f"{self.rejected} rejected")
        return ", ".join(parts) + "."


class SyncEngine:
    """Collects local changes, applies remote ones, remembers how far it got."""

    def __init__(self, device: str, stores: dict[str, Syncable],
                 state_path: Path | None = None,
                 clock: Callable[[], float] = time.time):
        if not device.strip():
            raise ValueError("a syncing device needs an id")
        self.device = device.strip()
        self._stores = dict(stores)
        self._state_path = Path(state_path) if state_path else None
        self._clock = clock
        # Two separate positions per peer, and they must stay separate:
        #   sent      the newest local change this peer has been given
        #   received  the newest change this peer has given us
        # Collapsing them into one number looks harmless and is not — sending
        # our own changes then moves the mark used to decide what to ask for,
        # so the peer's changes at the same instant are silently skipped and
        # the reverse direction quietly stops working.
        self._sent: dict[str, float] = {}
        self._received: dict[str, float] = {}
        # A timestamp alone cannot be a complete position, because timestamps
        # tie. Two facts saved in the same instant — a contacts import, a
        # batch of outcomes, anything that writes in a loop — share a ts, and
        # a mark of "everything up to T" then cannot distinguish the one that
        # was sent from the one that was not. Sending with `>` drops the
        # second one permanently; sending with `>=` re-sends the first one
        # forever and the exchange never terminates.
        #
        # So the position is a timestamp *and* the ids already acknowledged at
        # exactly that timestamp. That set is almost always a single entry, and
        # it is what makes the boundary exact instead of approximate.
        self._sent_ids: dict[str, set[str]] = {}
        self._load()

    @staticmethod
    def _identify(change: Change) -> str:
        return f"{change.store}/{change.key}"

    # ── how far we've got with each peer ────────────────────────────────────
    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        self._sent = {str(k): float(v)
                      for k, v in dict(data.get("sent", {})).items()}
        self._received = {str(k): float(v)
                          for k, v in dict(data.get("received", {})).items()}
        self._sent_ids = {str(k): {str(i) for i in v}
                          for k, v in dict(data.get("sent_ids", {})).items()}

    def _save(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"sent": self._sent, "received": self._received,
                            "sent_ids": {k: sorted(v)
                                         for k, v in self._sent_ids.items()}}),
                encoding="utf-8")
        except OSError:
            log.warning("sync: could not record sync position")

    def watermark(self, peer: str) -> float:
        """The newest local change `peer` has already been given."""
        return self._sent.get(peer, 0.0)

    def received_upto(self, peer: str) -> float:
        return self._received.get(peer, 0.0)

    # ── outgoing ────────────────────────────────────────────────────────────
    def changes_for(self, peer: str, limit: int = BATCH) -> list[dict]:
        """What this device has that `peer` has not seen, oldest first."""
        since = self.watermark(peer)
        at_mark = self._sent_ids.get(peer, set())
        collected: list[Change] = []
        for name, store in self._stores.items():
            try:
                # Asked for everything from *just before* the mark, so changes
                # sitting exactly on it are offered rather than assumed sent;
                # the id set below is what decides which of those still need to
                # go. Without this a tied timestamp is silently dropped.
                collected.extend(store.changes_since(_just_before(since)))
            except Exception as exc:        # noqa: BLE001 — one bad store
                log.warning("sync: %s could not list changes: %s", name, exc)
        ready = [
            change for change in collected
            if change.ts > since
            or (change.ts == since and self._identify(change) not in at_mark)
        ]
        # Never hand a peer its own work back. It authored these, it has them,
        # and with a hub relaying between three devices the echo would
        # otherwise be paid for on every single exchange.
        ready = [change for change in ready if change.device != peer]
        ready.sort(key=lambda c: c.ts)
        return [c.to_dict() for c in ready[:limit]]

    # ── incoming ────────────────────────────────────────────────────────────
    def apply(self, raw_changes: Sequence[dict], peer: str = "") -> SyncResult:
        """Merge a peer's changes. Never raises on bad input."""
        result = SyncResult(received=len(raw_changes))
        highest = self.received_upto(peer) if peer else 0.0

        for raw in raw_changes:
            change = Change.from_dict(raw)
            if change is None:
                result.rejected += 1
                continue
            if change.device == self.device:
                # Our own change coming back round. Applying it is harmless
                # but pointless, and it would move the watermark on data we
                # already had.
                continue
            store = self._stores.get(change.store)
            if store is None:
                # A store this device doesn't have — a Pi with no projects
                # file receiving project changes. Not an error; just not ours.
                result.rejected += 1
                continue
            try:
                existing = getattr(store, "current", None)
                if store.mode == KEYED and existing is not None:
                    held = existing(change.key)
                    if held is not None and not change.wins_against(held):
                        result.conflicts.append(Conflict(
                            store=change.store, key=change.key,
                            kept=held.device, discarded=change.device,
                            ts=self._clock()))
                        continue
                    if held is not None and held.device != change.device:
                        result.conflicts.append(Conflict(
                            store=change.store, key=change.key,
                            kept=change.device, discarded=held.device,
                            ts=self._clock()))
                if store.apply_change(change):
                    result.applied += 1
            except Exception as exc:        # noqa: BLE001
                log.warning("sync: could not apply %s/%s: %s",
                            change.store, change.key, exc)
                result.rejected += 1
                continue
            highest = max(highest, change.ts)

        if peer:
            self._received[peer] = highest
            self._save()
        return result

    def note_sent(self, peer: str, changes: Sequence[dict]) -> None:
        """Record that `peer` has now seen everything up to these changes."""
        if not peer or not changes:
            return
        mark = max(float(c.get("ts", 0)) for c in changes)
        self.note_sent_upto(peer, mark, [
            f"{c.get('store', '')}/{c.get('key', '')}"
            for c in changes if float(c.get("ts", 0)) == mark
        ])

    def note_sent_upto(self, peer: str, ts: float,
                       ids: Iterable[str] = ()) -> None:
        """Record a watermark the peer reported reaching, rather than one we
        inferred from what we handed over.

        Over a direct call those are the same number. Over a network they are
        not: handing bytes to a socket is not the same as a peer applying them,
        and advancing on the former loses anything dropped in between —
        silently, since the changes are simply never offered again.

        `ids` are the changes acknowledged at exactly `ts`. They come from the
        peer rather than being recomputed here, because a batch can be cut off
        by the size limit part-way through a group of tied timestamps: only the
        peer knows which of them it actually got.
        """
        held = self._sent.get(peer, 0.0)
        if not peer or ts < held:
            return
        if ts == held:
            # Same instant, more ids acknowledged. Widen rather than replace.
            self._sent_ids.setdefault(peer, set()).update(str(i) for i in ids)
        else:
            self._sent[peer] = ts
            self._sent_ids[peer] = {str(i) for i in ids}
        self._save()


# ── adapters for the stores that exist ───────────────────────────────────────
class ArchiveSync:
    """The recall archive. Append-only, so merging is a union and cannot lose."""

    mode = APPEND_ONLY

    def __init__(self, archive, device: str):
        self._archive = archive
        self._device = device

    def changes_since(self, ts: float) -> list[Change]:
        rows = [r for r in self._archive._rows() if float(r["ts"]) > ts]
        return [Change(store="archive", key=f"{self._device}:{r['id']}",
                       data={"text": r["text"], "kind": r["kind"],
                             "subject": r["subject"]},
                       ts=float(r["ts"]), device=self._device)
                for r in rows]

    def apply_change(self, change: Change) -> bool:
        # Deduped by content and timestamp: the same entry arriving twice
        # (two sync rounds, a retried message) must not become two memories.
        text = str(change.data.get("text", ""))
        for existing in self._archive.search(text, limit=5):
            if existing.text == text and abs(existing.ts - change.ts) < 1.0:
                return False
        return self._archive.remember(
            text, kind=str(change.data.get("kind", "fact")),
            subject=str(change.data.get("subject", "")), ts=change.ts) is not None


class ProjectSync:
    """Tasks and projects. Keyed, so edits on both sides have to be resolved."""

    mode = KEYED

    def __init__(self, projects, device: str, clock=time.time):
        self._projects = projects
        self._device = device
        self._clock = clock

    def changes_since(self, ts: float) -> list[Change]:
        data = self._projects._load()
        changes = []
        for task_id, task in data.get("tasks", {}).items():
            touched = float(task.get("done_at") or task.get("created", 0))
            if touched > ts:
                changes.append(Change(
                    store="projects", key=task_id, data=dict(task), ts=touched,
                    # Attributed to wherever it was first written, not to
                    # whoever is relaying it. Claiming a relayed change as our
                    # own makes the originating device see it as a foreign
                    # edit of its own task — a conflict that never happened,
                    # and an echo that bounces back and forth forever.
                    device=str(task.get("device") or self._device)))
        return changes

    def current(self, key: str) -> Change | None:
        task = self._projects._load().get("tasks", {}).get(key)
        if task is None:
            return None
        touched = float(task.get("done_at") or task.get("created", 0))
        return Change(store="projects", key=key, data=dict(task), ts=touched,
                      device=str(task.get("device", "")))

    def apply_change(self, change: Change) -> bool:
        with self._projects._lock:
            data = self._projects._load()
            record = dict(change.data)
            record["id"] = change.key
            record["device"] = change.device
            data.setdefault("tasks", {})[change.key] = record
            self._projects._save(data)
        return True


class MemorySync:
    """The hot tier: the handful of facts that ride in every prompt.

    Keyed, and the one store where losing the resolution actually hurts. The
    archive can hold two versions of an episode and the worse one is merely
    never returned; here, whichever version wins is spoken as fact until
    someone corrects it. So this leans on the epoch stamp `MemoryStore` writes
    rather than the human `updated` date — see the note there for why a date is
    not good enough.

    Deletions are not carried. A fact removed on the phone stays on the Pi
    until it is removed there too, because the alternative is worse: the store
    trims itself when it outgrows its budget, and a trim is indistinguishable
    from a delete once it has happened. Syncing deletions would let one
    device's trimming quietly erase facts everywhere.
    """

    mode = KEYED

    def __init__(self, memory, device: str):
        self._memory = memory
        self._device = device

    @staticmethod
    def _stamp(entry: dict) -> float:
        """Epoch seconds for an entry, however old its format is."""
        raw = entry.get("t")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        # Written before `t` existed: fall back to midnight on its date, which
        # is the most this format can honestly claim to know.
        try:
            return time.mktime(time.strptime(str(entry.get("updated", "")),
                                             "%Y-%m-%d"))
        except (ValueError, OverflowError):
            return 0.0

    def _entries(self):
        for category, items in self._memory.load().items():
            if not isinstance(items, dict):
                continue
            for key, entry in items.items():
                if isinstance(entry, dict) and "value" in entry:
                    yield category, key, entry

    def changes_since(self, ts: float) -> list[Change]:
        out = []
        for category, key, entry in self._entries():
            stamp = self._stamp(entry)
            if stamp > ts:
                out.append(Change(
                    store="memory", key=f"{category}/{key}",
                    data={"value": entry.get("value", ""),
                          "updated": entry.get("updated", "")},
                    ts=stamp,
                    device=str(entry.get("device") or self._device)))
        return out

    def current(self, key: str) -> Change | None:
        category, _, name = key.partition("/")
        entry = self._memory.load().get(category, {}).get(name)
        if not isinstance(entry, dict):
            return None
        return Change(store="memory", key=key,
                      data={"value": entry.get("value", "")},
                      ts=self._stamp(entry),
                      device=str(entry.get("device", "")))

    def apply_change(self, change: Change) -> bool:
        category, _, name = change.key.partition("/")
        if not category or not name:
            return False
        value = str(change.data.get("value", ""))
        if not value:
            return False
        with self._memory._lock:
            data = self._memory.load()
            if category not in data:
                # An unknown category from a newer device. Better filed under
                # notes than dropped: the fact is still true.
                category = "notes"
            data.setdefault(category, {})[name] = {
                "value": value,
                "updated": str(change.data.get("updated", "")) or
                           time.strftime("%Y-%m-%d", time.localtime(change.ts)),
                "t": int(change.ts),
                # Kept so the originating device recognises its own edit coming
                # back rather than treating it as a foreign one.
                "device": change.device,
            }
            self._memory._save(data)
        return True


class OutcomeSync:
    """Decisions and outcomes. Append-only — every row is written once.

    Worth syncing precisely because it is the thinnest evidence base she has:
    `preferences()` stays silent below a minimum row count, so three devices
    each learning separately may never any of them cross the threshold, while
    the union of them clears it easily.
    """

    mode = APPEND_ONLY

    def __init__(self, outcomes, device: str):
        self._outcomes = outcomes
        self._device = device
        # (device, ts) of every row already held. Built once and maintained,
        # because the naive check re-reads and re-parses the whole log for
        # every incoming row — a 500-change batch against a few thousand rows
        # is millions of parses, and a sync is supposed to be cheap enough to
        # run on a phone on battery.
        self._seen: set[tuple[str, float]] | None = None

    def _index(self) -> set[tuple[str, float]]:
        if self._seen is None:
            self._seen = {
                (str(r.get("device", "")), round(float(r.get("ts", 0) or 0), 6))
                for r in self._outcomes.rows()
            }
        return self._seen

    def changes_since(self, ts: float) -> list[Change]:
        out = []
        for row in self._outcomes.rows():
            stamp = float(row.get("ts", 0) or 0)
            if stamp <= ts:
                continue
            origin = str(row.get("device") or self._device)
            out.append(Change(
                store="outcomes",
                # Content-addressed so the same row relayed by two peers is
                # recognised as one row rather than appended twice.
                key=f"{origin}:{stamp:.6f}:{row.get('kind', '')}",
                data=dict(row), ts=stamp, device=origin))
        return out

    def apply_change(self, change: Change) -> bool:
        row = dict(change.data)
        row["device"] = change.device
        stamp = round(float(row.get("ts", change.ts) or change.ts), 6)
        row["ts"] = stamp
        fingerprint = (change.device, stamp)
        index = self._index()
        if fingerprint in index:
            return False
        self._outcomes._append(row)
        index.add(fingerprint)
        return True


class NoteSync:
    """Voice notes. Append-only — a note is written once and never edited."""

    mode = APPEND_ONLY

    def __init__(self, notes, device: str):
        self._notes = notes
        self._device = device

    def changes_since(self, ts: float) -> list[Change]:
        out = []
        for entry in self._notes.all():
            stamp = float(entry.get("ts", 0) or 0)
            if stamp > ts:
                origin = str(entry.get("device") or self._device)
                out.append(Change(store="notes", key=f"{origin}:{stamp:.6f}",
                                  data=dict(entry), ts=stamp, device=origin))
        return out

    def apply_change(self, change: Change) -> bool:
        text = str(change.data.get("text", ""))
        stamp = float(change.data.get("ts", change.ts) or change.ts)
        for existing in self._notes.all():
            if (existing.get("text") == text
                    and abs(float(existing.get("ts", 0) or 0) - stamp) < 1e-6):
                return False
        with self._notes._lock:
            data = self._notes._load([])
            data.append({"text": text, "ts": stamp, "device": change.device})
            self._notes._save(data)
        return True


class ConnectionSync:
    """People. Keyed by the store's own normalised name.

    Every field on a person merges rather than replaces, which makes this the
    one keyed store where last-write-wins is genuinely lossy in a way worth
    naming: learning someone's Instagram on the phone while learning their
    interests on the Pi means one of those two records wins whole. The merge
    the store does internally is not reproduced across devices, because doing
    it correctly needs per-field stamps and this does not have them.
    """

    mode = KEYED

    def __init__(self, connections, device: str):
        self._connections = connections
        self._device = device

    def changes_since(self, ts: float) -> list[Change]:
        out = []
        for key, record in self._connections._load({}).items():
            stamp = float(record.get("ts", 0) or 0)
            if stamp > ts:
                out.append(Change(
                    store="connections", key=str(key), data=dict(record),
                    ts=stamp,
                    device=str(record.get("device") or self._device)))
        return out

    def current(self, key: str) -> Change | None:
        record = self._connections._load({}).get(key)
        if record is None:
            return None
        return Change(store="connections", key=key, data=dict(record),
                      ts=float(record.get("ts", 0) or 0),
                      device=str(record.get("device", "")))

    def apply_change(self, change: Change) -> bool:
        with self._connections._lock:
            data = self._connections._load({})
            record = dict(change.data)
            record["device"] = change.device
            data[change.key] = record
            self._connections._save(data)
        return True


def build_engine(device: str, *, archive=None, projects=None, memory=None,
                 outcomes=None, notes=None, connections=None,
                 state_path: Path | None = None) -> SyncEngine:
    """A sync engine for whichever stores this device actually has.

    Every argument is optional and absent ones are simply not synced — a body
    with no project file takes part in everything else without pretending to
    have one, and `apply` rejects changes for stores it does not hold.
    """
    stores: dict[str, Syncable] = {}
    if archive is not None:
        stores["archive"] = ArchiveSync(archive, device)
    if projects is not None:
        stores["projects"] = ProjectSync(projects, device)
    if memory is not None:
        stores["memory"] = MemorySync(memory, device)
    if outcomes is not None:
        stores["outcomes"] = OutcomeSync(outcomes, device)
    if notes is not None:
        stores["notes"] = NoteSync(notes, device)
    if connections is not None:
        stores["connections"] = ConnectionSync(connections, device)
    # Two stores are deliberately absent, and both for the same reason — the
    # data does not carry enough to merge honestly:
    #
    #   lists      items are bare strings with no timestamps, so "milk added
    #              on the phone, bread added on the Pi" cannot be merged; one
    #              device's whole list would win and the other's additions
    #              would vanish silently.
    #   reminders  firing deletes the entry. Syncing them as they stand means
    #              every device holds its own copy and every device chimes —
    #              and once one has fired, its deletion is indistinguishable
    #              from a reminder that was never there. Doing this properly
    #              needs a fired-state that propagates, not a delete.
    return SyncEngine(device, stores, state_path=state_path)


def merge_rounds(left: SyncEngine, right: SyncEngine,
                 rounds: int = 2) -> tuple[SyncResult, SyncResult]:
    """Exchange changes both ways. Useful for tests and for a direct link.

    Two rounds by default: one carries each side's backlog, the second
    settles anything the first round's applications produced.
    """
    left_result = SyncResult()
    right_result = SyncResult()
    for _ in range(max(1, rounds)):
        outgoing = left.changes_for(right.device)
        applied = right.apply(outgoing, peer=left.device)
        left.note_sent(right.device, outgoing)
        right_result.received += applied.received
        right_result.applied += applied.applied
        right_result.conflicts.extend(applied.conflicts)
        left_result.sent += len(outgoing)

        back = right.changes_for(left.device)
        applied_back = left.apply(back, peer=right.device)
        right.note_sent(left.device, back)
        left_result.received += applied_back.received
        left_result.applied += applied_back.applied
        left_result.conflicts.extend(applied_back.conflicts)
        right_result.sent += len(back)
    return left_result, right_result
