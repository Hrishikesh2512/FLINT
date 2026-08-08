"""Who may do what, and a record of everything that was actually done.

Until now every tool in the registry was dispatchable by anything that could
reach the registry, and nothing anywhere wrote down that it happened. That was
survivable while every action was a conversation the user was present for. It
stopped being survivable the moment jobs started running unattended, on a
device whose shell server is root behind a PIN.

Three pieces, deliberately small:

    Policy    what this device is allowed to do — default deny
    AuditLog  append-only record of every guarded call, allowed or refused
    guarded() wraps a ToolRegistry so both apply to every dispatch

**Default deny.** A permission that was never granted is refused. The
alternative — allow anything not explicitly forbidden — means every new
capability silently arrives with full access, which is how this class of
problem happens in the first place.

**Refusals are values, not exceptions.** A denied tool returns a plain
sentence the model can read out, because the caller is usually a language
model mid-conversation and a traceback helps nobody. The audit log is where
the detail lives.

**The audit log is not security.** It is on the same disk as everything else
and anyone with root can rewrite it. It exists so that "what did she do while
I was asleep?" has an answer, which is the question that actually gets asked.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from flint_core.tools.registry import ToolRegistry, UnknownToolError

log = logging.getLogger("flint.permissions")

#: Permissions that grant reach beyond the device itself. Not special-cased in
#: the code — listed so a config review has something concrete to look at.
SENSITIVE = frozenset({
    "shell", "files", "remote_control", "messaging", "emergency",
    "home_control", "personal_data", "location",
})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    #: The permissions that caused a refusal — empty when allowed.
    missing: tuple[str, ...] = ()

    def reason(self) -> str:
        """The refusal, phrased to be said out loud.

        Deliberately does not name the tool: the caller is a voice assistant
        mid-conversation, and "I'm not allowed to call send_whatsapp" is not
        how a person talks. The tool name goes in the audit log, where someone
        debugging can actually use it.
        """
        if self.allowed:
            return ""
        needed = ", ".join(self.missing)
        return (f"I'm not allowed to do that — it needs permission for "
                f"{needed}, which isn't switched on.")


class Policy:
    """What this device may do. Default deny: ungranted is refused.

    `denied` is an explicit blocklist that beats `granted`, so a permission
    can be switched off for a device without editing the grant list it shares
    with every other device.
    """

    def __init__(self, granted: Iterable[str] = (), denied: Iterable[str] = ()):
        self._granted = frozenset(p.strip() for p in granted if p.strip())
        self._denied = frozenset(p.strip() for p in denied if p.strip())

    @property
    def granted(self) -> tuple[str, ...]:
        return tuple(sorted(self._granted - self._denied))

    @property
    def denied(self) -> tuple[str, ...]:
        return tuple(sorted(self._denied))

    def allows(self, permission: str) -> bool:
        return permission in self._granted and permission not in self._denied

    def check(self, permissions: Sequence[str]) -> Decision:
        """All-or-nothing: a tool needing three permissions needs all three."""
        missing = tuple(sorted(p for p in permissions if not self.allows(p)))
        return Decision(allowed=not missing, missing=missing)

    def describe(self) -> str:
        granted = ", ".join(self.granted) or "nothing"
        line = f"allowed: {granted}"
        if self._denied:
            line += f"; explicitly denied: {', '.join(self.denied)}"
        return line

    @classmethod
    def permissive(cls, capabilities=None) -> Policy:
        """Grant whatever the active capabilities ask for.

        The honest default for a single-user personal device: the point is a
        record of what happened and a switch to turn things off, not keeping
        the owner out of their own assistant. Pass nothing and it grants the
        full known set.
        """
        if capabilities is None:
            return cls(granted=SENSITIVE)
        return cls(granted=capabilities.permissions())


class AuditLog:
    """Append-only JSONL of every guarded call. Newest last.

    JSONL rather than a database because the whole value is being readable
    with `tail` on a device with no screen, and appending one line is the only
    write it ever does.
    """

    #: Lines kept. Rotation rewrites the file, so this is a bound on disk use
    #: on a Pi, not a retention policy anyone should rely on.
    MAX_LINES = 5000

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self._path = Path(path) if path else None
        self._clock = clock
        self._lock = Lock()
        self._lines_written = 0

    @property
    def path(self) -> Path | None:
        return self._path

    def record(self, action: str, *, allowed: bool, actor: str = "voice",
               permissions: Sequence[str] = (), detail: str = "") -> None:
        """Write one entry. Never raises — an unwritable log must not stop work."""
        entry = {
            "ts": round(self._clock(), 3),
            "actor": actor,
            "action": action,
            "allowed": bool(allowed),
            "permissions": list(permissions),
        }
        if detail:
            entry["detail"] = detail[:400]
        if not allowed:
            log.warning("audit: REFUSED %s (%s) for %s",
                        action, ", ".join(permissions) or "-", actor)
        if self._path is None:
            return
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._lines_written += 1
                if self._lines_written >= 200:
                    self._lines_written = 0
                    self._rotate()
        except OSError:
            log.warning("audit: could not write to %s", self._path)

    def _rotate(self) -> None:
        """Keep the newest MAX_LINES. Called under the lock."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= self.MAX_LINES:
            return
        kept = lines[-self.MAX_LINES:]
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".audit-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(kept) + "\n")
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def recent(self, limit: int = 20, *, refused_only: bool = False) -> list[dict]:
        if self._path is None:
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue          # a torn line from a power cut: skip, don't die
            if refused_only and entry.get("allowed", True):
                continue
            entries.append(entry)
        return entries[-limit:]

    def summary(self, limit: int = 5) -> str:
        """A spoken answer to "what have you been doing?"."""
        entries = self.recent(limit)
        if not entries:
            return "I haven't done anything worth logging yet."
        parts = []
        for entry in entries:
            when = time.strftime("%I:%M %p", time.localtime(entry.get("ts", 0)))
            mark = "" if entry.get("allowed") else " (refused)"
            parts.append(f"{when.lstrip('0')} {entry.get('action', '?')}{mark}")
        return "Recently: " + "; ".join(parts) + "."


