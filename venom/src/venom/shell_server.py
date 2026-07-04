"""Venom privileged shell server — a real root terminal for the web console.

The web console's terminal runs as a thread of the `venom` daemon, which is
sealed by systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`): the
whole filesystem is read-only and `sudo` can never work, so `mkdir`/`apt` and
friends fail. This tiny service closes that gap. It runs as **root**, outside
the sandbox, and executes commands the console hands it over a Unix socket —
turning the browser terminal into a full-privilege shell.

Trust model: identical to the existing root control channel (`control.sh`).
The only thing standing between a request and root is the console PIN and the
LAN — anyone who can already reach the console terminal gets this. The socket
is group-`venom`, mode 0660, so only the console daemon can talk to it.

One shared shell session (single operator, personal device): the daemon owns
the working directory and handles `cd` itself, so state persists across the
console's per-command HTTP requests. Concurrent requests are serialized.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading

SOCK = "/run/venom-shell/shell.sock"
CMD_TIMEOUT = 300  # apt/pip on a Pi can be slow; block the console that long


class RootShell:
    """A persistent root shell: tracks cwd, handles `cd`, runs everything else
    through a bash login shell with full privileges."""

    def __init__(self) -> None:
        self.cwd = "/root"
        self.prev = "/root"

    def run(self, cmd: str) -> dict:
        cmd = (cmd or "").strip()
        if not cmd:
            return {"out": "", "cwd": self.cwd}

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

        try:
            r = subprocess.run(["/bin/bash", "-lc", cmd], cwd=self.cwd,
                               capture_output=True, text=True,
                               timeout=CMD_TIMEOUT,
                               env={**os.environ, "TERM": "xterm-256color",
                                    "HOME": "/root"})
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
