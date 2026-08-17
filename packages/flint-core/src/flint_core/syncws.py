"""Websockets under the sync protocol — the server, and the client leaves use.

`syncnet.py` holds the protocol and deliberately owns no sockets, so that
merging can be tested without a network. This is the layer that does own them.

Both ends live in one file on purpose: they have to agree about framing, size
limits and error shape, and the cheapest way to keep two things agreeing is to
make them impossible to change separately. It sits in the shared core rather
than in the phone package because *which* device is the hub is a deployment
decision — today the phone, because it is the only one reliably reachable —
and a leaf must never have to depend on the hub's package to call home.

`websockets` is imported lazily, inside the functions that need it. A device
that never syncs never pays for the import, which matters on a 2 GB Pi where
start-up imports are already the slowest part of coming up.

Two things the network adds that the protocol layer does not deal with:

  * **A peer allowlist.** The token proves someone knows the secret; the
    allowlist says which device ids may use it. It exists because the failure
    it prevents is silent — a second install with a copied config does not
    error, it merges its state into yours.

  * **One exchange at a time.** The engine's watermarks are read, compared and
    written across several messages, so two peers interleaving would let one
    peer's ack advance a mark the other peer's changes were measured against.
    Syncs take milliseconds and happen a few times an hour; a lock costs
    nothing and removes a whole class of bug that would only ever show up as
    missing data weeks later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable

from flint_core.sync import SyncEngine, SyncResult
from flint_core.syncnet import SyncHub, SyncLeaf, SyncRefused

log = logging.getLogger("flint.syncws")

#: A sync message is small. Anything this large is not one.
MAX_MESSAGE = 4 * 1024 * 1024


class SyncServer:
    """A websocket front door for a `SyncHub`.

    Additive, like everything else in this codebase: if `websockets` is not
    installed or the port is taken, this logs and stays down, and the
    assistant carries on without device sync rather than failing to start.
    """

    def __init__(self, engine: SyncEngine, host: str = "0.0.0.0",  # noqa: S104
                 port: int = 8790, token: str = "",
                 peers: tuple[str, ...] = (),
                 on_exchange: Callable[[str, SyncResult], None] | None = None,
                 relay=None, roster=None):
        self._hub = SyncHub(engine, token=token, on_exchange=on_exchange,
                            relay=relay, roster=roster)
        self._host = host
        self._port = port
        self._peers = tuple(peers)
        self._lock = threading.Lock()
        self._server = None

    @property
    def hub(self) -> SyncHub:
        return self._hub

    def _permitted(self, message: dict) -> None:
        if not self._peers:
            return
        device = str(message.get("device", "")).strip()
        if device and device not in self._peers:
            raise SyncRefused(f"device {device!r} is not on the peer list")

    def handle_raw(self, raw: str) -> str:
        """One JSON string in, one out. The whole protocol surface."""
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("not an object")
        except ValueError:
            return json.dumps({"type": "sync_error", "reason": "bad json"})
        try:
            self._permitted(message)
        except SyncRefused as exc:
            log.warning("sync: %s", exc)
            return json.dumps({"type": "sync_error", "reason": str(exc)})
        with self._lock:
            return json.dumps(self._hub.handle(message))

    async def _serve(self, websocket) -> None:
        peer = "?"
        try:
            async for raw in websocket:
                if len(raw) > MAX_MESSAGE:
                    await websocket.send(json.dumps(
                        {"type": "sync_error", "reason": "message too large"}))
                    continue
                # The handler touches disk; keeping it off the event loop stops
                # a slow write stalling every other connection.
                reply = await asyncio.to_thread(self.handle_raw, raw)
                await websocket.send(reply)
        except Exception as exc:            # noqa: BLE001
            log.info("sync: connection from %s ended: %s", peer, exc)

    async def start(self) -> bool:
        try:
            import websockets
        except ImportError:
            log.warning("sync server not started: websockets is not installed")
            return False
        try:
            self._server = await websockets.serve(
                self._serve, self._host, self._port, max_size=MAX_MESSAGE)
        except OSError as exc:
            log.warning("sync server could not bind %s:%s — %s",
                        self._host, self._port, exc)
            return False
        log.info("sync hub listening on %s:%s", self._host, self._port)
        return True

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        finally:
            self._server = None


def sync_with_hub(engine: SyncEngine, uri: str, token: str = "",
                  timeout: float = 20.0, relay=None):
    """Run one exchange against a remote hub — the whole leaf-side surface.

    A leaf imports this one function and needs to know nothing else about the
    protocol. Failures propagate: the caller's retry is simply the next tick,
    and nothing was lost, because the watermark only advanced for changes the
    hub acknowledged.
    """
    import websockets.sync.client as ws_client

    leaf = SyncLeaf(engine, token=token, relay=relay)
    with ws_client.connect(uri, open_timeout=timeout,
                           max_size=MAX_MESSAGE) as socket:
        def call(message: dict) -> dict:
            socket.send(json.dumps(message))
            reply = socket.recv(timeout=timeout)
            loaded = json.loads(reply)
            if not isinstance(loaded, dict):
                raise SyncRefused("hub sent something that wasn't a message")
            return loaded

        return leaf.exchange(call)
