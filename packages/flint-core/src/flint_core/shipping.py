r"""From a sentence to a repo — the whole chain, as one job.

`building.py` gets working code onto disk. `vcs.py` can commit it. Neither
knows about the other, so "make me X and put it on GitHub" was two things a
person had to join by hand, and the join is the entire ask.

    build -> verify -> fix -> ... -> commit -> publish
                                                  |
                                     no gh? stop here and say so

Phases live in the job's scratch, so this is resumable the same way a build
is: a power cut between commit and publish resumes at publish rather than
rebuilding the project.

Three deliberate limits, all about the last step being the one that leaves
this machine:

  * **Private by default.** A new private repo is trivially deletable; a
    public one is indexed, forked and quoted within minutes. `public=True`
    has to be asked for, out loud, on purpose.
  * **Nothing is published that did not pass.** The commit happens only
    after the build loop is satisfied, and publishing only after the commit.
    A repo whose first commit is broken code is worse than no repo.
  * **A missing `gh` is a stopping point, not a failure.** The code is
    built, tested and committed locally; saying exactly that is a useful
    answer, and pretending it shipped is not.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from flint_core.building import run_build
from flint_core.kernel import Continue, Fail, Finish
from flint_core.vcs import GitRepo

log = logging.getLogger("flint.shipping")

GH_TIMEOUT = 180.0

#: A GitHub repo name: letters, digits, dot, dash, underscore.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def repo_name_for(goal: str, directory: Path, given: str = "") -> str:
    """A sane repo name from what was asked, or the folder it was built in."""
    if given.strip():
        candidate = given.strip()
    else:
        # First few meaningful words of the goal beat the temp-dir name.
        words = [w for w in re.findall(r"[A-Za-z0-9]+", goal.lower())
                 if w not in {"a", "an", "the", "make", "build", "me", "called",
                              "that", "with", "for", "and", "to", "in", "it"}]
        candidate = "-".join(words[:4]) or directory.name
    cleaned = _UNSAFE_NAME.sub("-", candidate).strip("-._")
    return (cleaned or "project")[:60]


def gh_available() -> bool:
    from shutil import which

    return which("gh") is not None


def _run(args: list[str], cwd: str, timeout: float = GH_TIMEOUT) -> tuple[bool, str]:
    try:
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              errors="replace", timeout=timeout, check=False)
    except FileNotFoundError:
        return False, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return False, f"{args[0]} was still running after {timeout:.0f}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {args[0]}: {exc}"
    return done.returncode == 0, f"{done.stdout}\n{done.stderr}".strip()


def run_ship(ctx):
    """One phase of build-and-publish. Delegates the build half to run_build."""
    scratch = ctx.scratch
    phase = scratch.get("phase", "build")
    directory = Path(str(ctx.params.get("cwd", "")) or ".").expanduser()

    # ── the build half, unchanged ───────────────────────────────────────────
    if phase in ("build", "verify", "fix"):
        outcome = run_build(ctx)
        if isinstance(outcome, Finish):
            # Built and verified. Intercept the finish and carry on into git
            # rather than stopping — the whole point of this job type.
            return Continue(note="built and checked — now committing it",
                            scratch={"phase": "commit",
                                     "built": outcome.result[:2000],
                                     "built_say": outcome.say}, sleep=0)
        return outcome            # Continue / Fail pass straight through

    if not directory.is_dir():
        return Fail(f"no such directory: {directory}", retry=False)
    git = GitRepo(directory)

    # ── commit what was built ───────────────────────────────────────────────
    if phase == "commit":
        # is_repo_root, not is_repo. A build folder that merely sits *inside*
        # some ancestor repository — a version-controlled home directory, say
        # — would otherwise skip the init and commit the new project into
        # that unrelated repo, sweeping up whatever else was lying around in
        # it. Found exactly that way.
        if not git.is_repo_root():
            inside = git.root()
            if inside:
                ctx.log(f"this folder sits inside {inside} — giving the "
                        f"project its own repo instead")
            ok, output = _run(["git", "init", "-q"], str(directory))
            if not ok:
                return Fail(f"couldn't start a repo here: {output}", retry=False)
            ctx.log("started a git repo")

        message = str(ctx.params.get("message") or "").strip() or (
            f"{ctx.goal[:70]}\n\nBuilt and verified before committing.")
        result = git.commit(message)
        if not result.ok:
            if "nothing staged" in result.error:
                # Already committed on a previous step, or the agent wrote
                # nothing new. Not worth failing the whole job over.
                ctx.log("nothing new to commit")
            else:
                return Fail(f"couldn't commit it: {result.text}", retry=False)
        else:
            ctx.log(f"committed on {git.branch()}")

        if not git.has_commits():
            return Fail("there is nothing committed to publish", retry=False)
        return Continue(note="committed", scratch={"phase": "publish"}, sleep=0)

    # ── put it on GitHub ────────────────────────────────────────────────────
    if phase == "publish":
        built_say = str(scratch.get("built_say", "")) or "It's built."
        if not ctx.params.get("publish", True):
            return Finish(result=str(scratch.get("built", "")),
                          say=f"{built_say} It's committed locally — say the "
                              f"word and I'll put it on GitHub.")
        if not gh_available():
            # Everything real still happened. Say what is true.
            return Finish(
                result=str(scratch.get("built", "")),
                say=f"{built_say} It's committed locally, but I can't put it "
                    f"on GitHub from here — the gh command isn't installed.")

        name = repo_name_for(ctx.goal, directory, str(ctx.params.get("repo_name", "")))
        visibility = "--public" if ctx.params.get("public") else "--private"
        ctx.log(f"creating {visibility.lstrip('-')} repo {name}")
        ok, output = _run(["gh", "repo", "create", name, visibility,
                           "--source=.", "--remote=origin", "--push"],
                          str(directory))
        if not ok:
            return Finish(
                result=output,
                say=f"{built_say} It's committed locally, but GitHub refused: "
                    f"{output.splitlines()[-1][:150] if output else 'unknown error'}")

        url = next((w for w in output.split() if "github.com" in w), name)
        ctx.log(f"published to {url}")
        return Finish(
            result=f"{scratch.get('built', '')}\n\nPublished: {url}",
            say=f"{built_say} It's on GitHub now — {url}")

    return Fail(f"unknown ship phase: {phase}", retry=False)
