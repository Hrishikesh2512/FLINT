"""Getting changes between devices, once there are more than two of them.

`sync.py` deliberately stops at plain dicts, and that was right: merging is a
correctness problem and moving bytes is a plumbing problem, and mixing them
makes the first one untestable. This is the plumbing — still with no sockets in
it, because the same separation applies one level down.

Two devices could exchange changes directly; three cannot, and not for the
reason you would guess. Merging three peers is no harder than two. *Reaching*
them is: the Pi hops onto a phone hotspot and changes subnet, the laptop is
asleep most of the day, and a phone is behind carrier NAT with no stable
address at all. A full mesh needs every pair to be reachable at the same
moment, which for these three devices is rarely true and never guaranteed.

So one device is the hub, and it should be the phone: the only one that is
always powered, always networked, and always carried. The others are leaves
that talk to it and never to each other. A leaf that has been off for a week
catches up in one conversation with the hub, and neither leaf ever needs to
know whether the other is even switched on.

    venom  ─┐
            ├─▶  carnage (hub)
    flint  ─┘

**What this layer adds over `merge_rounds` is the acknowledgement.** In
process, handing over a list cannot fail. Over a network it can fail halfway,
and the difference matters because of what `note_sent` does: it advances the
watermark that decides what gets sent next time. Advance it for changes that
never arrived and they are never offered again — the data is not resent, it is
gone, and nothing anywhere reports an error. So the sender here only advances
its watermark when the receiver says what it applied, and a dropped connection
costs a repeated batch rather than a silent hole.

Delivery is therefore at-least-once, which is exactly what the merge layer was
built to absorb: append-only stores dedupe, and keyed stores re-resolve to the
same answer. Re-sending is cheap; losing is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from flint_core.sync import BATCH, Conflict, SyncEngine, SyncResult

log = logging.getLogger("flint.syncnet")

#: Bumped only for a change that an older peer could not understand. Both
#: sides check it, because a leaf and a hub are updated at different times and
#: the failure of a silent mismatch is corrupt state rather than an error.
PROTOCOL = 1

HELLO = "sync_hello"
READY = "sync_ready"
PUSH = "sync_push"
ACK = "sync_ack"
PULL = "sync_pull"
CHANGES = "sync_changes"
ERROR = "sync_error"


class SyncRefused(Exception):
    """The hub declined: bad token, wrong protocol, no device id."""


def asdict_request(request) -> dict:
    """A relayed request as plain data. Defined here so `syncnet` never has to
    import `relay` — the transport carries dicts and knows nothing else."""
    from dataclasses import asdict

    return asdict(request)


def _boundary(changes: list[dict]) -> tuple[float, list[str]]:
    """The newest timestamp in a batch, and the ids sitting exactly on it.

    Both halves are the acknowledgement. The timestamp alone would leave the
    sender unable to tell a tied change it already sent from one it never did —
    see the note on `SyncEngine._sent_ids`.
    """
    if not changes:
        return 0.0, []
    newest = max(float(c.get("ts", 0) or 0) for c in changes)
    return newest, [
        f"{c.get('store', '')}/{c.get('key', '')}"
        for c in changes if float(c.get("ts", 0) or 0) == newest
    ]


def _error(reason: str) -> dict:
    return {"type": ERROR, "reason": reason}


@dataclass
class Exchange:
    """What one full conversation with a peer moved, in both directions."""

    peer: str = ""
    pushed: int = 0
    pulled: int = 0
    applied: int = 0
    rounds: int = 0
    #: Relayed requests and answers that moved — work handed between bodies,
    #: counted separately because it is a different kind of event to a memory
    #: change and reads wrong folded into the same number.
    relayed: int = 0
    conflicts: list[Conflict] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.peer or 'peer'}: sent {self.pushed}",
                 f"received {self.pulled}"]
        if self.relayed:
            parts.append(f"{self.relayed} relayed")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} conflict(s)")
        return ", ".join(parts) + "."


class SyncHub:
    """The always-on side. Holds its own engine and serves any number of leaves.

    Stateless between messages by design — every request carries the device it
    came from, so a leaf that reconnects mid-exchange simply continues. The
    only state that matters lives in the engine's watermarks, which are on
    disk.
    """

    def __init__(self, engine: SyncEngine, token: str = "",
                 on_exchange: Callable[[str, SyncResult], None] | None = None,
                 relay=None, roster=None):
        self._engine = engine
        self._token = token or ""
        self._on_exchange = on_exchange
        # Relayed requests and presence ride the sync connection rather than
        # opening a second one. See flint_core.relay for why that is the whole
        # design and not a shortcut.
        self._relay = relay
        self._roster = roster

    @property
    def device(self) -> str:
        return self._engine.device

    def _authorised(self, message: dict) -> str:
        """The peer's device id, or raises. Every handler starts here."""
        if self._token and str(message.get("token", "")) != self._token:
            raise SyncRefused("bad token")
        peer = str(message.get("device", "")).strip()
        if not peer:
            raise SyncRefused("no device id")
        if peer == self.device:
            # A device syncing with itself would apply its own changes back
            # over newer ones and move both watermarks. Almost always a
            # misconfigured device id copied between two installs.
            raise SyncRefused("peer and hub share a device id")
        return peer

    def handle(self, message: dict) -> dict:
        """One request in, one response out. Never raises."""
        try:
            kind = str(message.get("type", ""))
            if kind == HELLO:
                return self._hello(message)
            if kind == PUSH:
                return self._push(message)
            if kind == PULL:
                return self._pull(message)
            return _error(f"unknown message {kind!r}")
        except SyncRefused as exc:
            log.warning("sync: refused a peer — %s", exc)
            return _error(str(exc))
        except Exception:                       # noqa: BLE001
            # A hub that dies on one malformed message takes every device's
            # memory offline with it.
            log.exception("sync: hub failed to handle a message")
            return _error("hub error")

    def _hello(self, message: dict) -> dict:
        peer = self._authorised(message)
        if int(message.get("protocol", 0)) != PROTOCOL:
            raise SyncRefused(f"protocol {message.get('protocol')} "
                              f"!= {PROTOCOL}")
        log.info("sync: %s connected", peer)
        if self._roster is not None:
            # Presence, established by the only evidence that actually means
            # a device is alive: it just talked to us.
            self._roster.seen(peer)
        return {"type": READY, "device": self.device, "protocol": PROTOCOL}

    def _push(self, message: dict) -> dict:
        peer = self._authorised(message)
        changes = message.get("changes") or []
        if not isinstance(changes, list):
            # A string here would otherwise be iterated character by character
            # and reported as a batch of malformed changes, which reads like
            # data loss in the log when it is really a broken sender.
            raise SyncRefused("changes must be a list")
        if self._relay is not None:
            relayed = message.get("relay")
            if isinstance(relayed, list) and relayed:
                self._relay.merge(relayed)
        result = self._engine.apply(changes, peer=peer)
        if self._on_exchange is not None:
            try:
                self._on_exchange(peer, result)
            except Exception:                   # noqa: BLE001
                log.exception("sync: exchange callback failed")
        return {"type": ACK, "device": self.device,
                "received": result.received, "applied": result.applied,
                "rejected": result.rejected,
                "conflicts": [c.__dict__ for c in result.conflicts]}

    def _pull(self, message: dict) -> dict:
        peer = self._authorised(message)
        # The acknowledgement for whatever we sent last time rides on this
        # request. Only now does the watermark move — see the module docstring.
        acked = float(message.get("ack_upto", 0) or 0)
        if acked > 0:
            self._engine.note_sent_upto(peer, acked,
                                        list(message.get("ack_ids") or []))
        limit = min(int(message.get("limit", BATCH) or BATCH), BATCH)
        changes = self._engine.changes_for(peer, limit=limit)
        reply = {"type": CHANGES, "device": self.device, "changes": changes,
                 "more": len(changes) >= limit}
        if self._relay is not None:
            # Everything this peer should carry out, plus answers to what it
            # asked for. Small, and it saves the leaf a second round trip.
            reply["relay"] = (
                [asdict_request(r) for r in self._relay.waiting_for(peer)]
                + [asdict_request(r) for r in self._relay.answers_for(peer)])
        return reply


