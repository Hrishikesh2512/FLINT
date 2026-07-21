"""Gmail, the zero-OAuth way: IMAP with an app password.

Read-only by design — the mailbox is opened readonly so nothing is ever
marked seen, moved, or deleted; Venom just answers "any new mail?" and
reads a message aloud on request. An app password (Google account with
2FA) never expires, unlike testing-mode OAuth tokens.

Every IMAP call opens a short-lived connection (the Pi may sit idle for
hours; long-lived IMAP sessions just rot). The connection factory is
injectable so parsing is testable without a mailbox.
"""

from __future__ import annotations

import email
import email.header
import imaplib
import logging
import re

from venom.config import MailConfig

log = logging.getLogger("venom.gmail")

MAX_SPOKEN_BODY = 700


def _default_connect(config: MailConfig) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(config.imap_host, timeout=15)
    client.login(config.address, config.app_password)
    return client


def _decode(value: str) -> str:
    """RFC2047 headers ('=?UTF-8?B?...?=') → readable text."""
    parts = []
    for chunk, enc in email.header.decode_header(value or ""):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _sender_name(raw: str) -> str:
    """'Name <a@b.c>' → 'Name'; bare addresses stay as the mailbox part."""
    raw = _decode(raw)
    match = re.match(r'\s*"?([^"<]+?)"?\s*<', raw)
    if match:
        return match.group(1).strip()
    return raw.split("@")[0].strip("<> ")


def _plain_body(msg: email.message.Message) -> str:
    """The text/plain part, cleaned up for speech."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition"))):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
    body = re.sub(r"https?://\S+", "(link)", body)   # URLs are unspeakable
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{2,}", "\n", body).strip()
    if len(body) > MAX_SPOKEN_BODY:
        body = body[:MAX_SPOKEN_BODY] + " …(trimmed)"
    return body


class Mailbox:
    def __init__(self, config: MailConfig, connect=_default_connect):
        self._config = config
        self._connect = connect

    def _open(self):
        client = self._connect(self._config)
        client.select("INBOX", readonly=True)  # NEVER mutate the mailbox
        return client

    def unread_count(self) -> int:
        """How many unread messages are sitting there; -1 if we can't tell.

        A bare SEARCH with no fetches — cheap enough for the ambient loop to
        ask on every tick without hammering Gmail.
        """
        try:
            client = self._open()
            try:
                _, data = client.search(None, "UNSEEN")
                return len((data[0] or b"").split())
            finally:
                client.logout()
        except Exception as exc:
            log.warning("unread count failed: %s", exc)
            return -1

    def unread_summary(self, limit: int = 5) -> str:
        try:
            client = self._open()
            try:
                _, data = client.search(None, "UNSEEN")
                ids = (data[0] or b"").split()
                if not ids:
                    return "No unread mail — inbox zero, boss."
                lines = []
                for mid in ids[-limit:][::-1]:
                    _, msg_data = client.fetch(
                        mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                    msg = email.message_from_bytes(msg_data[0][1])
                    lines.append(f"{_sender_name(msg.get('From', '?'))} — "
                                 f"{_decode(msg.get('Subject', '(no subject)'))}")
                more = len(ids) - len(lines)
                summary = f"{len(ids)} unread. " + "; ".join(lines)
                if more > 0:
                    summary += f"; and {more} more"
                return summary + "."
            finally:
                client.logout()
        except Exception as exc:
            log.warning("unread check failed: %s", exc)
            return ("I couldn't check the mail — Gmail login failed or the "
                    "network is down.")

    def read_latest(self, from_sender: str = "") -> str:
        try:
            client = self._open()
            try:
                query = ("UNSEEN" if not from_sender
                         else f'FROM "{from_sender}"')
                _, data = client.search(None, query)
                ids = (data[0] or b"").split()
                if not ids and not from_sender:
                    _, data = client.search(None, "ALL")  # no unread → latest
                    ids = (data[0] or b"").split()
                if not ids:
                    whom = f" from {from_sender}" if from_sender else ""
                    return f"No mail{whom} found."
                _, msg_data = client.fetch(ids[-1], "(BODY.PEEK[])")
                msg = email.message_from_bytes(msg_data[0][1])
                sender = _sender_name(msg.get("From", "someone"))
                subject = _decode(msg.get("Subject", "(no subject)"))
                body = _plain_body(msg) or "(the message has no readable text)"
                return f"From {sender}, subject: {subject}. {body}"
            finally:
                client.logout()
        except Exception as exc:
            log.warning("read latest failed: %s", exc)
            return ("I couldn't fetch the mail — Gmail login failed or the "
                    "network is down.")
