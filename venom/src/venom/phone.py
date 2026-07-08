"""Find-my-phone: ring the user's phone loudly via an ntfy topic.

ntfy (ntfy.sh, or a self-hosted server) delivers a push to every device
subscribed to a topic — no account, no API key, one HTTP POST. Subscribe the
ntfy phone app to the topic once, give that topic an alarm/loud sound and
max priority, and shutter button 2 makes the phone ring even on silent.

Outbound WhatsApp uses the reverse path: a hit on a MacroDroid **webhook**
URL (contact + message as query params) triggers a "Send WhatsApp message"
action on the phone. WhatsApp has no send API, so the phone does the sending.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request

log = logging.getLogger("venom.phone")


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


def send_whatsapp(webhook: str, contact: str, message: str,
                  timeout: float = 8.0) -> str:
    """Hit the MacroDroid webhook so the phone sends a WhatsApp message.

    `contact` and `message` ride as URL-encoded query params; MacroDroid reads
    them and runs its Send-WhatsApp action. Returns a spoken-style status."""
    webhook = (webhook or "").strip()
    if not webhook:
        return "WhatsApp sending isn't set up yet."
    contact = (contact or "").strip()
    message = (message or "").strip()
    if not contact:
        return "Who should I message?"
    if not message:
        return "What should I say?"
    sep = "&" if "?" in webhook else "?"
    url = (f"{webhook}{sep}contact={urllib.parse.quote(contact)}"
           f"&message={urllib.parse.quote(message)}")
    try:
        urllib.request.urlopen(url, timeout=timeout).read()
        return f"Sent to {contact}."
    except Exception as exc:  # network / bad webhook — never crash the loop
        log.warning("whatsapp send failed: %s", exc)
        return f"Couldn't send to {contact} — the phone didn't answer."
