"""Proactive memory for FLINT.

Long-term memory is otherwise passive — it is injected into the system prompt
and recalled only when the user asks. This module makes FLINT *surface* a
relevant memory on its own: an upcoming birthday, a project it has not heard
about in a while, or a wish the user mentioned once.

Design goals:
- Cheap and deterministic. No LLM call to decide what to surface — it reads the
  existing long_term.json and picks a candidate by simple rules.
- Polite. It only speaks when FLINT is idle (not talking, not muted), respects a
  cooldown, caps how often it fires per run, and never nags about the same thing
  twice in a short window (state persisted to a small sidecar file).
- Self-contained. It talks to the live session only through two callbacks the
  engine is handed: `speak(text)` and `is_idle() -> bool`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime
from pathlib import Path

from memory.memory_manager import load_memory, get_base_dir

# ── cadence knobs ────────────────────────────────────────────────────────────
CHECK_INTERVAL_S   = 60          # how often to consider surfacing something
WARMUP_S           = 120         # stay quiet for the first 2 min after start
COOLDOWN_S         = 1800        # at least 30 min between proactive nudges
MAX_PER_RUN        = 4           # cap nudges per process lifetime
RENUDGE_AFTER_DAYS = 3           # don't resurface the same item within N days
PROJECT_STALE_DAYS = 7           # a project gets a check-in after this long
BIRTHDAY_LOOKAHEAD = 3           # surface birthdays up to N days ahead

STATE_PATH = get_base_dir() / "memory" / "proactive_state.json"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_month_day(text: str) -> tuple[int, int] | None:
    """Best-effort (month, day) from a free-text date. Returns None if unsure."""
    if not isinstance(text, str):
        return None
    t = text.strip().lower()

    # ISO-ish: 1999-05-04 / 2001/12/31
    m = re.search(r"\b\d{2,4}[-/](\d{1,2})[-/](\d{1,2})\b", t)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            return mo, da

    # bare MM-DD or DD/MM is ambiguous; skip to avoid wrong guesses.

    # "may 4", "4th may", "4 may"
    name = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", t)
    num  = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if name and num:
        mo = _MONTHS[name.group(1)]
        da = int(num.group(1))
        if 1 <= da <= 31:
            return mo, da
    return None


def _days_until(month: int, day: int, today: date) -> int | None:
    """Days from `today` until the next occurrence of month/day (0 = today)."""
    try:
        this_year = date(today.year, month, day)
    except ValueError:
        return None  # e.g. Feb 29 in a non-leap year — skip
    if this_year >= today:
        delta = (this_year - today).days
    else:
        try:
            nxt = date(today.year + 1, month, day)
        except ValueError:
            return None
        delta = (nxt - today).days
    return delta


def _entry_value(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("value", "")).strip()
    return str(entry or "").strip()


def _days_since(updated: str) -> int | None:
    try:
        d = datetime.strptime(updated, "%Y-%m-%d").date()
    except Exception:
        return None
    return (date.today() - d).days


class ProactiveMemoryEngine:
    """Periodically surfaces a relevant memory through the live session."""

    def __init__(self, speak, is_idle, enabled: bool = True):
        self._speak    = speak          # (str) -> None  (injects a turn)
        self._is_idle  = is_idle        # () -> bool
        self.enabled   = enabled
        self._started  = time.monotonic()
        self._last_fire = 0.0
        self._fired    = 0
        self._state    = self._load_state()

    # ── persistent "already nagged" state ────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[Proactive] ⚠️ state save failed: {e}")

    def _recently_surfaced(self, item_id: str) -> bool:
        last = self._state.get(item_id)
        if not last:
            return False
        days = _days_since(last)
        return days is not None and days < RENUDGE_AFTER_DAYS

    def _mark_surfaced(self, item_id: str) -> None:
        self._state[item_id] = date.today().strftime("%Y-%m-%d")
        self._save_state()

    # ── candidate selection ──────────────────────────────────────────────────
    def _candidates(self, memory: dict) -> list[tuple[int, str, str]]:
        """Return (priority, item_id, directive) tuples. Higher priority first."""
        out: list[tuple[int, str, str]] = []
        today = date.today()

        # 1) Birthdays — highest priority, time-sensitive.
        for cat in ("identity", "relationships"):
            for key, entry in (memory.get(cat) or {}).items():
                if "birthday" not in key.lower() and "birthday" not in _entry_value(entry).lower():
                    continue
                md = _parse_month_day(_entry_value(entry))
                if not md:
                    continue
                delta = _days_until(md[0], md[1], today)
                if delta is None or delta > BIRTHDAY_LOOKAHEAD:
                    continue
                who = key.replace("_", " ").replace("birthday", "").strip() or "someone they know"
                when = "today" if delta == 0 else ("tomorrow" if delta == 1 else f"in {delta} days")
                out.append((
                    100 - delta,
                    f"birthday:{cat}:{key}",
                    f"A birthday is coming up {when}: {who}. Warmly remind the user, "
                    f"and offer to help (a message, a reminder, gift ideas).",
                ))

        # 2) Stale projects — "how's it going?"
        for key, entry in (memory.get("projects") or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            stale = _days_since(entry.get("updated", "")) if isinstance(entry, dict) else None
            if stale is None or stale < PROJECT_STALE_DAYS:
                continue
            name = key.replace("_", " ").strip()
            out.append((
                50,
                f"project:{key}",
                f"The user has a project you have not heard about in a while: "
                f"'{name}' ({val}). Casually check in on how it is going.",
            ))

        # 3) Wishes — gentle check-in, lowest priority.
        for key, entry in (memory.get("wishes") or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            out.append((
                20,
                f"wish:{key}",
                f"The user once mentioned wanting: {val}. Bring it up lightly and "
                f"offer to help make progress on it.",
            ))

        out.sort(key=lambda t: t[0], reverse=True)
        return out

    def _pick(self) -> tuple[str, str] | None:
        memory = load_memory()
        for _prio, item_id, directive in self._candidates(memory):
            if not self._recently_surfaced(item_id):
                return item_id, directive
        return None

    # ── main loop ────────────────────────────────────────────────────────────
    async def loop(self) -> None:
        if not self.enabled:
            print("[Proactive] disabled")
            return
        print("[Proactive] 🧠 engine online")
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            try:
                self._tick()
            except Exception as e:
                print(f"[Proactive] ⚠️ tick error: {e}")

    def _tick(self) -> None:
        now = time.monotonic()
        if self._fired >= MAX_PER_RUN:
            return
        if now - self._started < WARMUP_S:
            return
        if now - self._last_fire < COOLDOWN_S:
            return
        if not self._is_idle():
            return

        pick = self._pick()
        if not pick:
            return

        item_id, directive = pick
        message = (
            "[PROACTIVE THOUGHT — say this now, unprompted, in ONE short, natural, "
            "friendly sentence, the way a thoughtful friend remembers something. "
            "Do not mention that you were prompted or that this is a reminder system.]\n"
            f"{directive}"
        )
        print(f"[Proactive] 💬 surfacing {item_id}")
        self._speak(message)
        self._last_fire = now
        self._fired += 1
        self._mark_surfaced(item_id)
