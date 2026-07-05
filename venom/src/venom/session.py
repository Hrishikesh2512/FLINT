"""Lightweight session state — distinct from the user-facing memory of facts.

Tracks the last time Venom spoke with the user so it can deliver a proactive
morning briefing on the first real conversation of the day. Small JSON in the
state dir; missing/corrupt file just means "never seen", never an error.

Briefing rule: the first conversation that is both (a) after 07:30 local and
(b) at least 6 hours since the last interaction (i.e. a genuine fresh start,
not just picking the headset back up), and not already briefed today.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

BRIEF_AFTER_MINUTES = 7 * 60 + 30   # 07:30 local
FRESH_GAP_SECONDS = 6 * 3600        # 6 hours away = a new morning


class SessionState:
    def __init__(self, path: Path):
        self._path = Path(path)

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def should_brief(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        lt = time.localtime(now)
        if lt.tm_hour * 60 + lt.tm_min < BRIEF_AFTER_MINUTES:
            return False  # too early in the morning
        data = self._load()
        if data.get("last_briefed_date") == time.strftime("%Y-%m-%d", lt):
            return False  # already briefed today
        last = data.get("last_interaction", 0) or 0
        if last and (now - last) < FRESH_GAP_SECONDS:
            return False  # used recently — not a fresh morning
        return True

    def mark_briefed(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        data = self._load()
        data["last_briefed_date"] = time.strftime("%Y-%m-%d", time.localtime(now))
        data["last_interaction"] = now
        self._write(data)

    def mark_interaction(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        data = self._load()
        data["last_interaction"] = now
        self._write(data)
