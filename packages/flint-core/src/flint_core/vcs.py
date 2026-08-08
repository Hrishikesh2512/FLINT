"""Git, as something an assistant may safely be given.

Most of git is harmless and a few parts are not, and the difference is not
where you would guess. Reading history, staging, committing to a branch — all
recoverable. Force-pushing, resetting hard, deleting a branch, rewriting
published history — not. An autonomous agent that can do the second set will
eventually do the second set.

So this module offers the first set and refuses the second, by name, at the
bottom (`FORBIDDEN`). It is not a sandbox — anything with shell access can run
git directly — but it means the *tools* an assistant is handed cannot destroy
work, and the refusal is a value it can read out rather than an exception.

Two more deliberate choices:

  * **Never commit on a detached HEAD or an unknown branch state.** A commit
    that lands nowhere is worse than no commit, because it looks like it
    worked.
  * **Never `git add -A` blindly.** The default stages tracked changes only;
    untracked files must be named. An agent that stages everything commits
    the `.env` sitting in the working directory sooner or later.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("flint.vcs")

DEFAULT_TIMEOUT = 60.0

#: Operations no assistant-facing tool will run, whatever it is asked. Matched
#: against the whole argument list, so "push --force-with-lease" is caught too.
FORBIDDEN = (
    "--force", "-f", "--hard", "--force-with-lease",
    "reset", "rebase", "filter-branch", "reflog", "gc", "prune",
)

#: Branches that must never be committed to directly by an agent.
PROTECTED_BRANCHES = frozenset({"main", "master", "production", "release"})

#: Files that must never be staged by name, however they are asked for.
SECRET_PATTERNS = (
    re.compile(r"(^|/)\.env(\..*)?$"),
    re.compile(r"(^|/)(id_rsa|id_ed25519)$"),
    re.compile(r"\.(pem|key|p12|pfx|keystore)$"),
    re.compile(r"(^|/)(credentials|secrets?)\.(json|ya?ml|toml)$"),
)


class GitError(Exception):
    pass


def looks_secret(path: str) -> bool:
    candidate = path.replace("\\", "/").strip()
    return any(pattern.search(candidate) for pattern in SECRET_PATTERNS)


@dataclass(frozen=True)
class GitResult:
    ok: bool
    output: str
    error: str = ""

    @property
    def text(self) -> str:
        return self.output if self.ok else (self.error or self.output)


class GitRepo:
    """Read-mostly git access to one working tree."""

    def __init__(self, path: str | Path, timeout: float = DEFAULT_TIMEOUT,
                 runner=None):
        self.path = Path(path).expanduser()
        self._timeout = timeout
        # Injected so tests never shell out; production uses subprocess.
        self._runner = runner or self._subprocess_runner

    # ── plumbing ────────────────────────────────────────────────────────────
    def _subprocess_runner(self, args: Sequence[str]) -> GitResult:
        try:
            done = subprocess.run(
                list(args), cwd=str(self.path), capture_output=True,
                text=True, errors="replace", timeout=self._timeout, check=False)
        except FileNotFoundError:
            return GitResult(False, "", f"{args[0]} is not installed")
        except (OSError, subprocess.SubprocessError) as exc:
            return GitResult(False, "", f"{args[0]} failed: {exc}")
        return GitResult(done.returncode == 0, done.stdout.strip(),
                         done.stderr.strip())

    def run(self, *args: str) -> GitResult:
        """One git command, with the destructive ones refused."""
        blocked = [a for a in args if a in FORBIDDEN]
        if blocked:
            return GitResult(
                False, "",
                f"I won't run git {' '.join(args)} — {', '.join(blocked)} can "
                f"destroy work that isn't recoverable. Do that one yourself.")
        if not self.path.is_dir():
            return GitResult(False, "", f"no such directory: {self.path}")
        return self._runner(["git", *args])

    # ── reading ─────────────────────────────────────────────────────────────
    def is_repo(self) -> bool:
        return self.run("rev-parse", "--git-dir").ok

    def branch(self) -> str:
        """The current branch, or "" when HEAD is genuinely detached.

        `--show-current` and not `rev-parse --abbrev-ref HEAD`: on a repo with
        no commits yet the rev-parse form *fails*, which made a brand-new
        project — the exact state anything just built is in — look like a
        detached HEAD and get refused. `--show-current` returns the branch
        name on an unborn branch and empty only when truly detached, which is
        the distinction that actually matters here.
        """
        result = self.run("branch", "--show-current")
        if result.ok and result.output:
            return result.output
        # Older git without --show-current; falls back to the previous form.
        fallback = self.run("rev-parse", "--abbrev-ref", "HEAD")
        return fallback.output if fallback.ok else ""

    def has_commits(self) -> bool:
        """False on a repo where nothing has ever been committed."""
        return self.run("rev-parse", "--verify", "HEAD").ok

    def status(self) -> GitResult:
        return self.run("status", "--porcelain=v1", "--branch")

    def changed_files(self) -> list[str]:
        result = self.run("status", "--porcelain")
        if not result.ok:
            return []
        files = []
        for line in result.output.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                files.append(parts[1])
        return files

    def diff(self, staged: bool = False) -> GitResult:
        return self.run("diff", "--staged") if staged else self.run("diff")

    def log(self, count: int = 5) -> GitResult:
        return self.run("log", f"-{max(1, min(count, 50))}", "--oneline")

    # ── writing ─────────────────────────────────────────────────────────────
    def untracked_files(self) -> list[str]:
        """Files git has never seen, honouring .gitignore."""
        result = self.run("ls-files", "--others", "--exclude-standard")
        if not result.ok:
            return []
        return [line.strip() for line in result.output.splitlines() if line.strip()]

    def stage(self, paths: Sequence[str] = ()) -> GitResult:
        """Stage the named paths, or everything safe to stage.

        Still never `-A`. With no paths this stages tracked changes first;
        if that stages nothing — a brand-new project, where no file is
        tracked yet — it falls back to adding untracked files *by name*,
        having filtered out anything that looks like a secret.

        Enumerating rather than `-A` is the whole point: the secret filter
        only works on names we have actually looked at. `-A` would have made
        the first commit work too, and would eventually have committed
        someone's `.env`.
        """
        secrets = [p for p in paths if looks_secret(p)]
        if secrets:
            return GitResult(
                False, "",
                f"I won't stage {', '.join(secrets)} — that looks like a "
                f"secret. Add it yourself if you really mean to.")
        if paths:
            return self.run("add", *paths)

        tracked = self.run("add", "-u")
        if self.has_staged_changes():
            return tracked

        untracked = self.untracked_files()
        safe = [p for p in untracked if not looks_secret(p)]
        skipped = [p for p in untracked if looks_secret(p)]
        if skipped:
            log.warning("vcs: not staging %s — looks like a secret",
                        ", ".join(skipped))
        if not safe:
            return tracked
        added = self.run("add", *safe)
        if added.ok and skipped:
            return GitResult(True, f"staged {len(safe)} file(s); left "
                                   f"{', '.join(skipped)} alone — looks secret")
        return added

    def commit(self, message: str, paths: Sequence[str] = (),
               allow_protected: bool = False) -> GitResult:
        message = (message or "").strip()
        if not message:
            return GitResult(False, "", "a commit needs a message")

        current = self.branch()
        if not current or current == "HEAD":
            # Detached HEAD: the commit would land nowhere and still look
            # like it worked, which is the worst available outcome.
            return GitResult(False, "", "HEAD is detached — I won't commit "
                                        "somewhere the work can be lost")
        # The protected-branch rule exists to stop an agent committing on top
        # of shared history. A repo with no commits has no history to endanger
        # — and refusing there would mean nothing newly built could ever make
        # its first commit, which is the one case where "main" is fine.
        if (current in PROTECTED_BRANCHES and not allow_protected
                and self.has_commits()):
            return GitResult(
                False, "",
                f"you're on {current} — I won't commit straight to it. "
                f"Make a branch first and I'll commit there.")

        staged = self.stage(paths)
        if not staged.ok:
            return staged
        if not self.has_staged_changes():
            return GitResult(False, "", "nothing staged to commit")
        return self.run("commit", "-m", message)

    def has_staged_changes(self) -> bool:
        # `git diff --staged --quiet` exits 0 when there is NOTHING staged and
        # 1 when there is — the inversion is worth a named method rather than
        # a double negative at the call site.
        return not self.run("diff", "--staged", "--quiet").ok

    def create_branch(self, name: str) -> GitResult:
        name = (name or "").strip()
        if not name or name.startswith("-"):
            return GitResult(False, "", f"invalid branch name: {name!r}")
        return self.run("checkout", "-b", name)

    def push(self, set_upstream: bool = True) -> GitResult:
        current = self.branch()
        if not current or current == "HEAD":
            return GitResult(False, "", "no branch to push")
        if current in PROTECTED_BRANCHES:
            return GitResult(False, "", f"I won't push {current} directly.")
        args = ["push"]
        if set_upstream:
            args += ["--set-upstream", "origin", current]
        return self.run(*args)

    # ── GitHub ──────────────────────────────────────────────────────────────
    def pull_request(self, title: str, body: str = "") -> GitResult:
        """Open a PR through the gh CLI, which already holds the auth."""
        title = (title or "").strip()
        if not title:
            return GitResult(False, "", "a pull request needs a title")
        if not self.path.is_dir():
            return GitResult(False, "", f"no such directory: {self.path}")
        return self._runner(["gh", "pr", "create", "--title", title,
                             "--body", body or title])

    def review_diff(self, against: str = "HEAD") -> GitResult:
        """The diff a reviewer would read: what this branch changed."""
        return self.run("diff", against, "--stat")
