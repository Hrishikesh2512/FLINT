"""Voice tools for git and deployment, bounded by an allowlist.

The `workspace` argument is anything carrying the four members below —
Venom passes its `DevConfig`, Carnage its own, and neither had to learn about
the other. Structural typing is doing real work here: the allowlist is a
product decision each device makes, while the tools over it are not.

    repos          ((name, path), ...)
    deploy_targets ({...}, ...)
    repo_path(name) -> str
    repo_names     -> (str, ...)
    default_repo   -> str
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Workspace(Protocol):
    """The repos and targets a device is allowed to touch."""

    deploy_targets: tuple[dict, ...]

    def repo_path(self, name: str) -> str: ...

    @property
    def repo_names(self) -> tuple[str, ...]: ...

    @property
    def default_repo(self) -> str: ...


def register_dev_tools(reg, workspace: Workspace, jobs=None):
    """Git and deployment, bounded by the repos and targets named in config.

    Every tool here resolves a *name* against the allowlist rather than taking
    a path or a host. She cannot reach a repo you did not name, and cannot
    deploy anywhere you did not list — which is the whole reason these were
    not wired up until you decided what they may touch.
    """
    from flint_core.deploy import DeployTargets
    from flint_core.vcs import GitRepo

    targets = DeployTargets.from_config(list(workspace.deploy_targets))

    def _repo(name: str):
        path = workspace.repo_path(name) or (workspace.default_repo if not name else "")
        if not path:
            known = ", ".join(workspace.repo_names) or "none"
            return None, (f"I don't have a repo called {name or 'that'}. "
                          f"I know about: {known}.")
        return GitRepo(path), ""

    @reg.tool(
        description=(
            "Says what's changed in a repo — branch, modified files, recent "
            "commits. Use for 'what have I changed?', 'kya status hai?', "
            "'what branch am I on?'. Name the repo if he has more than one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
        },
    )
    def code_status(repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        if not git.is_repo():
            return "That folder isn't a git repo."
        changed = git.changed_files()
        branch = git.branch()
        if not changed:
            return f"On {branch}, nothing changed — working tree is clean."
        shown = ", ".join(changed[:5])
        more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
        return f"On {branch} with {len(changed)} file(s) changed: {shown}{more}."

    @reg.tool(
        description=(
            "Commits the current changes with a message. Refuses on main or "
            "master — make a branch first. Use for 'commit this', 'commit kar "
            "de with message X'. Never invent a message: use his words, or "
            "ask what the change was."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The commit message"},
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
            "required": ["message"],
        },
    )
    def commit_code(message: str, repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        return git.commit(message).text

    @reg.tool(
        description=("Starts a new branch in a repo. Use for 'make a branch "
                     "called X', 'nayi branch banao'."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Branch name"},
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
            "required": ["name"],
        },
    )
    def new_branch(name: str, repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        result = git.create_branch(name)
        return f"You're on {name} now." if result.ok else result.text

    @reg.tool(
        description=("Pushes the current branch and opens a pull request. Use "
                     "for 'push it', 'raise a PR', 'PR bana do'."),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
            "required": ["title"],
        },
    )
    def open_pull_request(title: str, repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        pushed = git.push()
        if not pushed.ok:
            return f"Couldn't push: {pushed.text}"
        return git.pull_request(title).text

    if targets and jobs is not None:
        @reg.tool(
            description=(
                "Deploys a project to one of his configured targets. By "
                "DEFAULT this only says what it would do and changes nothing "
                "— tell him what it reports, then call it again with "
                "confirm=true ONLY if he clearly says go ahead. Never pass "
                "confirm=true on the first try, and never pick a target he "
                "didn't name. Known targets: " + ", ".join(targets.names()) + "."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string",
                               "description": "Which target, by name"},
                    "repo": {"type": "string", "description": "Which repo, by name"},
                    "confirm": {"type": "boolean",
                                "description": "true ONLY after he says go ahead"},
                },
                "required": ["target"],
            },
        )
        def deploy_project(target: str, repo: str = "",
                           confirm: bool = False) -> str:
            path = workspace.repo_path(repo) or workspace.default_repo
            if not path:
                return "I don't know which project to deploy."
            try:
                jobs.submit("deploy", f"deploy to {target}", origin="voice",
                            params={"cwd": path, "target": target,
                                    "confirm": bool(confirm)})
            except ValueError as exc:
                return str(exc)
            except Exception as exc:      # noqa: BLE001
                return f"I couldn't start that: {exc}"
            if confirm:
                return "Deploying now — I'll tell you how it goes."
            return ("Checking what that would do — I'll read it back to you "
                    "before anything ships.")

        @reg.tool(
            description=("Lists the places she's allowed to deploy to. Use for "
                         "'where can you deploy?'."),
        )
        def deploy_targets() -> str:
            return targets.describe()
