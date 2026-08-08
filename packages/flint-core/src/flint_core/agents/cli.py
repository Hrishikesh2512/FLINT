"""Driving a headless coding CLI as an agent — Claude Code, Codex, Gemini CLI.

These tools already do the hard part: they read a repo, plan, edit files, run
tests, and fix what they broke. What they lack is a way to be *called* by
something else and to report back in a form a voice assistant can use. That is
all this is.

**The command line is configuration, not code.** Each of these CLIs has its
own flags and they change between releases; a table of them hard-coded here
would be wrong within weeks and wrong silently. So the argv is supplied by the
host, with `{goal}` substituted:

    [[agent]]
    name    = "claude"
    command = ["claude", "-p", "{goal}"]
    good_at = ["code"]

`CLAUDE_CODE_DEFAULT` below is the one default shipped, because `claude -p
"<prompt>"` is Claude Code's documented headless mode. Anything else you
configure yourself — check `--help` for the version you actually have rather
than trusting a guess written here.

**Progress is streamed, not collected.** stdout is read line by line and each
line goes to `request.progress` as it arrives, which is the whole difference
between this and the one-shot `laptop_task` RPC it replaces: a five-minute
refactor can say what it is doing while it does it.

**What changed is observed, not claimed.** When the working directory is a git
repo, the porcelain status is compared before and after, so the artifact list
is what actually changed on disk rather than what the model said it did.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from flint_core.agents.base import AgentRequest, AgentResult, AgentSpec

log = logging.getLogger("flint.agents.cli")

#: Claude Code's headless/print mode. The one default worth shipping.
CLAUDE_CODE_DEFAULT: tuple[str, ...] = ("claude", "-p", "{goal}")

#: Lines longer than this are truncated before being reported as progress —
#: a CLI dumping a whole file into stdout must not flood the caller.
MAX_PROGRESS_LINE = 300
#: Total output retained for `detail`. Beyond this the middle is dropped.
MAX_DETAIL = 20_000


@dataclass(frozen=True)
class CLIAgentConfig:
    name: str
    command: tuple[str, ...]
    summary: str = ""
    good_at: frozenset[str] = field(default_factory=frozenset)
    cwd: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 600.0
    success_exit_codes: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a CLI agent needs a name")
        if not self.command:
            raise ValueError(f"agent {self.name!r}: command must not be empty")
        if not any("{goal}" in part for part in self.command):
            raise ValueError(
                f"agent {self.name!r}: command has no {{goal}} placeholder — "
                f"the agent would run the same thing whatever it was asked")


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n… [{dropped} characters omitted] …\n{text[-half:]}"


def _git_status(cwd: Path) -> set[str] | None:
    """Porcelain status as a set, or None when this isn't a git repo."""
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(cwd),
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def _changed_files(before: set[str] | None, after: set[str] | None) -> tuple[str, ...]:
    if before is None or after is None:
        return ()
    # Porcelain lines are "XY path"; take the path half of anything new.
    changed = []
    for line in sorted(after - before):
        parts = line.split(maxsplit=1)
        changed.append(parts[1] if len(parts) > 1 else line)
    return tuple(changed)


