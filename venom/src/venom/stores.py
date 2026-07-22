"""Persistent, voice-driven productivity stores for Venom.

Reminders, voice notes, and named lists (shopping / to-do) — all small JSON
files in the state dir (`/var/lib/venom/`), so they survive reboots and power
loss exactly like long-term memory does. Atomic writes + a lock, same pattern
as flint-core's MemoryStore.

Reminders differ from timers: timers are relative countdowns held in RAM
(`TimerBoard`), while reminders are absolute wall-clock ("tomorrow 8am") that
must outlive a reboot — so they live here on disk and fire by wall time.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock


class _JsonStore:
    """Base: atomic load/save of one JSON document under a lock."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self, default):
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _save(self, data) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".tmp-",
                                   suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class ReminderStore(_JsonStore):
    """Absolute-time reminders that survive reboots and fire by wall clock."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        super().__init__(path)
        self._clock = clock

    def add(self, text: str, due_epoch: float) -> dict:
        text = (text or "").strip() or "reminder"
        entry = {"text": text, "due": float(due_epoch),
                 "created": self._clock()}
        with self._lock:
            data = self._load([])
            data.append(entry)
            data.sort(key=lambda r: r.get("due", 0))
            self._save(data)
        return entry

    def pop_due(self) -> list[dict]:
        """Remove and return reminders whose time has arrived (persisted)."""
        now = self._clock()
        with self._lock:
            data = self._load([])
            due = [r for r in data if r.get("due", 0) <= now]
            if due:
                self._save([r for r in data if r.get("due", 0) > now])
        return due

    def pending(self) -> list[dict]:
        now = self._clock()
        return sorted((r for r in self._load([]) if r.get("due", 0) > now),
                      key=lambda r: r.get("due", 0))

    def cancel(self, text: str) -> int:
        """Remove reminders whose text contains `text` (case-insensitive)."""
        needle = (text or "").strip().lower()
        if not needle:
            return 0
        with self._lock:
            data = self._load([])
            keep = [r for r in data if needle not in r.get("text", "").lower()]
            removed = len(data) - len(keep)
            if removed:
                self._save(keep)
        return removed


class NoteStore(_JsonStore):
    """A running list of voice notes."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        super().__init__(path)
        self._clock = clock

    def add(self, text: str) -> dict:
        entry = {"text": (text or "").strip(), "ts": self._clock()}
        with self._lock:
            data = self._load([])
            data.append(entry)
            self._save(data)
        return entry

    def all(self) -> list[dict]:
        return self._load([])

    def clear(self) -> int:
        with self._lock:
            data = self._load([])
            self._save([])
        return len(data)


