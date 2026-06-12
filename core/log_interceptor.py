"""Log interceptor: tees stdout/stderr into subscribers + a history buffer.

``install()`` wraps ``sys.stdout`` and ``sys.stderr``.  Everything any part of
the app (or any third-party library) prints still reaches the real console,
but each completed line is also classified and fanned out to subscribers —
the in-app terminal panel and the WebSocket broadcaster.

Subscribers receive ``(line: str, level: str, stream: str)`` and must be fast
and exception-safe from their own point of view; a failing subscriber is
silently dropped from that emission (never breaks the printing code path).
"""

from __future__ import annotations

import io
import re
import sys
import threading
import time
from collections import deque
from typing import Callable

Subscriber = Callable[[str, str, str], None]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_LEVEL_PATTERNS = [
    ("error", re.compile(r"\b(error|err:|failed|exception|traceback|❌)\b", re.I)),
    ("warn",  re.compile(r"\b(warn|warning|⚠)", re.I)),
    ("ok",    re.compile(r"(✅|✓|\bsuccess\b|\bconnected\b|\bonline\b)", re.I)),
]


def classify(line: str) -> str:
    for level, pat in _LEVEL_PATTERNS:
        if pat.search(line):
            return level
    return "info"


class _Tee(io.TextIOBase):
    def __init__(self, original, stream_name: str, hub: "LogHub"):
        self._orig = original
        self._name = stream_name
        self._hub = hub
        self._buf = ""
        self._lock = threading.Lock()

    # io.TextIOBase plumbing -------------------------------------------------
    @property
    def encoding(self):
        return getattr(self._orig, "encoding", "utf-8")

    def writable(self):
        return True

    def fileno(self):
        return self._orig.fileno()

    def isatty(self):
        try:
            return self._orig.isatty()
        except Exception:
            return False

    # the actual tee ---------------------------------------------------------
    def write(self, text):
        try:
            self._orig.write(text)
        except UnicodeEncodeError:
            # cp1252 console choking on emoji — degrade instead of dropping.
            try:
                enc = getattr(self._orig, "encoding", "ascii") or "ascii"
                self._orig.write(str(text).encode(enc, "replace").decode(enc))
            except Exception:
                pass
        except Exception:
            pass
        with self._lock:
            self._buf += str(text)
            had_line = "\n" in self._buf
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._hub.dispatch(line, self._name)
        if had_line:
            # keep redirected/piped stdout live even if the process is killed
            try:
                self._orig.flush()
            except Exception:
                pass
        return len(text)

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass


class LogHub:
    def __init__(self, history_size: int = 2000):
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()
        self.history: deque[tuple[float, str, str, str]] = deque(maxlen=history_size)
        self._installed = False
        self._orig_out = None
        self._orig_err = None

    def dispatch(self, raw_line: str, stream: str) -> None:
        line = _ANSI_RE.sub("", raw_line).rstrip()
        if not line.strip():
            return
        level = "error" if stream == "stderr" else classify(line)
        entry = (time.time(), line, level, stream)
        with self._lock:
            self.history.append(entry)
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(line, level, stream)
            except Exception:
                pass

    def subscribe(self, cb: Subscriber) -> None:
        with self._lock:
            self._subs.append(cb)

    def unsubscribe(self, cb: Subscriber) -> None:
        with self._lock:
            try:
                self._subs.remove(cb)
            except ValueError:
                pass

    def snapshot(self) -> list[tuple[float, str, str, str]]:
        with self._lock:
            return list(self.history)

    # ── stream replacement ───────────────────────────────────────────────────
    def install(self) -> None:
        if self._installed:
            return
        self._orig_out, self._orig_err = sys.stdout, sys.stderr
        # Frozen/windowed builds can have None streams — substitute a sink.
        out = self._orig_out if self._orig_out else io.StringIO()
        err = self._orig_err if self._orig_err else io.StringIO()
        sys.stdout = _Tee(out, "stdout", self)
        sys.stderr = _Tee(err, "stderr", self)
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        sys.stdout, sys.stderr = self._orig_out, self._orig_err
        self._installed = False


_hub: LogHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> LogHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = LogHub()
        return _hub


def install() -> LogHub:
    hub = get_hub()
    hub.install()
    return hub


if __name__ == "__main__":
    hub = install()
    hub.subscribe(lambda line, lvl, st: None)
    print("plain info line")
    print("⚠️ a warning happened")
    print("❌ something failed")
    for ts, line, level, stream in hub.snapshot():
        safe = line.encode("ascii", "replace").decode("ascii")
        print(f"  [{level:5s}] {safe}", file=sys.__stdout__)