class CLIAgent:
    """One headless CLI, presented as an Agent."""

    def __init__(self, config: CLIAgentConfig):
        self.config = config
        self.name = config.name

    # ── availability ────────────────────────────────────────────────────────
    def installed(self) -> bool:
        """Is the executable actually on this machine?

        Checked at registration so an agent that isn't installed shows up as
        unavailable rather than failing the first time someone asks for it.
        """
        from shutil import which

        return which(self.config.command[0]) is not None

    # ── running ─────────────────────────────────────────────────────────────
    def _argv(self, request: AgentRequest) -> list[str]:
        cwd = request.cwd or self.config.cwd
        return [part.replace("{goal}", request.goal).replace("{cwd}", cwd)
                for part in self.config.command]

    def run(self, request: AgentRequest) -> AgentResult:
        cwd = Path(request.cwd or self.config.cwd or ".").expanduser()
        if not cwd.is_dir():
            return AgentResult.failed(f"no such directory: {cwd}", agent=self.name)

        argv = self._argv(request)
        env = {**os.environ, **self.config.env}
        timeout = min(request.timeout, self.config.timeout)
        before = _git_status(cwd)

        request.progress(f"{self.name}: starting")
        log.info("agent %s: %s (cwd=%s)", self.name, argv[0], cwd)
        try:
            process = subprocess.Popen(
                argv, cwd=str(cwd), env=env,
                stdout=subprocess.PIPE,
                # Merged rather than a second pipe: two pipes read serially is
                # the classic way to deadlock a chatty subprocess.
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, errors="replace", bufsize=1,
            )
        except (OSError, ValueError) as exc:
            return AgentResult.failed(f"could not start {argv[0]}: {exc}",
                                      agent=self.name)

        # Draining happens on its own thread so the timeout is enforced by
        # process.wait() rather than by the read loop. Checking the clock
        # inside `for line in stdout` looks equivalent and is not: that
        # iterator blocks until the *next* line, so an agent that hangs
        # silently — the case the timeout exists for — is never noticed.
        lines: list[str] = []

        def drain() -> None:
            try:
                for raw in process.stdout:            # type: ignore[union-attr]
                    line = raw.rstrip("\n")
                    lines.append(line)
                    if line.strip():
                        request.progress(line.strip()[:MAX_PROGRESS_LINE])
            except (OSError, ValueError):
                pass                                   # pipe closed under us

        reader = threading.Thread(target=drain, name=f"agent-{self.name}",
                                  daemon=True)
        reader.start()

        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._stop(process)
            code = -1
        reader.join(timeout=5)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass

        output = "\n".join(lines).strip()
        artifacts = _changed_files(before, _git_status(cwd))

        if timed_out:
            return AgentResult(
                ok=False, agent=self.name,
                summary=f"{self.name} was still working after "
                        f"{timeout / 60:.0f} minutes, so I stopped it.",
                detail=_truncate_middle(output, MAX_DETAIL),
                artifacts=artifacts,
                error=f"timed out after {timeout:.0f}s")

        ok = code in self.config.success_exit_codes
        return AgentResult(
            ok=ok, agent=self.name,
            summary=self._summarise(ok, output, artifacts),
            detail=_truncate_middle(output, MAX_DETAIL),
            artifacts=artifacts,
            error="" if ok else f"{self.name} exited with code {code}")

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        """Ask, then insist. A hung coding agent must not outlive the request."""
        try:
            process.terminate()
            process.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass

    def _summarise(self, ok: bool, output: str, artifacts: Sequence[str]) -> str:
        """A sentence someone can hear, built from what actually happened."""
        if artifacts:
            count = len(artifacts)
            shown = ", ".join(artifacts[:3])
            more = f" and {count - 3} more" if count > 3 else ""
            changed = f"{count} file{'s' if count != 1 else ''} changed: {shown}{more}"
        else:
            changed = "nothing changed on disk"
        if not ok:
            tail = " ".join(output.splitlines()[-3:])[:200]
            return f"{self.name} didn't finish cleanly — {tail or changed}"
        # The last non-empty line of a coding CLI is usually its own summary,
        # which beats anything synthesised from the exit code.
        last = next((ln.strip() for ln in reversed(output.splitlines())
                     if ln.strip()), "")
        return f"{last[:400]} ({changed})" if last else f"Done — {changed}"


def cli_agent_spec(config: CLIAgentConfig, *, priority: int = 0,
                   require_installed: bool = True) -> AgentSpec:
    """Register a CLI agent, marked unavailable when it isn't installed."""
    agent = CLIAgent(config)
    available = agent.installed() if require_installed else True
    if not available:
        log.info("agent %s: %r not found on PATH — unavailable",
                 config.name, config.command[0])
    return AgentSpec(
        name=config.name,
        summary=config.summary or f"Runs {config.command[0]}.",
        run=agent.run,
        good_at=config.good_at,
        available=available,
        # A coding agent edits files and runs whatever the repo tells it to.
        # There is no smaller honest permission set than this.
        permissions=("shell", "files"),
        priority=priority,
    )


def agents_from_config(entries: Sequence[Mapping]) -> list[AgentSpec]:
    """Build agent specs from plain config dicts (TOML `[[agent]]` blocks).

    A malformed entry is skipped with a warning rather than taking the process
    down — a typo in the agent table must not stop the assistant booting.
    """
    specs = []
    for entry in entries or ():
        try:
            specs.append(cli_agent_spec(CLIAgentConfig(
                name=str(entry.get("name", "")).strip(),
                command=tuple(str(part) for part in entry.get("command", ())),
                summary=str(entry.get("summary", "")).strip(),
                good_at=frozenset(str(t).strip().lower()
                                  for t in entry.get("good_at", ()) if str(t).strip()),
                cwd=str(entry.get("cwd", "")).strip(),
                timeout=float(entry.get("timeout", 600.0)),
            ), priority=int(entry.get("priority", 0))))
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning("agents: skipping bad entry %r (%s)", entry, exc)
    return specs
