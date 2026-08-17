"""Voice tools that hand a build to the job kernel.

Building software takes minutes, not seconds, so none of this happens inside
the conversation that asked for it. Each tool submits a job and returns
immediately; the kernel runs it, and delivery opens a fresh conversation when
it is done.
"""

from __future__ import annotations


def register_build_tools(reg, jobs, default_dir: str = ""):
    """Building software by voice — handed to a coding agent as a long job."""

    @reg.tool(
        description=(
            "Builds a working application from a description — writes it, "
            "runs it, and keeps fixing it until it works. Use when he asks "
            "you to build / make / write an app, a script, a tool, a game: "
            "'ek script bana do', 'build me a CLI that...'. This takes many "
            "minutes and happens in the background: say you're on it and "
            "you'll come back, then carry on. Do NOT wait for it. "
            "`where` is the folder to build in — ask him if you don't know."
        ),
        parameters={
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "description": "What to build, in full — the whole brief"},
                "where": {"type": "string",
                          "description": "Folder to build in, if he named one"},
            },
            "required": ["what"],
        },
    )
    def build_app(what: str, where: str = "") -> str:
        return _start_build("build", what, where)

    @reg.tool(
        description=(
            "Builds a working application AND puts it on GitHub — writes it, "
            "runs it, fixes it until it works, commits it, publishes it. Use "
            "when he asks you to build something and put it on GitHub / push "
            "it / make a repo for it. Takes many minutes and happens in the "
            "background: say you're on it and you'll come back, then carry "
            "on. The repo is PRIVATE unless he clearly says make it public."
        ),
        parameters={
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "description": "What to build, in full — the whole brief"},
                "where": {"type": "string",
                          "description": "Folder to build in, if he named one"},
                "repo_name": {"type": "string",
                              "description": "Repo name, if he said one"},
                "public": {"type": "boolean",
                           "description": "true ONLY if he clearly said public"},
            },
            "required": ["what"],
        },
    )
    def build_and_publish(what: str, where: str = "", repo_name: str = "",
                          public: bool = False) -> str:
        return _start_build("ship", what, where, repo_name=repo_name,
                            public=bool(public))

    def _start_build(job_type: str, what: str, where: str = "",
                     **extra) -> str:
        target = (where or default_dir).strip()
        if not target:
            return ("I need to know which folder to build in — tell me where "
                    "and I'll get started.")
        try:
            jobs.submit(job_type, what, origin="voice",
                        params={"cwd": target, "task": "code", **extra})
        except ValueError as exc:          # already building something
            return str(exc)
        except Exception as exc:           # noqa: BLE001 — never a spoken traceback
            return f"I couldn't start that: {exc}"
        if job_type == "ship":
            return ("On it — I'll build it, get it working, and put it on "
                    "GitHub. I'll come back to you when it's up.")
        return ("On it — I'll build it, run it, and keep at it until it "
                "works. I'll come back to you when it's done.")
