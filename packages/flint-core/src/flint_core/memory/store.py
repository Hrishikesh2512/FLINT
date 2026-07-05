"""Long-term memory store — the schema Flint proved, reusable by any runtime.

A small categorized JSON file of facts about the user, size-capped so the
whole thing can always ride inside a system prompt. Thread-safe, atomic
writes, oldest facts trimmed first when over budget.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from threading import Lock

CATEGORIES = ("identity", "preferences", "projects", "relationships",
              "places", "wishes", "notes")

MAX_VALUE_LENGTH = 380
DEFAULT_MAX_CHARS = 2200


class MemoryStore:
    def __init__(self, path: Path, max_chars: int = DEFAULT_MAX_CHARS):
        self._path = Path(path)
        self._max_chars = max_chars
        self._lock = Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> dict:
        empty = {category: {} for category in CATEGORIES}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(data, dict):
            return empty
        for category in CATEGORIES:
            data.setdefault(category, {})
        return data

    def _save(self, memory: dict) -> None:
        memory = self._trim(memory)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".mem-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(memory, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _trim(self, memory: dict) -> dict:
        if len(json.dumps(memory, ensure_ascii=False)) <= self._max_chars:
            return memory
        entries = [
            (category, key, entry.get("updated", "0000-00-00"))
            for category, items in memory.items()
            if isinstance(items, dict)
            for key, entry in items.items()
            if isinstance(entry, dict)
        ]
        entries.sort(key=lambda item: item[2])
        for category, key, _updated in entries:
            if len(json.dumps(memory, ensure_ascii=False)) <= self._max_chars:
                break
            del memory[category][key]
        return memory

    # ── mutation ─────────────────────────────────────────────────────────────
    def remember(self, category: str, key: str, value: str) -> str:
        if category not in CATEGORIES:
            category = "notes"
        value = str(value).strip()
        if not key or not value:
            return "nothing to save"
        if len(value) > MAX_VALUE_LENGTH:
            value = value[:MAX_VALUE_LENGTH].rstrip() + "…"
        with self._lock:
            memory = self.load()
            memory[category][key] = {
                "value": value,
                "updated": datetime.now().strftime("%Y-%m-%d"),
            }
            self._save(memory)
        return f"remembered {category}/{key}"

    def forget(self, category: str, key: str) -> str:
        with self._lock:
            memory = self.load()
            if key in memory.get(category, {}):
                del memory[category][key]
                self._save(memory)
                return f"forgot {category}/{key}"
        return f"not found: {category}/{key}"

    # ── prompt rendering ─────────────────────────────────────────────────────
    def render_for_prompt(self) -> str:
        memory = self.load()
        lines: list[str] = []
        titles = {
            "identity": None,  # rendered flat, no header
            "preferences": "Preferences:",
            "projects": "Active Projects / Goals:",
            "relationships": "People in their life:",
            "places": "Places they know / frequent:",
            "wishes": "Wishes / Plans:",
            "notes": "Other notes:",
        }
        for category in CATEGORIES:
            items = memory.get(category, {})
            if not items:
                continue
            header = titles[category]
            if header:
                lines.append("")
                lines.append(header)
            for key, entry in items.items():
                value = entry.get("value") if isinstance(entry, dict) else entry
                if not value:
                    continue
                label = key.replace("_", " ").title()
                lines.append(f"{label}: {value}" if header is None else f"  - {label}: {value}")
        if not lines:
            return ""
        header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
        return header + "\n".join(lines).strip() + "\n"
