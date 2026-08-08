"""Remembering more than fits in a prompt.

`MemoryStore` holds 2200 characters, trimmed oldest-first, and rides inside
every system prompt. That is exactly the right design for what it does — a
handful of facts she should never have to look up, present in every sentence
she speaks. It is also a hard ceiling, and it is the reason "long-term memory
of users, projects, conversations and preferences" has never been true here:
the fourth project pushes out the first, and a conversation from three weeks
ago was never in there at all.

So: two tiers, with different jobs.

    hot      MemoryStore — small, always in the prompt, never searched
    archive  this module — unbounded, never in the prompt, searched on demand

Nothing moves automatically between them. Promotion by some heuristic sounds
clever and produces a prompt that changes for reasons nobody can see; instead
the hot tier stays hand-curated by `save_memory`, and everything else lands
here where it can be found when it is actually relevant.

**Retrieval is lexical, not vector.** A 2 GB Pi runs no embedding model, and
shipping one would break the constraint the whole product is built on. For
personal-scale data — thousands of entries, not millions — scored token
overlap with a recency nudge is genuinely good enough, and it has the large
advantage of being explainable: you can see why something matched.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("flint.recall")

EPISODE = "episode"      # something that happened / was discussed
FACT = "fact"            # a durable detail that didn't fit the hot tier
PROJECT = "project"      # notes about a thing being built
PERSON = "person"        # who someone is
KINDS = (EPISODE, FACT, PROJECT, PERSON)

#: Entries returned by a search. More than this and the prompt block stops
#: being a recall and starts being a dump.
DEFAULT_LIMIT = 5

#: Below this score an entry is noise that happens to share a common word.
MIN_SCORE = 0.15

#: Recency nudge: an entry this old gets no boost at all. Recent memories
#: being slightly preferred matches how people actually ask ("that thing we
#: talked about") without letting recency drown out a better match.
RECENCY_HORIZON = 90 * 86400
RECENCY_WEIGHT = 0.25

_WORD = re.compile(r"[a-z0-9']+")

#: Words that match everything and therefore mean nothing.
_STOPWORDS = frozenset("""
a an and are as at be been but by can did do does for from had has have he her
his how i if in is it its me my of on or our she so than that the their them
then there these they this to was we were what when where which who why will
with would you your im dont ive kya hai ka ki ke ko me mein aur hi bhi tha thi
""".split())


def _normalise(word: str) -> str:
    """Fold a possessive onto its root.

    Without this, "Rahul's wedding" files under `rahul's` and a search for
    "Rahul" never finds it — and possessives are most of how anyone refers to
    the people and things a personal assistant remembers.
    """
    if word.endswith("'s"):
        word = word[:-2]
    return word.replace("'", "")


def tokenise(text: str) -> list[str]:
    words = (_normalise(w) for w in _WORD.findall((text or "").lower()))
    return [w for w in words if w and w not in _STOPWORDS and len(w) > 1]


@dataclass(frozen=True)
class Entry:
    id: int
    kind: str
    subject: str
    text: str
    ts: float
    score: float = 0.0

    def when(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        days = max(0, int((now - self.ts) // 86400))
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 30:
            return f"{days} days ago"
        if days < 365:
            return f"{days // 30} month{'s' if days >= 60 else ''} ago"
        return f"{days // 365} year{'s' if days >= 730 else ''} ago"

    def line(self, now: float | None = None) -> str:
        head = f"{self.subject}: " if self.subject else ""
        return f"({self.when(now)}) {head}{self.text}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    text    TEXT NOT NULL,
    tokens  TEXT NOT NULL DEFAULT '',
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind, ts);
"""


