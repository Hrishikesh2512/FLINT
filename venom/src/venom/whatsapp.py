"""Send WhatsApp messages by voice, via the self-hosted Baileys bridge.

The bridge (venom/whatsapp-service, a small Node service) owns the WhatsApp
Web session and does the actual sending; this is just a thin HTTP client to
its localhost API. Incoming messages don't come through here — the bridge
delivers those to Venom's NotificationHub over loopback (127.0.0.1), where it
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
        return self.send_detail(text, to)[1]

    def send_detail(self, text: str, to: str = "") -> tuple[bool, str]:
        """send() with an explicit delivered flag.

        The spoken sentence alone can't be trusted by callers that must know
        whether a message really landed (the SOS alert, above all) — every
        failure here reads like prose. Returns (delivered, sentence)."""
        text = (text or "").strip()
        if not text:
            return False, "There's no message to send."
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
            return False, ("I couldn't reach WhatsApp — the bridge isn't running "
                           "or the phone lost its link.")

        if status == 200 and data.get("ok"):
            who = data.get("name") or "them"
            return True, f"Sent to {who}."
        if status == 300 and data.get("candidates"):
            names = [c.get("name", "?") for c in data["candidates"][:4]]
            return False, ("I found a few matching contacts — " + ", ".join(names)
                           + ". Which one?")
        if status == 503:
            return False, ("WhatsApp isn't linked yet — open the console and scan "
                           "the QR under WhatsApp to pair it.")
        reason = data.get("error") or "something went wrong"
        return False, f"I couldn't send that: {reason}."

    def set_auto_reply(self, enabled: bool) -> str:
        """Turn auto-reply mode on/off. When on, the bridge answers incoming
        1:1 messages itself, in the user's voice, until turned off."""
        try:
            _, data = self._request(
                "POST", "/auto-reply", {"enabled": bool(enabled)})
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8", "replace"))
            except (ValueError, OSError):
                data = {}
        except (urllib.error.URLError, OSError) as exc:
            log.warning("whatsapp auto-reply toggle failed: %s", exc)
            return ("I couldn't reach WhatsApp — the bridge isn't running.")
        if enabled:
            if data.get("autoReply") and data.get("hasKey"):
                return ("Auto message mode is on. I'll reply to new WhatsApp "
                        "messages myself until you tell me to stop.")
            if data.get("autoReply") and not data.get("hasKey"):
                return ("Auto mode is on, but I have no Gemini key here to write "
                        "replies — nothing will be sent until that's set.")
            return "I couldn't turn auto mode on."
        return "Auto message mode is off. I'll stop replying on my own."
