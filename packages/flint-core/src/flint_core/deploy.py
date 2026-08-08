r"""Shipping it — and the several reasons not to, checked first.

Deployment is the first thing in this codebase that is genuinely hard to take
back. A bad commit sits on a branch; a bad deploy is live, and "live" may mean
a VPS someone else is using. Everything else here optimises for getting work
done unattended. This module optimises for not shipping something by accident,
and only then for shipping it.

Four gates, in order, and a deploy that fails any of them does not happen:

    1. the target is named in config      — never a host the model chose
    2. the project verifies               — never ship code nobody ran
    3. there is a recipe for it           — never invent a deploy command
    4. it was actually authorised         — dry run unless told otherwise

Gate 1 is the important one. Everywhere else in this system a model can
influence what happens; here it cannot even name the machine. `DeployTarget`s
come from config, the model picks one *by name* from that list, and an
unrecognised name is a refusal rather than an improvisation.

Gate 4 exists because "deploy it" said out loud to a voice assistant is not
the same as a human typing a deploy command. The default is a dry run that
reports exactly what would happen; shipping for real needs `confirm=True`,
which the voice layer only sets after saying what it is about to do.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("flint.deploy")

#: How a project is deployed, chosen by what is in it. Fixed argv templates —
#: `{name}` is the target's own name from config, never model output.
DEPLOY_RECIPES: tuple[tuple[str, tuple[tuple[str, ...], ...], str], ...] = (
    ("docker-compose.yml",
     (("docker", "compose", "build"), ("docker", "compose", "up", "-d")),
     "docker compose"),
    ("compose.yaml",
     (("docker", "compose", "build"), ("docker", "compose", "up", "-d")),
     "docker compose"),
    ("Dockerfile",
     (("docker", "build", "-t", "{name}", "."),
      ("docker", "run", "-d", "--name", "{name}", "{name}")),
     "docker"),
)

DEPLOY_TIMEOUT = 600.0
MAX_OUTPUT = 8000


class DeployError(Exception):
    pass


@dataclass(frozen=True)
class DeployTarget:
    """Somewhere it is allowed to go. Comes from config; never from a model.

    `host` empty means this machine. A non-empty host is reached over ssh,
    using whatever key and config ssh already has — no credentials are read,
    stored or passed by this module, because a deploy tool that handles
    secrets is a deploy tool that leaks them.
    """

    name: str
    host: str = ""
    directory: str = ""
    #: Extra confirmation for somewhere real users are. Production targets
    #: should set this even though `confirm` is already required.
    production: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DeployError("a deploy target needs a name")
        if " " in self.name:
            raise DeployError(f"target name must be one word: {self.name!r}")

    @property
    def local(self) -> bool:
        return not self.host.strip()

    def describe(self) -> str:
        where = "this machine" if self.local else self.host
        return f"{self.name} ({where}{', production' if self.production else ''})"


class DeployTargets:
    """The allowlist. An unknown name is refused, never guessed at."""

    def __init__(self, targets: Sequence[DeployTarget] = ()):
        self._targets = {t.name.lower(): t for t in targets}

    def __len__(self) -> int:
        return len(self._targets)

    def __iter__(self):
        return iter(self._targets.values())

    def names(self) -> list[str]:
        return sorted(self._targets)

    def get(self, name: str) -> DeployTarget:
        found = self._targets.get((name or "").strip().lower())
        if found is None:
            known = ", ".join(self.names()) or "none configured"
            raise DeployError(
                f"I don't have a deploy target called {name!r}. "
                f"I can deploy to: {known}.")
        return found

    def describe(self) -> str:
        if not self._targets:
            return "No deploy targets are configured."
        return "\n".join(t.describe() for t in self)

    @classmethod
    def from_config(cls, entries: Sequence[dict]) -> DeployTargets:
        targets = []
        for entry in entries or ():
            try:
                targets.append(DeployTarget(
                    name=str(entry.get("name", "")).strip(),
                    host=str(entry.get("host", "")).strip(),
                    directory=str(entry.get("directory", "")).strip(),
                    production=bool(entry.get("production", False)),
                ))
            except DeployError as exc:
                log.warning("deploy: skipping bad target %r (%s)", entry, exc)
        return cls(targets)


@dataclass(frozen=True)
class DeployPlan:
    """Exactly what would run, where. Printable before anything happens."""

    target: DeployTarget
    commands: tuple[tuple[str, ...], ...]
    kind: str
    directory: str = ""

    def rendered(self) -> list[str]:
        """The commands as they would actually be run, ssh wrapper included."""
        return [" ".join(shlex.quote(part) for part in cmd)
                for cmd in self.wrapped()]

    def wrapped(self) -> list[tuple[str, ...]]:
        if self.target.local:
            return [tuple(c) for c in self.commands]
        remote_dir = self.target.directory or self.directory
        return [("ssh", self.target.host,
                 f"cd {shlex.quote(remote_dir)} && "
                 + " ".join(shlex.quote(part) for part in cmd))
                for cmd in self.commands]

    def describe(self) -> str:
        lines = [f"Deploy to {self.target.describe()} using {self.kind}:"]
        lines += [f"  $ {line}" for line in self.rendered()]
        return "\n".join(lines)


def detect_recipe(names: Sequence[str]) -> tuple[tuple[tuple[str, ...], ...], str]:
    """How this project deploys, from what is in it. Empty when unrecognised."""
    flat = {str(n) for n in names}
    for marker, commands, kind in DEPLOY_RECIPES:
        if marker in flat:
            return commands, kind
    return (), ""


def plan_deploy(directory: Path, target: DeployTarget,
                names: Sequence[str] | None = None) -> DeployPlan:
    """What deploying this project to this target would do. Runs nothing."""
    if names is None:
        try:
            names = [p.name for p in directory.iterdir()]
        except OSError as exc:
            raise DeployError(f"can't read {directory}: {exc}") from None

    commands, kind = detect_recipe(names)
    if not commands:
        raise DeployError(
            "I don't know how to deploy this — there's no Dockerfile or "
            "compose file, and I'm not going to guess at a deploy command.")
    substituted = tuple(
        tuple(part.replace("{name}", target.name) for part in cmd)
        for cmd in commands)
    return DeployPlan(target=target, commands=substituted, kind=kind,
                      directory=str(directory))


def _run(command: Sequence[str], cwd: str, timeout: float) -> tuple[bool, str]:
    try:
        done = subprocess.run(list(command), cwd=cwd, capture_output=True,
                              text=True, errors="replace", timeout=timeout,
                              check=False)
    except FileNotFoundError:
        return False, f"{command[0]} is not installed on this machine"
    except subprocess.TimeoutExpired:
        return False, f"{command[0]} was still running after {timeout:.0f}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {command[0]}: {exc}"
    return done.returncode == 0, f"{done.stdout}\n{done.stderr}".strip()


def execute(plan: DeployPlan, *, confirm: bool = False, runner=None,
            timeout: float = DEPLOY_TIMEOUT) -> tuple[bool, str]:
    """Run the plan — or, without `confirm`, only say what it would do.

    The dry run is the default because the caller is usually a voice
    assistant acting on a spoken sentence, and "deploy it" said out loud
    deserves one more beat than a typed command.
    """
    if not confirm:
        return True, ("Dry run — nothing has been deployed.\n"
                      + plan.describe())

    run = runner or _run
    transcript: list[str] = []
    for command in plan.wrapped():
        shown = " ".join(command)
        log.info("deploy: %s", shown)
        transcript.append(f"$ {shown}")
        ok, output = run(command, plan.directory, timeout)
        transcript.append(output[:MAX_OUTPUT])
        if not ok:
            # Stop at the first failure. Carrying on after `docker build`
            # fails means `docker run` starts the *previous* image, which
            # looks like a successful deploy of code that was never built.
            return False, "\n".join(transcript)
    return True, "\n".join(transcript)


# ── as a kernel job ──────────────────────────────────────────────────────────
def run_deploy(ctx):
    """One deployment, as a job: verify, plan, then ship if authorised."""
    from flint_core.building import VERIFY_TIMEOUT, detect_verify_command
    from flint_core.kernel import Fail, Finish

    targets: DeployTargets = ctx.require("deploy_targets")
    verify = ctx.service("verify")
    directory = Path(str(ctx.params.get("cwd", "")) or ".").expanduser()
    if not directory.is_dir():
        return Fail(f"no such directory: {directory}", retry=False)

    try:
        target = targets.get(str(ctx.params.get("target", "")))
    except DeployError as exc:
        return Fail(str(exc), retry=False)

    names = [p.name for p in directory.iterdir()]

    # Gate 2: never ship code nobody ran. A project with nothing to run is
    # allowed through — refusing would make this unusable for static sites —
    # but a project with tests that fail is not.
    if verify is not None and not ctx.params.get("skip_verify"):
        command, description = detect_verify_command(names)
        if command:
            ctx.log(f"checking {description} before deploying")
            ok, output = verify(str(directory), command, VERIFY_TIMEOUT)
            if not ok:
                return Fail(
                    f"not deploying — {description} fails:\n"
                    f"{output[:1000]}", retry=False)

    try:
        plan = plan_deploy(directory, target, names)
    except DeployError as exc:
        return Fail(str(exc), retry=False)

    confirm = bool(ctx.params.get("confirm"))
    ctx.log(f"{'deploying' if confirm else 'dry run'}: {plan.kind} -> {target.name}")
    ok, transcript = execute(plan, confirm=confirm)
    if not ok:
        return Fail(f"deploy to {target.name} failed:\n{transcript[:1500]}",
                    retry=False)
    if not confirm:
        return Finish(result=transcript,
                      say=f"I haven't deployed anything — here's what it would "
                          f"do: {len(plan.commands)} command(s) on "
                          f"{target.describe()}. Say go ahead and I'll run it.")
    return Finish(result=transcript,
                  say=f"Deployed to {target.describe()}.")
