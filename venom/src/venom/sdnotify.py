"""Minimal sd_notify client (systemd readiness + watchdog), zero dependencies.

When the daemon runs under a systemd unit with Type=notify and WatchdogSec
set, systemd restarts it if the heartbeat stops — the core of the
"long-running wearable that recovers by itself" requirement. Outside
systemd (dev boxes, tests) every call is a silent no-op.
"""

from __future__ import annotations

import os
import socket


def _socket_address() -> str | None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return None
    if addr.startswith("@"):  # abstract socket namespace
        addr = "\0" + addr[1:]
    return addr


def sd_notify(state: str) -> bool:
    """Send one sd_notify datagram; True if it was actually sent."""
    addr = _socket_address()
    if addr is None or not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError:
        return False


def notify_ready() -> bool:
    return sd_notify("READY=1")


def notify_watchdog() -> bool:
    return sd_notify("WATCHDOG=1")


def notify_stopping() -> bool:
    return sd_notify("STOPPING=1")
