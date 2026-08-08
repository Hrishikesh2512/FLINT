"""What worked, what didn't, and why she chose what she chose.

Three of the list's items are really one problem — the assistant has no memory
of its own behaviour:

    #21  learn the user's preferences over time and adapt
    #33  improve from past task outcomes
    #32  explain the reasoning behind important decisions

All three need the same thing first: a record of decisions and outcomes that
outlives the conversation. Without it "learning" can only mean asking a model
to guess, which is not learning, and "explaining" can only mean asking a model
to invent a plausible rationale after the fact, which is worse than nothing —
a confabulated explanation is actively misleading.

So this stores two kinds of fact and derives from them, rather than
generating:

    Decision  what she chose, the alternatives, and the reason — recorded at
              the moment of choosing, never reconstructed afterwards
    Outcome   what happened when something ran: succeeded or failed, how long,
              which agent or tool did it

`preferences()` and `advice()` read those back. Everything they return is
counted from real rows, and anything with too little evidence returns nothing
at all rather than a confident guess off two data points — an assistant that
"learns" your preferences from one incident is worse company than one that
doesn't try.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

log = logging.getLogger("flint.outcomes")

#: Rows kept on disk. Old behaviour stops being evidence about current
#: behaviour fairly quickly, and this is a wearable, not a data warehouse.
MAX_ROWS = 4000

#: Below this many observations, report nothing. The whole failure mode of
#: "learning" features is confident conclusions from three data points.
MIN_EVIDENCE = 5

#: A choice must win this share of the time before it counts as a preference.
PREFERENCE_THRESHOLD = 0.7


@dataclass(frozen=True)
class Decision:
    """A choice worth being able to explain later.

    `reason` is recorded when the choice is made, by whatever made it. It is
    never generated afterwards: an explanation invented after the fact is a
    plausible story about a decision, not the decision.
    """

    what: str                       # "which agent", "which model", "replan"
    chose: str
    reason: str
    alternatives: tuple[str, ...] = ()
    context: str = ""
    ts: float = 0.0
    kind: str = "decision"


@dataclass(frozen=True)
class Outcome:
    """What actually happened when something ran."""

    action: str                     # tool name, job type, agent name
    ok: bool
    seconds: float = 0.0
    detail: str = ""
    actor: str = ""                 # who did it: an agent, a runner, a tool
    ts: float = 0.0
    kind: str = "outcome"


@dataclass
class _Row:
    data: dict = field(default_factory=dict)


class OutcomeLog:
    """Append-only record of decisions and outcomes, with derived summaries."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self._path = Path(path) if path else None
        self._clock = clock
        self._lock = Lock()
        self._memory: list[dict] = []      # used when there is no path
        self._writes = 0

    # ── writing ─────────────────────────────────────────────────────────────
    def _append(self, row: dict) -> None:
        row["ts"] = row.get("ts") or round(self._clock(), 3)
        if self._path is None:
            with self._lock:
                self._memory.append(row)
                del self._memory[:-MAX_ROWS]
            return
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                self._writes += 1
                if self._writes >= 200:
                    self._writes = 0
                    self._trim()
        except OSError:
            log.warning("outcomes: could not write to %s", self._path)

    def _trim(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= MAX_ROWS:
            return
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".outcomes-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines[-MAX_ROWS:]) + "\n")
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def record_decision(self, what: str, chose: str, reason: str,
                        alternatives: Sequence[str] = (), context: str = "") -> None:
        if not (what.strip() and chose.strip() and reason.strip()):
            # A decision with no reason is not explainable, and storing it
            # would only invite one to be invented later.
            return
        self._append(asdict(Decision(
            what=what.strip(), chose=chose.strip(), reason=reason.strip(),
            alternatives=tuple(alternatives), context=context.strip())))

    def record_outcome(self, action: str, ok: bool, seconds: float = 0.0,
                       detail: str = "", actor: str = "") -> None:
        if not action.strip():
            return
        self._append(asdict(Outcome(
            action=action.strip(), ok=bool(ok), seconds=float(seconds),
            detail=detail.strip()[:300], actor=actor.strip())))

    # ── reading ─────────────────────────────────────────────────────────────
    def rows(self, kind: str = "", limit: int = 0) -> list[dict]:
        if self._path is None:
            found = list(self._memory)
        else:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
            found = []
            for line in lines:
                try:
                    found.append(json.loads(line))
                except ValueError:
                    continue        # a torn line from a power cut
        if kind:
            found = [r for r in found if r.get("kind") == kind]
        return found[-limit:] if limit else found

    # ── derived: what actually works ────────────────────────────────────────
    def reliability(self, minimum: int = MIN_EVIDENCE) -> dict[str, float]:
        """Success rate per action, for actions seen often enough to mean it."""
        totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for row in self.rows("outcome"):
            entry = totals[row.get("action", "")]
            entry[0] += 1
            entry[1] += 1 if row.get("ok") else 0
        return {action: hits / seen
                for action, (seen, hits) in totals.items()
                if seen >= minimum and action}

    def unreliable(self, below: float = 0.5) -> list[str]:
        """Actions that fail more often than they work — worth not choosing."""
        return sorted(a for a, rate in self.reliability().items() if rate < below)

    def typical_seconds(self, action: str) -> float:
        """How long this usually takes — for honest 'this will be a while'."""
        times = [float(r.get("seconds", 0)) for r in self.rows("outcome")
                 if r.get("action") == action and r.get("seconds")]
        if len(times) < 3:
            return 0.0
        times.sort()
        return times[len(times) // 2]           # median: robust to one 10-min outlier

    # ── derived: what the user prefers ──────────────────────────────────────
    def preferences(self, minimum: int = MIN_EVIDENCE) -> dict[str, str]:
        """Choices that have settled into a habit.

        A preference is only reported when one option wins clearly and often.
        Below either bar this returns nothing for that question — an assistant
        that announces your preferences after two incidents is worse company
        than one that never mentions them.
        """
        by_question: dict[str, Counter] = defaultdict(Counter)
        for row in self.rows("decision"):
            by_question[row.get("what", "")][row.get("chose", "")] += 1

        settled = {}
        for question, counts in by_question.items():
            total = sum(counts.values())
            if not question or total < minimum:
                continue
            choice, wins = counts.most_common(1)[0]
            if choice and wins / total >= PREFERENCE_THRESHOLD:
                settled[question] = choice
        return settled

    def advice(self) -> list[str]:
        """Short, evidence-backed notes for a system prompt. Empty when unsure."""
        notes = []
        for question, choice in sorted(self.preferences().items()):
            notes.append(f"For {question}, he almost always wants {choice}.")
        for action in self.unreliable():
            notes.append(f"{action} has failed more often than it has worked — "
                         f"prefer another way if there is one.")
        return notes

    def render_for_prompt(self) -> str:
        """The learned block for a system prompt, or "" when nothing is known."""
        notes = self.advice()
        if not notes:
            return ""
        return ("[WHAT YOU'VE LEARNED — from what actually happened before, "
                "not guesses. Act on it quietly; never recite it.]\n"
                + "\n".join(f"- {note}" for note in notes) + "\n")

    # ── derived: why she did that ───────────────────────────────────────────
    def explain(self, what: str = "", limit: int = 3) -> str:
        """The real recorded reasons for recent choices.

        Reads back what was stored at the time. If nothing was recorded it
        says so, rather than producing a plausible-sounding reconstruction.
        """
        rows = [r for r in self.rows("decision")
                if not what or what.lower() in r.get("what", "").lower()]
        if not rows:
            return ("I didn't record a reason for that one." if what
                    else "I haven't recorded any decisions yet.")
        parts = []
        for row in rows[-limit:]:
            line = f"{row.get('what')}: chose {row.get('chose')} — {row.get('reason')}"
            others = row.get("alternatives") or []
            if others:
                line += f" (over {', '.join(others)})"
            parts.append(line)
        return "; ".join(parts) + "."