class ConversationLog(_JsonStore):
    """A rolling journal of spoken exchanges, so she remembers what you two
    talked about across sessions and reboots — not just saved facts.

    Every completed turn is appended; the most recent window is rendered into
    the next session's system prompt for continuity ("kal wali problem solve
    hui?"), capped so the file and the prompt both stay small."""

    MAX_TURNS = 400          # on disk
    PROMPT_TURNS = 40        # rendered into the prompt
    MAX_TEXT = 300           # per-turn chars kept

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        super().__init__(path)
        self._clock = clock

    def add(self, who: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        entry = {"who": who, "text": text[:self.MAX_TEXT], "ts": self._clock()}
        with self._lock:
            data = self._load([])
            data.append(entry)
            if len(data) > self.MAX_TURNS:
                data = data[-self.MAX_TURNS:]
            self._save(data)

    def recent(self, n: int = PROMPT_TURNS) -> list[dict]:
        return self._load([])[-n:]

    def _when(self, ts: float, now: float) -> str:
        lt, ln = time.localtime(ts), time.localtime(now)
        clock = time.strftime("%I:%M %p", lt).lstrip("0")
        if (lt.tm_year, lt.tm_yday) == (ln.tm_year, ln.tm_yday):
            return clock
        if now - ts < 172800 and abs(ln.tm_yday - lt.tm_yday) in (1, 364, 365):
            return f"yesterday {clock}"
        return time.strftime("%a %d %b, ", lt) + clock

    def render_for_prompt(self, n: int = PROMPT_TURNS,
                          now: float | None = None) -> str:
        turns = self.recent(n)
        if not turns:
            return ""
        now = self._clock() if now is None else now
        lines = ["[RECENT CONVERSATIONS — what you two actually talked about "
                 "lately. Use it: pick up threads, follow up on things he "
                 "mentioned, honour what you told him. You have ALREADY "
                 "greeted him at the times shown — do not restart with "
                 "first-hello energy or repeat an observation (like the late "
                 "hour) you already made. Lines in [square brackets] are tools "
                 "that ACTUALLY RAN — the real record of what happened. A "
                 "claim of yours with no bracketed line behind it never "
                 "happened: never imitate those, and never let one of yours "
                 "be empty.]"]
        last_when = ""
        for t in turns:
            when = self._when(float(t.get("ts", now)), now)
            stamp = f"({when}) " if when != last_when else ""
            last_when = when
            speaker = t.get("who")
            text = t.get("text", "")
            if speaker == "action":
                # Proof a tool ran. Without these the journal reads as a string
                # of bare claims and she learns that saying it is doing it —
                # see ACTION_TOOLS in venom/live.py.
                lines.append(f"{stamp}[you really did it: {text}]")
                continue
            who = "him" if speaker == "you" else "you"
            lines.append(f"{stamp}{who}: {text}")
        return "\n".join(lines) + "\n"


class ConnectionStore(_JsonStore):
    """People Venom knows — numbers, nicknames, socials, interests, notes — so
    she can both *contact* them (name -> number, independent of WhatsApp's own
    contact sync) and *recall who they are*. One record per person, keyed by a
    normalized name; nicknames/aliases resolve to the same record. Every field
    merges, so telling her one fact at a time builds the person up over time.

    Record shape:
        {"name": "Rahul Sharma", "aliases": ["rahul bhai"],
         "phones": ["919812345678"], "instagram": "rahul.s",
         "interests": ["cricket"], "notes": ["met at college"]}
    """

    @staticmethod
    def _key(name: str) -> str:
        return (name or "").strip().lower()

    @staticmethod
    def _digits(phone: str) -> str:
        return "".join(ch for ch in (phone or "") if ch.isdigit())

    @staticmethod
    def _blank(name: str) -> dict:
        return {"name": name, "aliases": [], "phones": [],
                "instagram": "", "interests": [], "notes": []}

    def _match_key(self, data: dict, query: str) -> str | None:
        """Storage key for a person by name or any alias (exact, then loose)."""
        q = (query or "").strip().lower()
        if not q:
            return None
        if q in data:
            return q
        for k, rec in data.items():
            if q in [a.lower() for a in rec.get("aliases", [])]:
                return k
        # Loose: query is a substring of the name, or every word of the query
        # appears in the name ("rahul" -> "Rahul Sharma", "sharma rahul" too).
        for k, rec in data.items():
            name = rec.get("name", "").lower()
            hay = [name, k] + [a.lower() for a in rec.get("aliases", [])]
            if any(q in h for h in hay if h):
                return k
            if name and all(w in name for w in q.split()):
                return k
        return None

    @staticmethod
    def _add_unique(lst: list, val: str) -> None:
        val = (val or "").strip()
        if val and val.lower() not in [x.lower() for x in lst]:
            lst.append(val)

    def save(self, name: str, phone: str = "", nickname: str = "",
             instagram: str = "", interest: str = "", note: str = "") -> dict:
        """Create or update a person, merging every provided field."""
        with self._lock:
            data = self._load({})
            key = (self._match_key(data, name)
                   or (self._match_key(data, nickname) if nickname else None))
            if key is None:
                key = self._key(name) or self._key(nickname)
                if not key:
                    return {}
                rec = self._blank((name or nickname).strip())
            else:
                rec = data.get(key, self._blank((name or nickname).strip()))
            if name and not rec.get("name"):
                rec["name"] = name.strip()
            # A different-but-real name given for an existing record becomes an
            # alias rather than clobbering the canonical name.
            if name and name.strip().lower() != rec.get("name", "").lower():
                self._add_unique(rec["aliases"], name)
            if nickname:
                self._add_unique(rec["aliases"], nickname)
            digits = self._digits(phone)
            if digits:
                self._add_unique(rec["phones"], digits)
            if instagram:
                rec["instagram"] = instagram.strip().lstrip("@")
            if interest:
                self._add_unique(rec["interests"], interest)
            if note:
                self._add_unique(rec["notes"], note)
            data[key] = rec
            self._save(data)
        return rec

    def find(self, query: str) -> dict | None:
        data = self._load({})
        key = self._match_key(data, query)
        return data.get(key) if key else None

    def phone_for(self, query: str) -> str:
        rec = self.find(query)
        phones = rec.get("phones", []) if rec else []
        return phones[0] if phones else ""

    def describe(self, query: str) -> str:
        rec = self.find(query)
        if not rec:
            return f"I don't have anyone saved as {query}."
        parts = [rec["name"]]
        if rec.get("aliases"):
            parts.append(f"(also {', '.join(rec['aliases'])})")
        bits = []
        if rec.get("phones"):
            bits.append(f"number {', '.join(rec['phones'])}")
        if rec.get("instagram"):
            bits.append(f"Instagram {rec['instagram']}")
        if rec.get("interests"):
            bits.append(f"into {', '.join(rec['interests'])}")
        if rec.get("notes"):
            bits.append("; ".join(rec["notes"]))
        return " — ".join([" ".join(parts)] + ([", ".join(bits)] if bits else [])) \
            or rec["name"]

    def all_names(self) -> list[str]:
        return [rec.get("name", k) for k, rec in self._load({}).items()]

    def forget(self, query: str) -> bool:
        with self._lock:
            data = self._load({})
            key = self._match_key(data, query)
            if key is None:
                return False
            del data[key]
            self._save(data)
        return True


class ListStore(_JsonStore):
    """Named item lists — shopping, to-do, packing, etc."""

    DEFAULT = "shopping"

    def _norm(self, name: str) -> str:
        return (name or self.DEFAULT).strip().lower() or self.DEFAULT

    def add_item(self, item: str, list_name: str = DEFAULT) -> str:
        name, item = self._norm(list_name), (item or "").strip()
        if not item:
            return "nothing to add"
        with self._lock:
            data = self._load({})
            items = data.setdefault(name, [])
            if any(i.lower() == item.lower() for i in items):
                return f"{item} is already on the {name} list"
            items.append(item)
            self._save(data)
        return f"added {item} to the {name} list"

    def remove_item(self, item: str, list_name: str = DEFAULT) -> str:
        name, needle = self._norm(list_name), (item or "").strip().lower()
        with self._lock:
            data = self._load({})
            items = data.get(name, [])
            keep = [i for i in items if i.lower() != needle]
            if len(keep) == len(items):
                return f"{item} isn't on the {name} list"
            data[name] = keep
            self._save(data)
        return f"removed {item} from the {name} list"

    def show(self, list_name: str = DEFAULT) -> list[str]:
        return list(self._load({}).get(self._norm(list_name), []))

    def clear(self, list_name: str = DEFAULT) -> int:
        name = self._norm(list_name)
        with self._lock:
            data = self._load({})
            count = len(data.get(name, []))
            if name in data:
                data[name] = []
                self._save(data)
        return count

    def names(self) -> list[str]:
        return [n for n, items in self._load({}).items() if items]
