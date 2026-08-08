"""What is going on right now, gathered from wherever it lives.

Venom already knows the time, roughly where she is, and what's on the
calendar. What she has never known is what her user is actually *doing* — the
app in front of him, the repo he's in, the file he touched five minutes ago.
That is the difference between "what's on today?" and noticing he's been in
the same failing test file for an hour.

Three things make this awkward, and each one shapes the design:

  * **The sources live in different places.** Time is free, location is a
    cached network lookup, the foreground window is an OS call on the laptop.
    So sources are pluggable and each declares how expensive it is.

  * **Freshness varies enormously.** The clock changes every second; the git
    branch changes a few times a day. One refresh interval for everything
    either burns cycles or serves stale answers, so each source carries its
    own TTL and is only re-read when its own has expired.

  * **A probe can fail, and often will.** No window manager, no repo, the
    laptop asleep. A failing source must never take the prompt down with it,
    and — the part that is easy to get wrong — a source that has *started*
    failing must stop contributing rather than keep serving its last value
    forever. Stale context is worse than absent context: acting on where he
    was an hour ago is a confident, specific mistake.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("flint.context")

#: A cached reading older than its TTL by this multiple is dropped rather
#: than served. Beyond it the source is treated as unavailable.
STALE_MULTIPLE = 3.0


@dataclass(frozen=True)
class ContextItem:
    """One thing known about right now."""

    key: str
    label: str          # how it reads in a prompt: "Working in"
    value: str
    ts: float = 0.0

    def line(self) -> str:
        return f"{self.label}: {self.value}"


class ContextSource(Protocol):
    name: str
    ttl: float

    def read(self) -> ContextItem | None:
        ...


@dataclass
class _Cached:
    item: ContextItem | None = None
    read_at: float = 0.0
    failing: bool = False


@dataclass(frozen=True)
class Source:
    """A named probe with its own refresh interval."""

    name: str
    ttl: float
    read: Callable[[], ContextItem | None]


class ContextGatherer:
    """Reads what it can, caches per source, and never lets one break the rest."""

    def __init__(self, sources: Iterable[Source] = (),
                 clock: Callable[[], float] = time.time):
        self._sources = list(sources)
        self._clock = clock
        self._cache: dict[str, _Cached] = {}

    def add(self, source: Source) -> Source:
        self._sources.append(source)
        return source

    def __len__(self) -> int:
        return len(self._sources)

    def _fresh_enough(self, cached: _Cached, source: Source, now: float) -> bool:
        return (cached.item is not None
                and now - cached.read_at < source.ttl)

    def _too_stale(self, cached: _Cached, source: Source, now: float) -> bool:
        return now - cached.read_at > source.ttl * STALE_MULTIPLE

    def snapshot(self, refresh: bool = False) -> list[ContextItem]:
        """Everything currently known, freshest reading of each source."""
        now = self._clock()
        found: list[ContextItem] = []

        for source in self._sources:
            cached = self._cache.setdefault(source.name, _Cached())
            if not refresh and self._fresh_enough(cached, source, now):
                found.append(cached.item)
                continue
            try:
                item = source.read()
            except Exception as exc:        # noqa: BLE001 — a probe is not the app
                log.debug("context: %s failed: %s", source.name, exc)
                cached.failing = True
                item = None
            else:
                cached.failing = False

            if item is not None:
                cached.item = item
                cached.read_at = now
                found.append(item)
                continue

            # The read gave nothing. Serve the last good value only while it
            # is plausibly still true; past that, say nothing rather than
            # something confidently wrong.
            if cached.item is not None and not self._too_stale(cached, source, now):
                found.append(cached.item)
            else:
                cached.item = None
        return found

    def render_for_prompt(self, refresh: bool = False) -> str:
        """The context block, or "" when nothing is known.

        Empty when there is nothing to say — a block reading "Nothing known"
        spends tokens telling her she knows nothing.
        """
        items = self.snapshot(refresh)
        if not items:
            return ""
        lines = ["[RIGHT NOW — what he's actually doing. Use it to be "
                 "specific and to time things well; don't read it back at him.]"]
        lines += [f"- {item.line()}" for item in items]
        return "\n".join(lines) + "\n"

    def failing(self) -> list[str]:
        return sorted(name for name, cached in self._cache.items() if cached.failing)


# ── sources that need no platform at all ─────────────────────────────────────
def clock_source(clock: Callable[[], float] = time.time) -> Source:
    def read() -> ContextItem:
        now = clock()
        local = time.localtime(now)
        return ContextItem(key="time", label="Time",
                           value=time.strftime("%A %H:%M", local), ts=now)

    return Source(name="time", ttl=30.0, read=read)


def git_project_source(directory: str | Path, runner=None) -> Source:
    """Which repo and branch he's in — the best single proxy for "what he's on"."""
    def read() -> ContextItem | None:
        from flint_core.vcs import GitRepo

        repo = GitRepo(directory, runner=runner) if runner else GitRepo(directory)
        if not repo.is_repo():
            return None
        branch = repo.branch()
        if not branch:
            return None
        # Resolved, not just expanded: Path(".").name is "", so a gatherer
        # pointed at the working directory would report " on v2/rebuild".
        name = Path(directory).expanduser().resolve().name
        changed = len(repo.changed_files())
        value = f"{name} on {branch}"
        if changed:
            value += f", {changed} file(s) changed"
        return ContextItem(key="project", label="Working in", value=value)

    # A branch changes a few times a day; re-shelling out every prompt is waste.
    return Source(name="project", ttl=120.0, read=read)


