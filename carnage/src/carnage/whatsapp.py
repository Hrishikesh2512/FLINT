"""His old WhatsApp chats, from WhatsApp's own "Export chat".

WhatsApp keeps its history encrypted where no other app can read it, and its
backups are encrypted the same way. The one door it opens is per chat: ⋮ →
More → Export chat, shared to Jarvis. That text, every message, his own
included, is parsed here and filed in her archive, so "what did Rahul say
about Goa?" is answered from the chat itself months later.

Filed as one entry per chat per day: a day of a conversation is the unit
that reads well when found ("12 Mar 2026, with Rahul: …"), and it keeps a
years-long chat to a few thousand entries rather than tens of thousands.
Importing the same export twice adds nothing: each day is filed once, and a
newer export of the same chat adds only the days that are new.

The archive is shared with her other devices through sync, like the rest of
her memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from flint_core.recall import EPISODE

log = logging.getLogger("carnage.whatsapp")

#: One day of a chat is split into entries of about this size, so a busy
#: day stays readable when it comes back from a search.
CHUNK = 1500

# 15/10/26, 10:24 am - Rahul: hi          (Android, 12h; the space before am
# 15/10/2026, 22:24 - Rahul: hi            may be a narrow no-break space)
# [15/10/26, 10:24:05 AM] Rahul: hi        (iPhone)
_LINE = re.compile(
    r"^‎?\[?(?P<d>\d{1,2})[/.-](?P<m>\d{1,2})[/.-](?P<y>\d{2,4}),?\s+"
    r"(?P<h>\d{1,2}):(?P<min>\d{2})(?::\d{2})?[\s  ]*(?P<ap>[aApP]\.?[mM]\.?)?\]?"
    r"\s*(?:-\s*)?(?P<rest>.*)$")
_SENDER = re.compile(r"^(?P<who>[^:]{1,60}?):\s(?P<text>.*)$", re.DOTALL)

#: Lines that carry nothing worth remembering.
_NOISE = re.compile(
    r"^(<media omitted>|<attached: .*>|image omitted|video omitted|audio omitted|"
    r"sticker omitted|gif omitted|document omitted|null|this message was deleted|"
    r"you deleted this message|<this message was edited>)$", re.IGNORECASE)


@dataclass(frozen=True)
class Message:
    when: datetime
    who: str
    text: str


def chat_name(*hints: str) -> str:
    """The chat's name from the export's file name or share subject."""
    for hint in hints:
        found = re.search(r"WhatsApp Chat (?:with|-)\s*(.+?)(?:\.txt|\.zip)?$", hint or "",
                          re.IGNORECASE)
        if found:
            return found.group(1).strip()
    return ""


def parse(text: str) -> list[Message]:
    """Every message in an export, oldest first. [] when it is not one."""
    rows: list[tuple[tuple, str]] = []
    for raw in (text or "").splitlines():
        line = _LINE.match(raw)
        if line:
            rows.append((_parts(line), line["rest"]))
        elif rows:
            # A message that ran over several lines.
            parts, rest = rows[-1]
            rows[-1] = (parts, f"{rest}\n{raw}")
    if len(rows) < 2:
        return []
    day_first = _day_first([p for p, _ in rows])
    out: list[Message] = []
    for parts, rest in rows:
        sender = _SENDER.match(rest)
        if not sender:
            continue                        # "Messages are end-to-end encrypted", joins, …
        body = sender["text"].replace("‎", "").strip()
        if not body or _NOISE.match(body):
            continue
        when = _when(parts, day_first)
        if when is not None:
            out.append(Message(when, sender["who"].strip(), body))
    return out


def import_export(archive, text: str, chat: str = "", me: str = "",
                  ledger: Path | None = None) -> dict:
    """File an export in the archive. {"imported", "messages", "days",
    "new_days", "chat", "first", "last"}, "imported" False when the text
    is not a WhatsApp export at all."""
    messages = parse(text)
    if not messages:
        return {"imported": False}
    chat = chat.strip() or _guess_chat(messages, me) or "a chat"
    seen = _load(ledger)
    days: dict[str, list[Message]] = {}
    for m in messages:
        days.setdefault(m.when.strftime("%Y-%m-%d"), []).append(m)

    new_days = 0
    for day, said in days.items():
        key = hashlib.sha256(f"{chat}|{day}".encode()).hexdigest()[:20]
        digest = hashlib.sha256("\n".join(f"{m.who}:{m.text}" for m in said).encode()).hexdigest()
        if seen.get(key) == digest:
            continue
        if key in seen:
            # A newer export of a day already filed (the day was still going):
            # only the messages after what was filed are new.
            said = said[seen.get(f"{key}:n", 0):]
        for piece in _chunks(said, me):
            archive.remember(
                f"{said[0].when.strftime('%-d %b %Y')}, with {chat}: {piece}",
                kind=EPISODE, subject=f"WhatsApp {chat}",
                ts=said[0].when.timestamp())
        seen[key] = digest
        seen[f"{key}:n"] = len(days[day])
        new_days += 1
    _save(ledger, seen)
    log.info("whatsapp: %s, %d messages over %d days, %d new",
             chat, len(messages), len(days), new_days)
    return {"imported": True, "chat": chat, "messages": len(messages),
            "days": len(days), "new_days": new_days,
            "first": messages[0].when.strftime("%b %Y"),
            "last": messages[-1].when.strftime("%b %Y")}


def spoken(result: dict) -> str:
    """What she says once an import is done."""
    if not result.get("imported"):
        return "That doesn't look like a WhatsApp chat export."
    if not result.get("new_days"):
        return f"I already have your chat with {result['chat']}, nothing new in this one."
    span = (result["first"] if result["first"] == result["last"]
            else f"{result['first']} to {result['last']}")
    return (f"Got it, your chat with {result['chat']}, {result['messages']:,} messages "
            f"from {span}. Ask me anything about it.")


# ── the details ─────────────────────────────────────────────────────────────
def _parts(line: re.Match) -> tuple:
    return (int(line["d"]), int(line["m"]), int(line["y"]), int(line["h"]),
            int(line["min"]), (line["ap"] or "").replace(".", "").lower())


def _day_first(parts: list[tuple]) -> bool:
    """dd/mm or mm/dd? Settled by any field over 12; India writes day first."""
    if any(p[1] > 12 for p in parts):
        return False
    return True


def _when(parts: tuple, day_first: bool) -> datetime | None:
    a, b, year, hour, minute, half = parts
    day, month = (a, b) if day_first else (b, a)
    if year < 100:
        year += 2000
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _chunks(said: list[Message], me: str) -> list[str]:
    lines = [f"{'He' if me and m.who.lower() == me.lower() else m.who}: {m.text}"
             for m in said]
    out, current = [], ""
    for line in lines:
        if current and len(current) + len(line) > CHUNK:
            out.append(current)
            current = ""
        current = f"{current} · {line}" if current else line
    if current:
        out.append(current)
    return out


def _guess_chat(messages: list[Message], me: str) -> str:
    """With no name given: the one other person, when there is just one."""
    others = {m.who for m in messages if not me or m.who.lower() != me.lower()}
    return others.pop() if len(others) == 1 else ""


def _load(ledger: Path | None) -> dict:
    if ledger is None or not ledger.exists():
        return {}
    try:
        return json.loads(ledger.read_text())
    except (OSError, ValueError):
        return {}


def _save(ledger: Path | None, seen: dict) -> None:
    if ledger is None:
        return
    tmp = ledger.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen))
    tmp.replace(ledger)
