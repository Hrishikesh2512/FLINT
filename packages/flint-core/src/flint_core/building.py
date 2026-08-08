r"""Building something that works, then making it work — the generate/fix loop.

Two items on the wish list are one mechanism:

    #2   build a complete application from a single prompt
    #17  debug and fix its own generated code

Because "build an app" is not a generation problem. A model writes plausible
code on the first try roughly always and *working* code much less often; what
separates the two is running it, reading the failure, and going again. The
loop is the feature. Generation is a step inside it.

    build  ->  verify  ->  (fails) ->  fix  ->  verify  ->  ...  ->  done
                   \-> (passes) -> done

FLINT already had a version of this in `actions/dev_agent.py`, and it works,
but it runs the whole cycle inside one blocking call: a restart loses
everything, nothing bounds the spend but its own attempt counter, and the user
waits with no idea what is happening. Running it as a kernel job instead makes
each phase a step — so progress is journalled, the budget is enforced from
outside, a power cut resumes rather than restarts, and the result arrives by
her opening a conversation.

**The verify command never comes from the model.** `dev_agent.py` takes its
`run_command` from the planner's JSON, which means the thing being executed is
chosen by the thing being debugged. Here it comes from `VERIFY_RECIPES`, keyed
on what files actually exist. A model can influence *what gets written*; it
cannot pick the command that then runs on the machine.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

from flint_core.kernel import Continue, Fail, Finish, JobContext

log = logging.getLogger("flint.building")

#: How to check a project works, chosen by what is in it. Ordered: the first
#: match wins, so a real test suite beats a smoke run.
#:
#: These are fixed commands, not templates — nothing model-generated is ever
#: interpolated into one, and nothing runs through a shell.
VERIFY_RECIPES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("pytest.ini", ("python", "-m", "pytest", "-q"), "the test suite"),
    ("tox.ini", ("python", "-m", "pytest", "-q"), "the test suite"),
    ("tests", ("python", "-m", "pytest", "-q"), "the test suite"),
    ("Cargo.toml", ("cargo", "test", "-q"), "the test suite"),
    ("go.mod", ("go", "test", "./..."), "the test suite"),
    ("package.json", ("npm", "test", "--silent"), "the test suite"),
    ("main.py", ("python", "main.py"), "a smoke run"),
    ("app.py", ("python", "app.py"), "a smoke run"),
    ("index.js", ("node", "index.js"), "a smoke run"),
)

#: How long any one verification may take before it counts as hung.
VERIFY_TIMEOUT = 120.0

#: Failure output handed back to the fixer. Enough to see the traceback,
#: not so much that it crowds out the instruction.
MAX_ERROR_CHARS = 4000

#: Fix attempts before giving up and saying so. The kernel's step budget is
#: the outer bound; this is the one that produces a useful message.
MAX_FIX_ATTEMPTS = 4


def detect_verify_command(names: Sequence[str]) -> tuple[tuple[str, ...], str]:
    """The command that checks this project, and what it is in words.

    Returns an empty command when nothing is recognised — better to report
    "I built it but couldn't check it" than to invent a way of running it.
    """
    present = {str(n).strip("/\\").split("/")[0].split("\\")[0] for n in names}
    flat = {str(n) for n in names}
    for marker, command, description in VERIFY_RECIPES:
        if marker in present or marker in flat:
            return command, description
    if any(n.startswith("test_") and n.endswith(".py") for n in flat):
        return ("python", "-m", "pytest", "-q"), "the test suite"
    return (), ""


def _default_verifier(cwd: str, command: Sequence[str],
                      timeout: float) -> tuple[bool, str]:
    """Run one fixed command in the project. No shell, always bounded."""
    try:
        done = subprocess.run(
            list(command), cwd=cwd, capture_output=True, text=True,
            errors="replace", timeout=timeout, check=False)
    except FileNotFoundError:
        return False, f"{command[0]} is not installed on this machine"
    except subprocess.TimeoutExpired:
        return False, f"{command[0]} was still running after {timeout:.0f}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {command[0]}: {exc}"
    output = f"{done.stdout}\n{done.stderr}".strip()
    return done.returncode == 0, output


def _project_files(directory: Path) -> list[str]:
    try:
        return sorted(p.name for p in directory.iterdir())
    except OSError:
        return []


BUILD_INSTRUCTION = (
    "{goal}\n\n"
    "Write the whole thing so it actually runs. Include a way to verify it: "
    "tests where that makes sense, otherwise an entry point that exercises "
    "the main path. Add any dependency manifest the project needs "
    "(requirements.txt, package.json, Cargo.toml) so it can be installed."
)

FIX_INSTRUCTION = (
    "The project you just wrote does not work yet. Here is the goal, then "
    "exactly what happened when it was run.\n\n"
    "GOAL: {goal}\n\n"
    "COMMAND: {command}\n\n"
    "OUTPUT:\n{error}\n\n"
    "Fix the cause, not the symptom. Do not delete tests or weaken assertions "
    "to make the failure go away — if a test is genuinely wrong, fix the test "
    "and say so. Change as little as possible."
)


def run_build(ctx: JobContext):
    """One phase of building an application. Phases live in the job's scratch.

    Each call does exactly one thing and returns, so the kernel owns the loop:
    it journals progress, enforces the budget, and can be interrupted between
    phases without losing what has been done.
    """
    from flint_core.agents import AgentRequest

    agents = ctx.require("agents")
    verify = ctx.service("verify", _default_verifier)
    scratch = ctx.scratch
    phase = scratch.get("phase", "build")

    directory = Path(str(ctx.params.get("cwd", "")) or ".").expanduser()
    if not directory.is_dir():
        return Fail(f"no such directory: {directory}", retry=False)

    agent_task = str(ctx.params.get("task", "code"))
    agent_name = str(ctx.params.get("agent", ""))

    # ── write it, or fix what's there ───────────────────────────────────────
    if phase in ("build", "fix"):
        attempts = int(scratch.get("attempts", 0))
        if phase == "fix":
            if attempts >= MAX_FIX_ATTEMPTS:
                return Fail(
                    f"I couldn't get it working after {attempts} attempts. "
                    f"The last failure was: "
                    f"{str(scratch.get('last_error', ''))[:200]}", retry=False)
            instruction = FIX_INSTRUCTION.format(
                goal=ctx.goal,
                command=" ".join(scratch.get("verify_command") or ()),
                error=str(scratch.get("last_error", ""))[:MAX_ERROR_CHARS])
            ctx.log(f"fixing (attempt {attempts + 1} of {MAX_FIX_ATTEMPTS})")
        else:
            instruction = BUILD_INSTRUCTION.format(goal=ctx.goal)
            ctx.log("writing the first version")

        result = agents.run(
            AgentRequest(goal=instruction, cwd=str(directory),
                         on_progress=ctx.log,
                         timeout=float(ctx.params.get("timeout", 900.0))),
            task=agent_task, agent=agent_name)

        if result.needs_input:
            return Finish(result=result.detail or result.question,
                          say=result.question)
        if not result.ok:
            # The agent itself failed (crashed, timed out, not installed) —
            # distinct from the code it wrote failing, which is the fix loop's
            # job. Retrying an agent that will not start is pointless.
            return Fail(result.error or result.summary, retry=False)

        written = list(scratch.get("written") or [])
        for name in result.artifacts:
            if name not in written:
                written.append(name)
        return Continue(
            note=f"{'fixed' if phase == 'fix' else 'wrote'} "
                 f"{len(result.artifacts)} file(s)",
            scratch={"phase": "verify", "written": written,
                     "attempts": attempts + (1 if phase == "fix" else 0),
                     "agent_summary": result.summary},
            sleep=0)

    # ── run it and see ──────────────────────────────────────────────────────
    if phase == "verify":
        command, description = detect_verify_command(_project_files(directory))
        if not command:
            # Built, but unverifiable. Saying so is the honest outcome;
            # claiming success for code nobody ran is not.
            return Finish(
                result=str(scratch.get("agent_summary", "")) or "Built.",
                say="I built it, but there was nothing I could run to check "
                    "it actually works — worth a look yourself.")

        ctx.log(f"running {description}: {' '.join(command)}")
        ok, output = verify(str(directory), command, VERIFY_TIMEOUT)
        if ok:
            attempts = int(scratch.get("attempts", 0))
            written = scratch.get("written") or []
            fixed = f" after {attempts} fix{'es' if attempts != 1 else ''}" if attempts else ""
            return Finish(
                result=f"{scratch.get('agent_summary', '')}\n\n"
                       f"{description} passes.\n\n{output[:MAX_ERROR_CHARS]}",
                say=f"It's built and {description} passes{fixed} — "
                    f"{len(written)} file(s) in {directory.name}.")

        ctx.log(f"{description} failed")
        return Continue(
            note=f"{description} failed — going back to fix it",
            scratch={"phase": "fix", "last_error": output,
                     "verify_command": list(command)},
            sleep=0)

    return Fail(f"unknown build phase: {phase}", retry=False)
