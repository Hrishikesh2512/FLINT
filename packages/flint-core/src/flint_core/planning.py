"""Plans whose steps can actually use each other's results.

FLINT's planner carried this rule, in capitals, at the top of its prompt:

    NEVER reference previous step results in parameters. Every step is
    independent.

Which is why it also carried a five-step cap — independent steps cannot build
on each other, so more of them buys nothing — and why `agent/executor.py` grew
`_inject_context`: a hard-coded special case that, for `file_controller` writes
only, joined every previous result over 100 characters together and dropped
them into `content`. One tool, one parameter, one guess about which results
mattered. Anything else — search, then summarise the *first* result; write a
file, then open *that* file — was simply not expressible.

A plan here is a small dependency graph. Steps refer to earlier results by
number:

    {"step": 3, "tool": "file_controller",
     "parameters": {"action": "write", "name": "notes.txt",
                    "content": "{{step:1}}\\n\\n{{step:2}}"}}

References are resolved from real results at execution time, and — the part
that matters — **validated before anything runs**. A plan that points at a
step that does not exist, or at itself, or forward in time, is rejected while
there is still someone to ask for a better one. Discovering it halfway through
means the first half already happened.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("flint.planning")

#: `{{step:2}}` — whitespace tolerated, because models produce `{{ step: 2 }}`.
STEP_REF = re.compile(r"\{\{\s*step\s*:\s*(\d+)\s*\}\}", re.IGNORECASE)

#: `{{goal}}` — the original request, useful for summarise/translate steps.
GOAL_REF = re.compile(r"\{\{\s*goal\s*\}\}", re.IGNORECASE)


class PlanError(Exception):
    """The plan is not runnable. Ask the planner again rather than starting it."""


def _referenced(value: Any) -> set[int]:
    """Every step number mentioned anywhere inside a parameter value."""
    if isinstance(value, str):
        return {int(n) for n in STEP_REF.findall(value)}
    if isinstance(value, Mapping):
        return set().union(*(_referenced(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_referenced(v) for v in value)) if value else set()
    return set()


@dataclass(frozen=True)
class PlanStep:
    step: int
    tool: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: False means "carry on if this fails" — the executor skips rather than
    #: replanning. Steps other steps depend on should stay critical.
    critical: bool = True

    def __post_init__(self) -> None:
        if not str(self.tool).strip():
            raise PlanError(f"step {self.step} has no tool")

    def references(self) -> frozenset[int]:
        return frozenset(_referenced(dict(self.parameters)))


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: tuple[PlanStep, ...] = ()

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: Mapping[str, Any], goal: str = "") -> Plan:
        """Build from the planner's JSON. Raises PlanError on anything unusable."""
        if not isinstance(data, Mapping):
            raise PlanError("plan is not an object")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanError("plan has no steps")

        steps = []
        for index, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, Mapping):
                raise PlanError(f"step {index} is not an object")
            parameters = raw.get("parameters") or {}
            if not isinstance(parameters, Mapping):
                raise PlanError(f"step {index}: parameters must be an object")
            steps.append(PlanStep(
                # Models number steps inconsistently (0-based, skipping,
                # repeating). Position is the truth; the model's number is a
                # hint we discard, so references always mean the same thing.
                step=index,
                tool=str(raw.get("tool", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                parameters=dict(parameters),
                critical=bool(raw.get("critical", True)),
            ))
        return cls(goal=str(data.get("goal") or goal), steps=tuple(steps))

    # ── validation ──────────────────────────────────────────────────────────
    def problems(self, known_tools: Iterable[str] = ()) -> list[str]:
        """Everything wrong with this plan, in the order a reader would care.

        Returned rather than raised so the caller can feed them back to the
        planner: "you referenced step 5, there are only 3" is a far better
        prompt for a retry than "invalid plan".
        """
        found: list[str] = []
        tools = set(known_tools)
        count = len(self.steps)

        for step in self.steps:
            if tools and step.tool not in tools:
                found.append(f"step {step.step}: no such tool {step.tool!r}")
            for target in sorted(step.references()):
                if target < 1 or target > count:
                    found.append(
                        f"step {step.step} references step {target}, "
                        f"but the plan has {count} step(s)")
                elif target == step.step:
                    found.append(f"step {step.step} references itself")
                elif target > step.step:
                    # The result does not exist yet. Left as an error rather
                    # than reordering: a plan that thinks it can read the
                    # future is a plan whose author misunderstood the task.
                    found.append(
                        f"step {step.step} references step {target}, "
                        f"which has not run yet")
        return found

    def validate(self, known_tools: Iterable[str] = ()) -> Plan:
        found = self.problems(known_tools)
        if found:
            raise PlanError("; ".join(found))
        return self

    # ── execution support ───────────────────────────────────────────────────
    def resolve(self, step: PlanStep, results: Mapping[int, Any]) -> dict[str, Any]:
        """This step's parameters with every reference replaced by real output.

        A referenced step that produced nothing substitutes empty rather than
        leaving the placeholder in place — a tool receiving a literal
        "{{step:2}}" would write that string into a file and call it success.
        """
        def substitute(value: Any) -> Any:
            if isinstance(value, str):
                def replace(match: re.Match) -> str:
                    return str(results.get(int(match.group(1)), "") or "")

                return GOAL_REF.sub(lambda _: self.goal, STEP_REF.sub(replace, value))
            if isinstance(value, Mapping):
                return {k: substitute(v) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v) for v in value]
            return value

        return {key: substitute(value) for key, value in dict(step.parameters).items()}

    def depends_on(self, step: PlanStep) -> frozenset[int]:
        return step.references()

    def describe(self) -> str:
        lines = [f"Goal: {self.goal}"]
        for step in self.steps:
            refs = sorted(step.references())
            after = f"  (uses step {', '.join(map(str, refs))})" if refs else ""
            lines.append(f"  {step.step}. [{step.tool}] {step.description}{after}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self):
        return iter(self.steps)
