"""Voice tools over work that outlives the conversation asking for it.

Background jobs and delegated watches are both "go away, do this properly,
come back to me" — the kernel owns scheduling and budgets, the watch store
owns what is being watched, and these are only the spoken surface over them.
"""

from __future__ import annotations


def register_watch_tools(reg, watches):
    """Jobs she goes away and keeps an eye on, then reports back on."""
    @reg.tool(
        description=(
            "Takes on a background job: keep checking something on the web "
            "and INTERRUPT the user later, on your own, the moment it "
            "happens. Use whenever he delegates something with a 'tell me "
            "when' / 'let me know if' / 'keep an eye on' shape — 'tell me "
            "when the match turns', 'batao jab result aa jaye', 'let me "
            "know if the price drops'.\n"
            "`what` must be self-contained enough to search for on its own "
            "hours from now — resolve 'it' and 'that' into real names "
            "before calling. `condition` is what makes it worth "
            "interrupting him for; leave it EMPTY to be told on any real "
            "change. Set `urgent` only if he wants waking at night.\n"
            "Each check costs a web search, so pick an honest "
            "`check_every_minutes`: minutes for a live match, an hour for "
            "a result that lands sometime today. Tell him you'll come back "
            "to him — do NOT keep checking within this conversation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "description": "What to keep checking, self-contained"},
                "condition": {"type": "string",
                              "description": "What makes it worth telling him; "
                                             "omit to fire on any real change"},
                "check_every_minutes": {
                    "type": "integer",
                    "description": "Minutes between checks (min 2, default 10)"},
                "for_hours": {"type": "number",
                              "description": "Give up after this long (default 24)"},
                "urgent": {"type": "boolean",
                           "description": "true to interrupt even at night"},
            },
            "required": ["what"],
        },
    )
    def watch_for(what: str, condition: str = "",
                  check_every_minutes: int = 10, for_hours: float = 24.0,
                  urgent: bool = False) -> str:
        try:
            entry = watches.add(what, condition,
                                interval=max(2, int(check_every_minutes or 10)) * 60,
                                ttl_hours=for_hours, urgent=urgent)
        except ValueError as exc:
            return str(exc)
        every = int(entry["interval"] // 60)
        return (f"Watching that now — checking every {every} minutes. "
                "I'll come back to you when it happens.")

    @reg.tool(
        description=("Says what background watches are running. Use for "
                     "'what are you watching?', 'kya track kar rahi ho?'."),
    )
    def list_watches() -> str:
        return watches.summary()

    @reg.tool(
        description=(
            "Stops a background watch. Pass a few words of the thing being "
            "watched; omit `what` to stop every watch. Use for 'stop "
            "watching the match', 'sab band kar do'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "description": "Words identifying the watch; omit for all"},
            },
        },
    )
    def stop_watching(what: str = "") -> str:
        dropped = watches.cancel(what)
        if not dropped:
            return "I wasn't watching anything matching that."
        if not what.strip():
            return f"Stopped all {dropped} watches."
        return f"Stopped {dropped} watch{'es' if dropped > 1 else ''}."


def register_job_tools(reg, jobs):
    """Multi-step work handed to the kernel and delivered when it lands."""
    @reg.tool(
        description=(
            "Hands a whole question to your background worker to go and "
            "research properly — several web searches, then a written "
            "answer — and comes back to him with it later. Use when he "
            "asks you to look into / research / dig into something, or "
            "when a real answer plainly needs more than one search: "
            "'iske baare mein pata karo', 'research this properly', "
            "'find out everything about X and tell me'.\n"
            "NOT for a quick fact — use web_search for anything one search "
            "answers, because that comes back inside this conversation.\n"
            "`goal` must be self-contained enough to work on an hour from "
            "now: resolve 'it', 'that' and 'him' into real names first. "
            "Tell him you'll go and do it and come back — then move on. Do "
            "NOT wait for it or keep asking about it in this conversation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "The question to research, self-contained"},
            },
            "required": ["goal"],
        },
    )
    def research_in_background(goal: str) -> str:
        try:
            jobs.submit("research", goal, origin="voice")
        except ValueError as exc:      # at the per-type ceiling
            return str(exc)
        except Exception as exc:       # noqa: BLE001 — never a spoken traceback
            return f"I couldn't start that: {exc}"
        return ("On it — I'll go and look into that properly and come back "
                "to you when I have the answer.")

    @reg.tool(
        description=("Says what background work is running and how far "
                     "along it is. Use for 'what are you working on?', "
                     "'us research ka kya hua?', 'any progress?'."),
    )
    def background_jobs() -> str:
        return jobs.summary()

    @reg.tool(
        description=(
            "Stops background work. Pass a few words of what he wants "
            "stopped; omit `what` to stop everything. Use for 'stop that "
            "research', 'sab cancel kar do'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "description": "Words identifying the job; omit for all"},
            },
        },
    )
    def cancel_background_job(what: str = "") -> str:
        stopped = jobs.cancel_matching(what)
        if not stopped:
            return "I'm not working on anything matching that."
        if not what.strip():
            return f"Stopped all {stopped} background jobs."
        return f"Stopped {stopped} job{'s' if stopped > 1 else ''}."
