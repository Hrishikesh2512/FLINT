"""Venom privileged shell server — a real root terminal for the web console.

The web console's terminal runs as a thread of the `venom` daemon, which is
sealed by systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`): the
whole filesystem is read-only and `sudo` can never work, so `mkdir`/`apt` and
friends fail. This tiny service closes that gap. It runs as **root**, outside
the sandbox, and executes commands the console hands it over a Unix socket —
turning the browser terminal into a full-privilege shell.

Trust model: the socket is group-`venom`, mode 0660, so only the console
daemon can talk to it, and the console itself is loopback-only behind a PIN.
That is the boundary. What follows does **not** add a second one — anything
that can reach this socket can still do anything root can do, and pretending
otherwise would be worse than not trying.

What it does add is the three things whose absence actually hurt:

  * **A record.** Every command is written to an append-only log *before* it
    runs, so one that hangs, reboots the Pi or wipes the disk still leaves a
    trace. "What happened while I was asleep" had no answer at all before.

  * **A stop on the unrecoverable few.** `rm -rf /`, `mkfs`, `dd` onto a
    block device. Not security — anyone determined can spell them
    differently — but these are the commands whose accidental form ends the
    device, and a typo should not be enough. Same reasoning as the refusals
    in `flint_core.vcs`.

  * **A clean environment.** The shell used to inherit this service's whole
    environment. Nothing secret lives there today, but `env` in a browser
    terminal printing whatever a future systemd drop-in adds is a leak
    waiting to be introduced, so only what a shell needs is passed.

Deliberately still stdlib-only: this is the one process on the device running
as unsandboxed root, and every import is attack surface. The audit log here is
twenty lines rather than a reuse of `flint_core.permissions.AuditLog` for
exactly that reason.

One shared shell session (single operator, personal device): the daemon owns
the working directory and handles `cd` itself, so state persists across the
console's per-command HTTP requests. Concurrent requests are serialized.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

SOCK = "/run/venom-shell/shell.sock"
AUDIT = "/var/log/venom-shell.log"
CMD_TIMEOUT = 300  # apt/pip on a Pi can be slow; block the console that long

#: Commands whose accidental form ends the device. Refused outright.
#:
#: This is a typo guard, not a sandbox, and the patterns are written to catch
#: the *canonical* dangerous form without touching legitimate work: `rm -rf /`
#: is refused, `rm -rf /home/pi/tmp` is not.
FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(-\S+\s+)*-?[a-z]*[rf][a-z]*\s+(-\S+\s+)*/\s*(\*|$|;|&)"),
     "delete the entire filesystem"),
    (re.compile(r"--no-preserve-root"), "delete the entire filesystem"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "reformat a filesystem"),
    (re.compile(r"\bdd\b[^|;]*\bof=/dev/(sd|hd|mmcblk|nvme|vd)"),
     "overwrite a block device"),
    (re.compile(r">\s*/dev/(sd|hd|mmcblk|nvme|vd)\w"), "overwrite a block device"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;?\s*:"), "run a fork bomb"),
    (re.compile(r"\bchmod\s+-R\s+[0-7]{3,4}\s+/\s*($|;|&)"),
     "change permissions on the entire filesystem"),
)

#: Environment handed to the shell. Everything else is dropped, so a secret
#: added to this unit later cannot be read out of a browser terminal.
SHELL_ENV = {
    "TERM": "xterm-256color",
    "HOME": "/root",
    "USER": "root",
    "LOGNAME": "root",
    "SHELL": "/bin/bash",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": os.environ.get("LANG", "C.UTF-8"),
}

#: Commands per minute. A console that has been taken over should not be able
#: to grind the Pi to a halt before anyone notices the log filling up.
RATE_LIMIT = 60


def forbidden_reason(cmd: str) -> str:
    """Why this command is refused, or "" if it may run."""
    for pattern, what in FORBIDDEN:
        if pattern.search(cmd):
            return what
    return ""


class AuditLog:
    """Append-only record of every command. Never raises."""

    def __init__(self, path: str = AUDIT):
        self._path = path
        self._lock = threading.Lock()

    def write(self, event: str, cmd: str, detail: str = "") -> None:
        entry = {"ts": round(time.time(), 3), "event": event,
                 "cmd": cmd[:500]}
        if detail:
            entry["detail"] = detail[:200]
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                # Root-only: the log records root's actions and the console
                # daemon has no business editing it.
                os.chmod(self._path, 0o600)
        except OSError:
            pass        # a log that cannot be written must not stop the shell


class RateLimiter:
    """A sliding minute. Cheap, and enough to make a runaway console obvious."""

    def __init__(self, limit: int = RATE_LIMIT, clock=time.monotonic):
        self._limit = limit
        self._clock = clock
        self._hits: list[float] = []

    def allow(self) -> bool:
        now = self._clock()
        self._hits = [t for t in self._hits if now - t < 60.0]
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(now)
        return True


class RootShell:
    """A persistent root shell: tracks cwd, handles `cd`, runs everything else
    through a bash login shell with full privileges."""

    def __init__(self, audit: AuditLog | None = None,
                 limiter: RateLimiter | None = None) -> None:
        self.cwd = "/root"
        self.prev = "/root"
        self.audit = audit if audit is not None else AuditLog()
        self.limiter = limiter if limiter is not None else RateLimiter()

    def run(self, cmd: str) -> dict:
        cmd = (cmd or "").strip()
        if not cmd:
            return {"out": "", "cwd": self.cwd}

        if not self.limiter.allow():
            self.audit.write("throttled", cmd)
            return {"out": "[too many commands — slow down]", "cwd": self.cwd}

        refused = forbidden_reason(cmd)
        if refused:
            self.audit.write("refused", cmd, refused)
            return {"out": f"[refused: this would {refused}. If you really "
                           f"mean it, do it over SSH — not from here.]",
                    "cwd": self.cwd}

        # cd is a shell builtin — subprocess can't persist it, so handle it.
        if cmd == "cd" or cmd.startswith("cd "):
            target = cmd[2:].strip() or "/root"
            if target == "-":
                target = self.prev
            new = os.path.normpath(
                os.path.join(self.cwd, os.path.expanduser(target)))
            if os.path.isdir(new):
                self.prev, self.cwd = self.cwd, new
                return {"out": "", "cwd": self.cwd}
            return {"out": f"cd: {target}: not a directory", "cwd": self.cwd}

        # Logged before it runs, not after: a command that hangs, reboots the
        # Pi, or wipes the disk never reaches an "after".
        self.audit.write("run", cmd, f"cwd={self.cwd}")
        try:
            r = subprocess.run(["/bin/bash", "-lc", cmd], cwd=self.cwd,
                               capture_output=True, text=True,
                               timeout=CMD_TIMEOUT,
                               env=dict(SHELL_ENV))
            out = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            out = f"[timed out after {CMD_TIMEOUT}s]"
        except Exception as exc:  # noqa: BLE001 — surface anything to the console
            out = f"[error: {exc}]"
        return {"out": out[-20000:], "cwd": self.cwd}


def _handle(conn: socket.socket, shell: RootShell, lock: threading.Lock) -> None:
    with conn:
        f = conn.makefile("rwb")
        line = f.readline()
        if not line:
            return
        try:
            req = json.loads(line)
        except ValueError:
            return
        with lock:  # one command at a time keeps cwd coherent
            resp = shell.run(str(req.get("cmd", "")))
        f.write((json.dumps(resp) + "\n").encode())
        f.flush()


def serve(path: str = SOCK) -> None:
    shell = RootShell()
    lock = threading.Lock()
    shell.audit.write("started", "", f"socket={path}")

    try:
        os.unlink(path)  # clear a stale socket from an unclean shutdown
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o660)
    try:  # let the (unprivileged) console daemon connect, nobody else
        shutil.chown(path, group="venom")
    except (LookupError, PermissionError, OSError):
        pass
    srv.listen(8)

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_handle, args=(conn, shell, lock),
                         daemon=True).start()


if __name__ == "__main__":
    serve()
