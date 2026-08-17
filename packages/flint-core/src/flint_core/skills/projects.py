"""Voice tools over a ProjectStore — real work, deadlines, what's blocked.

`flint_core.projects` is the store; this is the handful of tools that let
someone talk to it. They are split because a device can want one without the
other: the sync engine needs the store on every device, while only a device
someone actually speaks to needs the tools.

Nothing here is device-specific — a task blocked on another task is the same
fact on a Pi, a phone, or a laptop — which is why it lives in the shared core
rather than being copied into each body's tool belt.
"""

from __future__ import annotations

import time


def register_project_tools(reg, projects, clock=time.time):
    """Tracking real work: tasks, deadlines, and what's actually blocked.

    Deliberately separate from add_to_list/add_note, which are for flat lists
    ("buy milk"). The difference the user feels is that this one can answer
    "what should I do next" — because it knows what is waiting on what.
    """
    def _due_epoch(in_hours: float | None, in_days: float | None) -> float | None:
        if in_hours:
            return clock() + float(in_hours) * 3600
        if in_days:
            return clock() + float(in_days) * 86400
        return None

    @reg.tool(
        description=(
            "Tracks a piece of real work with a deadline and what it's waiting "
            "on. Use for 'remind me to finish X by Friday', 'add a task', "
            "'I need to do X after Y is done'. NOT for shopping items — that's "
            "add_to_list. Set `after` to whatever must happen first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What needs doing"},
                "project": {"type": "string", "description": "Which project, if any"},
                "in_hours": {"type": "number", "description": "Due in this many hours"},
                "in_days": {"type": "number", "description": "Due in this many days"},
                "after": {"type": "string",
                          "description": "A few words of the task this waits on"},
            },
            "required": ["title"],
        },
    )
    def add_task(title: str, project: str = "", in_hours: float | None = None,
                 in_days: float | None = None, after: str = "") -> str:
        try:
            task = projects.add_task(
                title, project=project, due=_due_epoch(in_hours, in_days),
                depends_on=[after] if after.strip() else ())
        except Exception as exc:  # noqa: BLE001 — spoken, never a traceback
            return str(exc)
        if task["depends_on"]:
            return f"Added — {title}, once {after} is done."
        return f"Added — {title}."

    @reg.tool(
        description=("Says what he should actually work on: overdue things, "
                     "what's ready to start, what's blocked. Use for 'what's "
                     "on?', 'what should I do next?', 'kya karna hai?'."),
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Limit to one project"},
            },
        },
    )
    def whats_next(project: str = "") -> str:
        return projects.summary(project)

    @reg.tool(
        description=("Explains why a task can't start yet — names the actual "
                     "thing blocking it. Use for 'why can't I start X?', "
                     "'what's X waiting on?'."),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words of the task"},
            },
            "required": ["task"],
        },
    )
    def why_blocked(task: str) -> str:
        return projects.explain(task)

    @reg.tool(
        description=("Marks a task done. Use for 'I finished X', 'X ho gaya', "
                     "'mark X complete'."),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words of the task"},
            },
            "required": ["task"],
        },
    )
    def complete_task(task: str) -> str:
        done = projects.complete(task)
        if done is None:
            return f"I don't have a task matching {task}."
        return f"Nice — {done['title']} is done."

    @reg.tool(
        description=("Records that one task has to wait for another. Use for "
                     "'X can't start until Y is done', 'do Y first'."),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task that waits"},
                "after": {"type": "string", "description": "What must happen first"},
            },
            "required": ["task", "after"],
        },
    )
    def block_task(task: str, after: str) -> str:
        try:
            projects.block_on(task, after)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return f"Got it — {task} waits for {after}."
