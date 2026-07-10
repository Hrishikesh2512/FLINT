"""Find-my-phone: ring the user's phone loudly via an ntfy topic.

ntfy (ntfy.sh, or a self-hosted server) delivers a push to every device
subscribed to a topic — no account, no API key, one HTTP POST. Subscribe the
ntfy phone app to the topic once, give that topic an alarm/loud sound and
max priority, and shutter button 2 makes the phone ring even on silent.
"""

from __future__ import annotations

import logging
import urllib.request

log = logging.getLogger("venom.phone")


def push_alert(server: str, topic: str, body: str, title: str = "Venom",
               priority: str = "high", timeout: float = 8.0) -> bool:
    """POST a plain notification to the phone's ntfy topic — Venom's channel
    for anything it must say when the headset can't carry it (dead audio
    path, and whatever else needs the user's eyes)."""
    topic = (topic or "").strip()
    if not topic:
        return False
    req = urllib.request.Request(
        f"{server.rstrip('/')}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": "warning"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as exc:  # network, DNS, HTTP error — never crash the loop
        log.warning("phone alert failed: %s", exc)
        return False


def find_phone(server: str, topic: str, timeout: float = 8.0) -> str:
    """POST a max-priority alert to the phone's ntfy topic. Returns a spoken-
    style status string (also useful in logs)."""
    topic = (topic or "").strip()
    if not topic:
        return "No phone is set up to find."
    url = f"{server.rstrip('/')}/{topic}"
    req = urllib.request.Request(
        url,
        data=b"Venom is looking for your phone",
        headers={"Title": "Find my phone", "Priority": "max",
                 "Tags": "loudspeaker"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return "Ringing your phone."
    except Exception as exc:  # network, DNS, HTTP error — never crash the loop
        log.warning("find-phone failed: %s", exc)
        return "Couldn't reach your phone."
