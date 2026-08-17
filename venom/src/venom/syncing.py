"""Keeping this Pi's memory in step with the phone.

Venom is a leaf: it reaches out to the hub and never listens for anything. On
a wearable that is not a simplification, it is the only shape that works —
this device has no stable address, spends its life moving between a home
network and a phone hotspot, and is frequently asleep. Something that can be
*reached* has to be the hub, and that is Carnage.

The loop is deliberately dull. Every few minutes it runs one exchange and logs
what moved. Everything that could make it interesting — retries, backoff,
partial delivery — is already handled a layer down by `flint_core.syncnet`,
which re-sends anything the far side did not acknowledge. So a failure here
needs no recovery logic at all: the next tick is the retry, and the data is
still queued because the watermark never moved.

The one rule that matters: **a sync failure must never touch the
conversation.** No network this device carries is reliable, and an assistant
that goes quiet because a hotspot dropped is worse than one that is briefly
out of step.
"""

from __future__ import annotations

import asyncio
import logging

from flint_core.sync import build_engine
from flint_core.syncnet import SyncRefused

log = logging.getLogger("venom.sync")

#: After this many consecutive failures, stop logging each one at warning.
#: A Pi left in a drawer should not fill the journal with the same line.
QUIET_AFTER = 3


class SyncLoop:
    """One exchange with the hub, every `interval_seconds`."""

    def __init__(self, config, *, memory=None, archive=None, projects=None,
                 outcomes=None, notes=None, connections=None, state_dir=None,
                 exchange=None, relay=None, roster=None, carry=None):
        self._config = config
        self._failures = 0
        self._exchange = exchange or _default_exchange
        self._relay = relay
        self._roster = roster
        # What to do with work another body has asked this one to carry out.
        # A callable rather than a tool dispatch, because the far side asked
        # *her* — this body answers with its own judgement and its own tools.
        self._carry = carry
        self.engine = build_engine(
            config.device,
            memory=memory, archive=archive, projects=projects,
            outcomes=outcomes, notes=notes, connections=connections,
            state_path=(state_dir / "sync.json") if state_dir else None)

    async def run(self) -> None:
        log.info("sync: %s → %s every %.0fs", self._config.device,
                 self._config.hub, self._config.interval_seconds)
        while True:
            await asyncio.sleep(self._config.interval_seconds)
            await self.tick()

    async def tick(self) -> bool:
        """One exchange. True when it worked. Never raises."""
        try:
            # Off the event loop: this opens a socket and writes to disk, and
            # the voice loop is on the same loop.
            result = await asyncio.to_thread(
                self._exchange, self.engine, self._config.hub,
                self._config.token, self._relay)
        except SyncRefused as exc:
            # A refusal is a configuration problem, not a network one — it
            # will not fix itself, so it is always worth saying.
            log.warning("sync refused by the hub: %s", exc)
            self._failures += 1
            return False
        except Exception as exc:            # noqa: BLE001
            self._failures += 1
            if self._failures <= QUIET_AFTER:
                log.warning("sync failed (%s) — retrying in %.0fs",
                            exc, self._config.interval_seconds)
            elif self._failures % 20 == 0:
                log.warning("sync still failing after %d attempts: %s",
                            self._failures, exc)
            return False

        if self._failures:
            log.info("sync recovered after %d failed attempt(s)", self._failures)
            self._failures = 0
        if self._roster is not None and result.peer:
            # A successful exchange is the only honest evidence that the other
            # device is actually there.
            self._roster.seen(result.peer)
        self._carry_out_requests()
        if result.pushed or result.applied:
            log.info("sync: %s", result.summary())
        for conflict in result.conflicts:
            # Visible rather than silent: something the other device wrote won,
            # and this device's version of that fact is gone.
            log.info("sync: %s/%s — kept %s, discarded %s",
                     conflict.store, conflict.key, conflict.kept,
                     conflict.discarded)
        return True


    def _carry_out_requests(self) -> None:
        """Do whatever the other bodies asked of this one.

        Answers are posted back on the next exchange rather than immediately —
        one more round trip would double the cost of every sync for something
        nobody is waiting on synchronously.
        """
        if self._relay is None or self._carry is None:
            return
        from flint_core.relay import carry_out

        handled = carry_out(self._relay, self._config.device, self._carry)
        for request in handled:
            log.info("relay: did '%s' for %s — %s", request.text[:60],
                     request.sender, request.status)


def _default_exchange(engine, hub: str, token: str, relay=None):
    """Import the websocket client only when a sync is actually attempted.

    Keeps `websockets` off the import path of a device that never syncs, which
    matters on a 2 GB Pi where the voice loop's imports are already the
    slowest part of coming up.
    """
    from flint_core.syncws import sync_with_hub

    return sync_with_hub(engine, hub, token=token, relay=relay)
