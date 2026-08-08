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
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("flint.sync")

APPEND_ONLY = "append"
KEYED = "keyed"

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
        self._load()

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

    def _save(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"sent": self._sent, "received": self._received}),
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
        collected: list[Change] = []
        for name, store in self._stores.items():
            try:
                collected.extend(store.changes_since(since))
            except Exception as exc:        # noqa: BLE001 — one bad store
                log.warning("sync: %s could not list changes: %s", name, exc)
        collected.sort(key=lambda c: c.ts)
        return [c.to_dict() for c in collected[:limit]]

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
        self._sent[peer] = max(self._sent.get(peer, 0.0),
                               max(float(c.get("ts", 0)) for c in changes))
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


def build_engine(device: str, *, archive=None, projects=None,
                 state_path: Path | None = None) -> SyncEngine:
    """A sync engine for whichever stores this device actually has."""
    stores: dict[str, Syncable] = {}
    if archive is not None:
        stores["archive"] = ArchiveSync(archive, device)
    if projects is not None:
        stores["projects"] = ProjectSync(projects, device)
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
