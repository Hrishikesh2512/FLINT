"""Projects, tasks, milestones, deadlines — and what is actually blocked.

A to-do list is a flat list of strings; `venom/stores.py` already has one and
it is the right thing for "buy milk". This is the other shape: work with
structure, where the useful questions are not "what is on the list" but

    what can I actually start right now?      (nothing blocking it)
    what is about to be late?                 (deadlines, ordered)
    what is this whole thing waiting on?      (the blocking chain)

Dependencies are the part that earns the module. Without them "what should I
do next" is unanswerable and every task looks equally available; with them it
falls out of the data. They are also the part that goes wrong: a dependency
cycle makes every task in it permanently blocked and silently unstartable, so
`add_task` refuses to create one rather than letting it be discovered later
by a confused user.

Same storage shape as the rest of Venom's stores — one small JSON file, atomic
writes under a lock — so it survives a reboot exactly like reminders do.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from threading import Lock

OPEN = "open"
DONE = "done"
DROPPED = "dropped"

#: A task due within this window counts as "soon" when nothing else is said.
SOON_HOURS = 48.0


class ProjectError(Exception):
    pass


class ProjectStore:
    """Projects with tasks, deadlines and dependencies, on disk."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self._path = Path(path)
        self._clock = clock
        self._lock = Lock()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"projects": {}, "tasks": {}}
        if not isinstance(data, dict):
            return {"projects": {}, "tasks": {}}
        data.setdefault("projects", {})
        data.setdefault("tasks", {})
        return data

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".proj-",
                                   suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── projects ────────────────────────────────────────────────────────────
    def add_project(self, name: str, goal: str = "", due: float | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ProjectError("a project needs a name")
        with self._lock:
            data = self._load()
            key = name.lower()
            if key in data["projects"]:
                return data["projects"][key]
            record = {"name": name, "goal": goal.strip(), "due": due,
                      "created": self._clock(), "status": OPEN}
            data["projects"][key] = record
            self._save(data)
        return record

    def projects(self, include_done: bool = False) -> list[dict]:
        data = self._load()
        return [p for p in data["projects"].values()
                if include_done or p.get("status") == OPEN]

    # ── tasks ───────────────────────────────────────────────────────────────
    def add_task(self, title: str, project: str = "", due: float | None = None,
                 depends_on: Sequence[str] = (), milestone: str = "") -> dict:
        title = (title or "").strip()
        if not title:
            raise ProjectError("a task needs a title")
        with self._lock:
            data = self._load()
            blockers = [self._resolve(data, ref) for ref in depends_on]
            missing = [ref for ref, found in zip(depends_on, blockers, strict=True)
                       if not found]
            if missing:
                raise ProjectError(
                    f"nothing here matches: {', '.join(missing)}")

            task_id = uuid.uuid4().hex[:8]
            record = {
                "id": task_id, "title": title,
                "project": (project or "").strip().lower(),
                "milestone": (milestone or "").strip(),
                "due": due, "created": self._clock(),
                "depends_on": blockers, "status": OPEN, "done_at": None,
            }
            data["tasks"][task_id] = record
            # A cycle makes every task in it permanently unstartable, and the
            # symptom ("nothing is ever ready") is miles from the cause.
            if self._has_cycle(data["tasks"]):
                raise ProjectError(
                    f"that would make a circular dependency — {title!r} would "
                    f"end up waiting on itself")
            self._save(data)
        return record

    def _resolve(self, data: dict, ref: str) -> str:
        """A task id, or the id of the newest open task whose title matches."""
        ref = (ref or "").strip()
        if not ref:
            return ""
        if ref in data["tasks"]:
            return ref
        needle = ref.lower()
        matches = [t for t in data["tasks"].values()
                   if needle in t.get("title", "").lower()
                   and t.get("status") == OPEN]
        matches.sort(key=lambda t: t.get("created", 0), reverse=True)
        return matches[0]["id"] if matches else ""

    @staticmethod
    def _has_cycle(tasks: dict) -> bool:
        colour: dict[str, int] = {}

        def visit(node: str) -> bool:
            state = colour.get(node, 0)
            if state == 1:          # back-edge: on the current path
                return True
            if state == 2:
                return False
            colour[node] = 1
            for nxt in tasks.get(node, {}).get("depends_on", []):
                if nxt in tasks and visit(nxt):
                    return True
            colour[node] = 2
            return False

        return any(visit(task_id) for task_id in list(tasks))

    def _update(self, task_id: str, **fields) -> dict | None:
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                return None
            task.update(fields)
            self._save(data)
        return task

    def block_on(self, ref: str, blocker_ref: str) -> dict:
        """"Actually, X can't start until Y is done." — added after the fact.

        This is the only way a cycle can be created (`add_task` resolves
        dependencies against tasks that already exist, so a new task can only
        ever point backwards). Hence the check lives here as well: refuse the
        edit rather than leave two tasks permanently waiting on each other.
        """
        with self._lock:
            data = self._load()
            task_id = self._resolve(data, ref)
            blocker_id = self._resolve(data, blocker_ref)
            if not task_id:
                raise ProjectError(f"nothing here matches: {ref}")
            if not blocker_id:
                raise ProjectError(f"nothing here matches: {blocker_ref}")
            if task_id == blocker_id:
                raise ProjectError("a task can't wait on itself")

            task = data["tasks"][task_id]
            if blocker_id in task["depends_on"]:
                return task
            task["depends_on"] = [*task["depends_on"], blocker_id]
            if self._has_cycle(data["tasks"]):
                raise ProjectError(
                    f"that would make a circular dependency — "
                    f"{task['title']!r} would end up waiting on itself")
            self._save(data)
        return task

    def complete(self, ref: str) -> dict | None:
        data = self._load()
        task_id = self._resolve(data, ref)
        if not task_id:
            return None
        return self._update(task_id, status=DONE, done_at=self._clock())

    def drop(self, ref: str) -> dict | None:
        data = self._load()
        task_id = self._resolve(data, ref)
        if not task_id:
            return None
        return self._update(task_id, status=DROPPED)

    # ── the questions worth asking ──────────────────────────────────────────
    def tasks(self, project: str = "", include_done: bool = False) -> list[dict]:
        wanted = (project or "").strip().lower()
        found = [t for t in self._load()["tasks"].values()
                 if (not wanted or t.get("project") == wanted)
                 and (include_done or t.get("status") == OPEN)]
        # Dated work first, in date order; undated after it.
        return sorted(found, key=lambda t: (t.get("due") is None,
                                            t.get("due") or 0,
                                            t.get("created", 0)))

    def blockers(self, task: dict, tasks: dict | None = None) -> list[dict]:
        """The unfinished tasks this one is waiting on."""
        tasks = tasks if tasks is not None else self._load()["tasks"]
        return [tasks[i] for i in task.get("depends_on", [])
                if i in tasks and tasks[i].get("status") == OPEN]

    def ready(self, project: str = "") -> list[dict]:
        """Open tasks with nothing blocking them — what can start right now."""
        tasks = self._load()["tasks"]
        return [t for t in self.tasks(project) if not self.blockers(t, tasks)]

    def blocked(self, project: str = "") -> list[dict]:
        tasks = self._load()["tasks"]
        return [t for t in self.tasks(project) if self.blockers(t, tasks)]

    def due_soon(self, within_hours: float = SOON_HOURS,
                 project: str = "") -> list[dict]:
        """Dated open tasks inside the window, soonest first. Overdue included."""
        horizon = self._clock() + max(0.0, within_hours) * 3600
        return [t for t in self.tasks(project)
                if t.get("due") is not None and t["due"] <= horizon]

    def overdue(self, project: str = "") -> list[dict]:
        now = self._clock()
        return [t for t in self.tasks(project)
                if t.get("due") is not None and t["due"] < now]

    # ── spoken summaries ────────────────────────────────────────────────────
    def _when(self, due: float | None) -> str:
        if due is None:
            return ""
        delta = due - self._clock()
        if delta < 0:
            return " (overdue)"
        if delta < 3600:
            return f" (in {int(delta // 60)} min)"
        if delta < 86400:
            return f" (in {int(delta // 3600)}h)"
        return f" (in {int(delta // 86400)}d)"

    def summary(self, project: str = "") -> str:
        ready = self.ready(project)
        blocked = self.blocked(project)
        late = self.overdue(project)
        if not ready and not blocked:
            return ("Nothing on that project yet." if project
                    else "You've got nothing tracked right now.")
        parts = []
        if late:
            parts.append(f"{len(late)} overdue: " +
                         ", ".join(t["title"] for t in late[:3]))
        if ready:
            parts.append("ready to start: " +
                         ", ".join(t["title"] + self._when(t.get("due"))
                                   for t in ready[:3]))
        if blocked:
            parts.append(f"{len(blocked)} waiting on something else")
        return "; ".join(parts) + "."

    def explain(self, ref: str) -> str:
        """Why a task cannot start yet — the actual blocking chain."""
        data = self._load()
        task_id = self._resolve(data, ref)
        if not task_id:
            return f"I don't have a task matching {ref!r}."
        task = data["tasks"][task_id]
        blocking = self.blockers(task, data["tasks"])
        if not blocking:
            return f"{task['title']} is ready to start — nothing is blocking it."
        names = ", ".join(b["title"] for b in blocking)
        return f"{task['title']} is waiting on {names}."


def collect_due(store: ProjectStore, within_hours: float = SOON_HOURS
                ) -> Iterable[dict]:
    """Deadlines worth mentioning unprompted. Used by the ambient loop."""
    return store.due_soon(within_hours)