class SyncLeaf:
    """The device that reaches out. Runs one full exchange against a callable.

    `call` takes a request dict and returns the hub's response dict. Anything
    that can do that works — a websocket, HTTP, a queue, or the hub object
    itself in a test — which is how this file stays free of sockets.
    """

    def __init__(self, engine: SyncEngine, token: str = "",
                 max_rounds: int = 10, relay=None):
        self._engine = engine
        self._token = token or ""
        self._max_rounds = max(1, max_rounds)
        self._relay = relay

    @property
    def device(self) -> str:
        return self._engine.device

    def _envelope(self, kind: str, **extra: Any) -> dict:
        message = {"type": kind, "device": self.device, **extra}
        if self._token:
            message["token"] = self._token
        return message

    @staticmethod
    def _check(response: Any) -> dict:
        if not isinstance(response, dict):
            raise SyncRefused("hub sent a non-message")
        if response.get("type") == ERROR:
            raise SyncRefused(str(response.get("reason", "refused")))
        return response

    def exchange(self, call: Callable[[dict], Any]) -> Exchange:
        """Push everything we have, pull everything they have. Raises on refusal."""
        greeting = self._check(call(self._envelope(HELLO, protocol=PROTOCOL)))
        peer = str(greeting.get("device", "")).strip()
        if not peer:
            raise SyncRefused("hub did not name itself")
        if int(greeting.get("protocol", 0)) != PROTOCOL:
            raise SyncRefused(f"hub speaks protocol {greeting.get('protocol')}")

        exchange = Exchange(peer=peer)
        self._push_all(call, peer, exchange)
        self._pull_all(call, peer, exchange)
        return exchange

    def _push_all(self, call, peer: str, exchange: Exchange) -> None:
        # Anything this device wants relayed goes up with the first push, and
        # on its own if there is nothing else to send — a queued request must
        # not sit here waiting for an unrelated memory change to travel with.
        relayed = self._relay.all_dicts() if self._relay is not None else []
        for _ in range(self._max_rounds):
            outgoing = self._engine.changes_for(peer)
            if not outgoing and not relayed:
                return
            self._check(call(self._envelope(PUSH, changes=outgoing,
                                            relay=relayed)))
            relayed = []
            if not outgoing:
                return
            # Only now. If the call above had raised, these changes stay
            # unacknowledged and are offered again next time.
            self._engine.note_sent(peer, outgoing)
            exchange.pushed += len(outgoing)
            exchange.rounds += 1
            if len(outgoing) < BATCH:
                return

    def _pull_all(self, call, peer: str, exchange: Exchange) -> None:
        """Pull until a batch comes back empty.

        The loop deliberately does not stop on `more: false`. Stopping there
        would end the exchange holding an acknowledgement the hub never
        receives, so the hub would re-send that final batch on every future
        sync, forever. One more round-trip returns nothing and carries the ack
        that closes it out.
        """
        acked = 0.0
        acked_ids: list[str] = []
        for _ in range(self._max_rounds):
            response = self._check(
                call(self._envelope(PULL, limit=BATCH, ack_upto=acked,
                                    ack_ids=acked_ids)))
            if self._relay is not None:
                # Work for this device, and answers to what it asked for.
                # Absorbed before the early return below, or a relay-only
                # exchange (no memory changes at all) would drop them.
                relayed = response.get("relay")
                if isinstance(relayed, list) and relayed:
                    exchange.relayed += self._relay.merge(relayed)
            changes = list(response.get("changes") or [])
            if not changes:
                return
            result = self._engine.apply(changes, peer=peer)
            exchange.pulled += result.received
            exchange.applied += result.applied
            exchange.conflicts.extend(result.conflicts)
            exchange.rounds += 1
            acked, acked_ids = _boundary(changes)
        log.warning("sync: stopped after %d rounds with more to pull from %s — "
                    "will continue next time", self._max_rounds, peer)


def hub_and_leaf(hub: SyncHub) -> Callable[[dict], dict]:
    """A `call` that talks to a hub object directly — for tests and one box."""
    return hub.handle
