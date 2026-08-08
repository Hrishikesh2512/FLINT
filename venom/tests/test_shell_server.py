"""The root shell: what it refuses, what it records, what it never leaks.

None of this is a security boundary — anything that reaches the socket is
already root. These are the guards against a typo ending the device and
against nobody being able to tell what happened afterwards.
"""

from __future__ import annotations

import json

import pytest

from venom.shell_server import (
    SHELL_ENV,
    AuditLog,
    RateLimiter,
    RootShell,
    forbidden_reason,
)


@pytest.fixture()
def audit(tmp_path):
    return AuditLog(str(tmp_path / "shell.log"))


def entries(audit) -> list[dict]:
    try:
        with open(audit._path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


# ── the unrecoverable few ───────────────────────────────────────────────────
@pytest.mark.parametrize("cmd,what", [
    ("rm -rf /", "delete the entire filesystem"),
    ("rm -rf /*", "delete the entire filesystem"),
    ("rm -fr /", "delete the entire filesystem"),
    ("sudo rm -rf --no-preserve-root /", "delete the entire filesystem"),
    ("mkfs.ext4 /dev/sda1", "reformat a filesystem"),
    ("mkfs -t ext4 /dev/sdb", "reformat a filesystem"),
    ("dd if=/dev/zero of=/dev/sda bs=1M", "overwrite a block device"),
    ("dd if=x.img of=/dev/mmcblk0", "overwrite a block device"),
    ("cat image.img > /dev/sda", "overwrite a block device"),
    (":(){ :|:& };:", "run a fork bomb"),
    ("chmod -R 777 /", "change permissions on the entire filesystem"),
])
def test_the_device_ending_commands_are_refused(cmd, what):
    assert forbidden_reason(cmd) == what


@pytest.mark.parametrize("cmd", [
    "rm -rf /home/pi/build",
    "rm -rf ./node_modules",
    "rm -rf /var/lib/venom/music",
    "apt-get install -y ffmpeg",
    "systemctl restart venom",
    "mkdir -p /opt/venom/data",
    "dd if=/dev/urandom of=./noise.bin bs=1M count=1",
    "chmod -R 755 /opt/venom",
    "journalctl -u venom -n 100",
    "df -h /",
    "ls /",
])
def test_ordinary_work_is_not_touched(cmd):
    """The guard is worthless if it stops the reason the shell exists."""
    assert forbidden_reason(cmd) == ""


def test_a_refused_command_does_not_run(audit):
    shell = RootShell(audit=audit)
    result = shell.run("rm -rf /")
    assert "refused" in result["out"]
    assert "delete the entire filesystem" in result["out"]
    assert entries(audit)[-1]["event"] == "refused"


def test_a_refusal_points_somewhere_it_can_still_be_done(audit):
    """It is the operator's device. The guard is a speed bump, not a wall —
    and pretending otherwise would just be annoying."""
    assert "over SSH" in RootShell(audit=audit).run("mkfs.ext4 /dev/sda")["out"]


# ── the record ──────────────────────────────────────────────────────────────
def test_every_command_is_logged_before_it_runs(audit, tmp_path):
    shell = RootShell(audit=audit)
    shell.cwd = str(tmp_path)
    shell.run("echo hello")
    logged = entries(audit)
    assert logged[-1]["event"] == "run"
    assert logged[-1]["cmd"] == "echo hello"
    assert str(tmp_path) in logged[-1]["detail"]


def test_a_command_that_never_returns_is_still_on_record(audit, tmp_path,
                                                         monkeypatch):
    """Logged before running, so a hang or a reboot still leaves a trace.

    Simulated by making the subprocess call itself blow up: if the write only
    happened afterwards, nothing would reach the log.
    """
    import venom.shell_server as server

    def never_returns(*args, **kwargs):
        raise KeyboardInterrupt("the box went down here")

    monkeypatch.setattr(server.subprocess, "run", never_returns)
    shell = RootShell(audit=audit)
    shell.cwd = str(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        shell.run("reboot now")
    assert entries(audit)[-1]["cmd"] == "reboot now"


def test_the_log_is_not_readable_by_the_console_user(audit, tmp_path):
    import os
    import sys

    audit.write("run", "whoami")
    if sys.platform.startswith("win"):
        pytest.skip("POSIX file modes only")
    assert os.stat(audit._path).st_mode & 0o077 == 0


def test_an_unwritable_log_never_stops_the_shell(tmp_path):
    audit = AuditLog(str(tmp_path))          # a directory: writing must fail
    shell = RootShell(audit=audit)
    assert shell.run("")["cwd"] == "/root"   # no exception


def test_long_commands_are_truncated_in_the_log(audit):
    audit.write("run", "x" * 5000)
    assert len(entries(audit)[-1]["cmd"]) == 500


# ── the environment ─────────────────────────────────────────────────────────
def test_the_shell_environment_carries_nothing_secret():
    """`env` in a browser terminal must not print whatever a future drop-in adds."""
    for key in SHELL_ENV:
        assert "KEY" not in key and "TOKEN" not in key and "SECRET" not in key
    assert set(SHELL_ENV) <= {"TERM", "HOME", "USER", "LOGNAME", "SHELL",
                              "PATH", "LANG"}


def test_the_real_environment_is_not_passed_through(audit, tmp_path, monkeypatch):
    """The shell used to inherit os.environ wholesale — so a key added to this
    unit later would have been printable from a browser terminal."""
    import venom.shell_server as server

    monkeypatch.setenv("GEMINI_API_KEY", "sk-should-never-reach-the-shell")
    seen = {}

    def capture(argv, **kwargs):
        seen.update(kwargs.get("env") or {})
        raise OSError("stop here")

    monkeypatch.setattr(server.subprocess, "run", capture)
    shell = RootShell(audit=audit)
    shell.cwd = str(tmp_path)
    shell.run("env")

    assert "GEMINI_API_KEY" not in seen
    assert seen["HOME"] == "/root"          # but a usable shell env is there


def test_the_environment_dict_is_copied_per_command(audit, tmp_path, monkeypatch):
    """Handing out the module dict itself would let one command mutate what
    every later command sees."""
    import venom.shell_server as server

    def capture(argv, **kwargs):
        (kwargs.get("env") or {})["INJECTED"] = "x"
        raise OSError("stop here")

    monkeypatch.setattr(server.subprocess, "run", capture)
    shell = RootShell(audit=audit)
    shell.cwd = str(tmp_path)
    shell.run("whoami")
    assert "INJECTED" not in server.SHELL_ENV


# ── rate limiting ───────────────────────────────────────────────────────────
def test_a_runaway_console_is_throttled():
    clock = [0.0]
    limiter = RateLimiter(limit=3, clock=lambda: clock[0])
    assert [limiter.allow() for _ in range(4)] == [True, True, True, False]


def test_the_window_slides():
    clock = [0.0]
    limiter = RateLimiter(limit=2, clock=lambda: clock[0])
    limiter.allow()
    limiter.allow()
    assert limiter.allow() is False
    clock[0] = 61.0
    assert limiter.allow() is True


def test_throttling_is_recorded(audit):
    clock = [0.0]
    shell = RootShell(audit=audit, limiter=RateLimiter(limit=1,
                                                       clock=lambda: clock[0]))
    shell.run("echo one")
    shell.run("echo two")
    assert entries(audit)[-1]["event"] == "throttled"


# ── the bits that already worked ────────────────────────────────────────────
def test_cd_persists_across_commands(tmp_path, audit):
    shell = RootShell(audit=audit)
    result = shell.run(f"cd {tmp_path}")
    assert result["cwd"] == str(tmp_path)
    assert shell.run("")["cwd"] == str(tmp_path)


def test_cd_to_nowhere_is_reported(audit):
    shell = RootShell(audit=audit)
    assert "not a directory" in shell.run("cd /no/such/place")["out"]


def test_cd_dash_goes_back(tmp_path, audit):
    # Two real directories rather than relying on /root, which does not exist
    # on the dev box this suite also runs on.
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    shell = RootShell(audit=audit)
    shell.run(f"cd {first}")
    shell.run(f"cd {second}")
    assert shell.run("cd -")["cwd"] == str(first)


def test_an_empty_command_does_nothing(audit):
    assert RootShell(audit=audit).run("   ") == {"out": "", "cwd": "/root"}
    assert entries(audit) == []
