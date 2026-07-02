"""Connectivity probing — a single TCP reachability primitive.

Everything that needs to know "can I reach X?" (internet check, laptop
brain, cloud endpoints) goes through probe_tcp so behavior and timeouts
stay uniform and the resolver can be tested with a fake prober.
"""

from __future__ import annotations

import asyncio


async def probe_tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    """True if a TCP connection to host:port succeeds within timeout."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True