def recent_files_source(directories: Sequence[str | Path], within_minutes: float = 90.0,
                        limit: int = 3, clock: Callable[[], float] = time.time) -> Source:
    """What he's touched lately — the files, not the apps."""
    def read() -> ContextItem | None:
        cutoff = clock() - within_minutes * 60
        found: list[tuple[float, str]] = []
        for directory in directories:
            path = Path(directory).expanduser()
            try:
                for entry in path.iterdir():
                    if not entry.is_file() or entry.name.startswith("."):
                        continue
                    modified = entry.stat().st_mtime
                    if modified >= cutoff:
                        found.append((modified, entry.name))
            except OSError:
                continue
        if not found:
            return None
        found.sort(reverse=True)
        return ContextItem(key="files", label="Recently edited",
                           value=", ".join(name for _, name in found[:limit]))

    return Source(name="files", ttl=90.0, read=read)


def active_window_source() -> Source:
    """The app in front of him. Windows via ctypes; nothing elsewhere.

    Returns None rather than guessing on platforms without a probe — an
    assistant that says "you're in Chrome" on a machine it cannot see is
    worse than one that says nothing.
    """
    def read() -> ContextItem | None:
        title = _foreground_window_title()
        if not title:
            return None
        return ContextItem(key="app", label="On screen", value=title[:120])

    return Source(name="app", ttl=20.0, read=read)


def _foreground_window_title() -> str:
    import sys

    if not sys.platform.startswith("win"):
        # macOS needs AppleScript and Linux needs a window manager call; both
        # are real work and neither belongs in a cross-platform core module.
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.GetForegroundWindow()
        if not handle:
            return ""
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value.strip()
    except Exception:                       # noqa: BLE001 — no window station, etc.
        return ""


def build_gatherer(*, project_dir: str | Path = "",
                   watch_dirs: Sequence[str | Path] = (),
                   include_time: bool = False,
                   include_window: bool = True) -> ContextGatherer:
    """The usual set, skipping anything this device can't answer.

    `include_time` is off by default because the voice prompt already carries
    the date and time — two clocks in one prompt is one too many.
    """
    sources: list[Source] = []
    if include_time:
        sources.append(clock_source())
    if include_window:
        sources.append(active_window_source())
    if project_dir:
        sources.append(git_project_source(project_dir))
    if watch_dirs:
        sources.append(recent_files_source(watch_dirs))
    return ContextGatherer(sources)