class GuardedRegistry:
    """A ToolRegistry that checks permissions and audits before dispatching.

    Wraps rather than subclasses so everything that reads a registry — the
    Gemini declarations, the planner docs, `in` — keeps working untouched on
    the real one underneath.
    """

    def __init__(self, registry: ToolRegistry, policy: Policy,
                 audit: AuditLog | None = None, actor: str = "voice"):
        self._registry = registry
        self._policy = policy
        self._audit = audit or AuditLog(None)
        self._actor = actor

    def __getattr__(self, name: str) -> Any:
        # gemini_declarations, openai_tools, names, get, planner_documentation...
        return getattr(self._registry, name)

    def __contains__(self, name: str) -> bool:
        return name in self._registry

    def __iter__(self):
        return iter(self._registry)

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def audit(self) -> AuditLog:
        return self._audit

    def dispatch(self, name: str, args: dict[str, Any] | None = None,
                 **extra: Any) -> Any:
        try:
            spec = self._registry.get(name)
        except UnknownToolError:
            self._audit.record(name, allowed=False, actor=self._actor,
                               detail="no such tool")
            raise

        decision = self._policy.check(spec.permissions)
        if not decision.allowed:
            self._audit.record(name, allowed=False, actor=self._actor,
                               permissions=spec.permissions,
                               detail=f"missing {', '.join(decision.missing)}")
            # A sentence, not an exception: the caller is usually a model
            # mid-sentence, and it needs something it can say out loud.
            return decision.reason()

        detail = ", ".join(f"{k}={v}" for k, v in dict(args or {}).items())
        self._audit.record(name, allowed=True, actor=self._actor,
                           permissions=spec.permissions, detail=detail)
        return self._registry.dispatch(name, args, **extra)


def guarded(registry: ToolRegistry, policy: Policy,
            audit: AuditLog | None = None, actor: str = "voice") -> GuardedRegistry:
    """Wrap `registry` so every dispatch is checked and recorded."""
    return GuardedRegistry(registry, policy, audit, actor)