class Archive:
    """Everything she knows that doesn't fit in the prompt."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                log.debug("recall: WAL unavailable")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── writing ─────────────────────────────────────────────────────────────
    def remember(self, text: str, kind: str = FACT, subject: str = "",
                 ts: float | None = None) -> int | None:
        text = " ".join((text or "").split())
        if not text:
            return None
        kind = kind if kind in KINDS else FACT
        tokens = tokenise(f"{subject} {text}")
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO entries (kind, subject, text, tokens, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, subject.strip(), text, json.dumps(tokens),
                 float(ts if ts is not None else self._clock())))
            self._db.commit()
            return int(cursor.lastrowid)

    def forget(self, entry_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self._db.commit()
        return cursor.rowcount > 0

    def forget_matching(self, query: str) -> int:
        """Drop everything that matches — for "forget about X"."""
        victims = self.search(query, limit=100)
        return sum(1 for entry in victims if self.forget(entry.id))

    # ── reading ─────────────────────────────────────────────────────────────
    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def _rows(self, kind: str = "") -> list[sqlite3.Row]:
        sql = "SELECT * FROM entries"
        args: tuple = ()
        if kind:
            sql += " WHERE kind = ?"
            args = (kind,)
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def recent(self, limit: int = DEFAULT_LIMIT, kind: str = "") -> list[Entry]:
        rows = sorted(self._rows(kind), key=lambda r: r["ts"], reverse=True)
        return [self._entry(r) for r in rows[:limit]]

    @staticmethod
    def _entry(row, score: float = 0.0) -> Entry:
        return Entry(id=row["id"], kind=row["kind"], subject=row["subject"],
                     text=row["text"], ts=row["ts"], score=score)

    def search(self, query: str, limit: int = DEFAULT_LIMIT,
               kind: str = "") -> list[Entry]:
        """Entries worth reading for this query, best first.

        Rare words count for more than common ones (a search for "Rahul's
        wedding" should be decided by "Rahul", not by "wedding" appearing in
        forty entries), and recent entries get a small nudge.
        """
        wanted = set(tokenise(query))
        if not wanted:
            return []
        rows = self._rows(kind)
        if not rows:
            return []

        # How many entries contain each term — the basis for weighting it.
        total = len(rows)
        parsed = []
        seen_in: dict[str, int] = {}
        for row in rows:
            try:
                tokens = set(json.loads(row["tokens"]))
            except (TypeError, ValueError):
                tokens = set(tokenise(f"{row['subject']} {row['text']}"))
            parsed.append((row, tokens))
            for term in tokens & wanted:
                seen_in[term] = seen_in.get(term, 0) + 1

        now = self._clock()
        scored: list[Entry] = []
        for row, tokens in parsed:
            overlap = tokens & wanted
            if not overlap:
                continue
            # Inverse document frequency: a term in every entry says nothing.
            weight = sum(math.log(1 + total / (1 + seen_in.get(t, 0)))
                         for t in overlap)
            best_possible = sum(math.log(1 + total / 1) for _ in wanted)
            relevance = weight / best_possible if best_possible else 0.0
            age = max(0.0, now - float(row["ts"]))
            recency = max(0.0, 1.0 - age / RECENCY_HORIZON)
            score = relevance * (1 - RECENCY_WEIGHT) + recency * RECENCY_WEIGHT * relevance
            if score >= MIN_SCORE:
                scored.append(self._entry(row, score))

        scored.sort(key=lambda e: (-e.score, -e.ts))
        return scored[:limit]

    # ── prompt support ──────────────────────────────────────────────────────
    def render_for_prompt(self, query: str, limit: int = DEFAULT_LIMIT,
                          now: float | None = None) -> str:
        """The recalled block, or "" when nothing is relevant.

        Returning nothing when nothing matches is the whole point: a recall
        section padded with the closest available entries teaches her to bring
        up things that have no bearing on what was asked.
        """
        found = self.search(query, limit)
        if not found:
            return ""
        now = self._clock() if now is None else now
        lines = ["[THINGS YOU REMEMBER that seem relevant to this. Use them "
                 "naturally if they fit; ignore them if they don't.]"]
        lines += [f"- {entry.line(now)}" for entry in found]
        return "\n".join(lines) + "\n"

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for row in self._rows():
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        if not counts:
            return "I haven't got anything filed away yet."
        parts = [f"{n} {kind}{'s' if n != 1 else ''}"
                 for kind, n in sorted(counts.items())]
        return "I've got " + ", ".join(parts) + " filed away."


def archive_conversation(archive: Archive, turns: Sequence[dict],
                         summarise: Callable[[str], str] | None = None,
                         subject: str = "") -> int | None:
    """File a finished conversation so it can be found again later.

    Stores a summary when one can be made and the raw exchange otherwise —
    a worse memory is still better than no memory, and a summariser that is
    rate-limited must not silently lose the conversation.
    """
    said = [f"{t.get('who', '?')}: {t.get('text', '')}" for t in turns
            if t.get("text") and t.get("who") != "action"]
    if not said:
        return None
    transcript = "\n".join(said)
    text = transcript
    if summarise is not None:
        try:
            summarised = summarise(transcript).strip()
            if summarised:
                text = summarised
        except Exception as exc:            # noqa: BLE001
            log.warning("recall: could not summarise conversation (%s)", exc)
    return archive.remember(text, kind=EPISODE, subject=subject)
