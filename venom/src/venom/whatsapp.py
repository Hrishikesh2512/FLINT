"""Send WhatsApp messages by voice, via the self-hosted Baileys bridge.

The bridge (venom/whatsapp-service, a small Node service) owns the WhatsApp
Web session and does the actual sending; this is just a thin HTTP client to
its localhost API. Incoming messages don't come through here — the bridge
forwards those to Venom's ntfy topic, where the existing NotificationHub
chimes and reads them. So this module is send-only.

Design mirrors gmail.py: every call is short-lived, failures degrade into a
plain spoken sentence, and nothing here can crash the voice loop.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from venom.config import WhatsAppConfig

log = logging.getLogger("venom.whatsapp")


class WhatsAppClient:
    def __init__(self, config: WhatsAppConfig):
        self._config = config
        self._base = f"http://{config.host}:{config.port}"

    def _request(self, method: str, path: str, body: dict | None = None):
        url = self._base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._config.token:
            headers["X-Token"] = self._config.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self._config.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})

    def status(self) -> dict:
        """Bridge health: {connected, loggedIn, user, contacts, hasQR}."""
        try:
            _, data = self._request("GET", "/health")
            return data
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("whatsapp health failed: %s", exc)
            return {}

    def send(self, text: str, to: str = "") -> str:
        """Send `text` to `to` (a name, number, or blank to reply to the last
        chat). Returns a spoken-style confirmation or explanation."""
        text = (text or "").strip()
        if not text:
            return "There's no message to send."
        try:
            status, data = self._request(
                "POST", "/send", {"to": to or "", "text": text})
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8", "replace"))
            except (ValueError, OSError):
                data = {}
            status = exc.code
        except (urllib.error.URLError, OSError) as exc:
            log.warning("whatsapp send failed: %s", exc)
            return ("I couldn't reach WhatsApp — the bridge isn't running or the "
                    "phone lost its link.")

        if status == 200 and data.get("ok"):
            who = data.get("name") or "them"
            return f"Sent to {who}."
        if status == 300 and data.get("candidates"):
            names = [c.get("name", "?") for c in data["candidates"][:4]]
            return ("I found a few matching contacts — " + ", ".join(names)
                    + ". Which one?")
        if status == 503:
            return ("WhatsApp isn't linked yet — open the console and scan the "
                    "QR under WhatsApp to pair it.")
        reason = data.get("error") or "something went wrong"
        return f"I couldn't send that: {reason}."
