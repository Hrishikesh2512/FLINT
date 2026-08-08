"""Choosing *which* model does a piece of work, not just which one is up.

`LLMGateway` already picks a provider — but only by availability: it walks the
configured order and takes the first thing that answers. Every request gets
the same model whether it is "haan yaar" or "why is this deadlocking". That is
the right behaviour for failover and the wrong one for cost and quality: the
cheap fast model is wasted on hard reasoning, and the expensive one is wasted
on classifying a sentence.

Routing adds the missing dimension — what *kind* of work is this?

    Task.CHAT       conversational, latency is the whole experience
    Task.REASONING  multi-step thinking where being right matters most
    Task.CODE       writing or debugging code
    Task.VISION     there is an image involved
    Task.BULK       high-volume mechanical work: classify, extract, judge

Each task carries a preference — speed, quality or cost — and the router
orders candidate models by it. A chat turn takes the fastest model that can
do the job; a reasoning turn takes the strongest; a bulk judge takes the
cheapest that is good enough.

**No prices in here.** Real per-token costs change constantly and vary by
region and contract; a table of them in source would be wrong within weeks and
wrong silently. Models are ranked into coarse tiers instead, and the host
supplies the catalogue from config — so updating it is an edit to a TOML file,
not a release.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

log = logging.getLogger("flint.llm.routing")


class Task:
    """What kind of work a request is. Routing keys off this and nothing else."""

    CHAT = "chat"
    REASONING = "reasoning"
    CODE = "code"
    VISION = "vision"
    BULK = "bulk"

    ALL = (CHAT, REASONING, CODE, VISION, BULK)


class Tier:
    """Coarse capability/cost rank. Deliberately not a price."""

    CHEAP = "cheap"
    STANDARD = "standard"
    PREMIUM = "premium"

    ORDER = {CHEAP: 0, STANDARD: 1, PREMIUM: 2}

    @classmethod
    def rank(cls, tier: str) -> int:
        return cls.ORDER.get(tier, cls.ORDER[cls.STANDARD])


#: What each task optimises for when several models can do it.
#:   speed   — first token matters more than the last word (spoken reply)
#:   quality — being right is worth the money and the wait
#:   cost    — mechanical work at volume; good enough is good enough
PREFERENCE = {
    Task.CHAT: "speed",
    Task.REASONING: "quality",
    Task.CODE: "quality",
    Task.VISION: "quality",
    Task.BULK: "cost",
}


@dataclass(frozen=True)
class ModelSpec:
    """One routable model: who serves it, and what it is worth using for."""

    provider: str
    model: str
    good_at: frozenset[str] = field(default_factory=frozenset)
    tier: str = Tier.STANDARD
    fast: bool = False
    vision: bool = False

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("a model spec needs a provider and a model")
        unknown = set(self.good_at) - set(Task.ALL)
        if unknown:
            raise ValueError(f"{self.model}: unknown task(s) {sorted(unknown)}")

    def suits(self, task: str) -> bool:
        # An empty good_at means "general purpose" — usable for anything,
        # just never preferred over a model that names the task.
        return not self.good_at or task in self.good_at


class Router:
    """Orders the models that could serve a task, best candidate first.

    Ordering only — the gateway still walks the list and still falls through
    on error or rate limit, so routing never costs availability. A router with
    nothing suitable returns everything rather than nothing: a worse model is
    always better than no answer.
    """

    def __init__(self, catalogue: Iterable[ModelSpec] = ()):
        self._catalogue = list(catalogue)

    def __len__(self) -> int:
        return len(self._catalogue)

    def __iter__(self):
        return iter(self._catalogue)

    def add(self, spec: ModelSpec) -> None:
        self._catalogue.append(spec)

    def candidates(self, task: str, *, need_vision: bool = False,
                   max_tier: str | None = None) -> list[ModelSpec]:
        """Models that can serve `task`, in the order they should be tried."""
        pool = [s for s in self._catalogue if not need_vision or s.vision]
        if max_tier is not None:
            ceiling = Tier.rank(max_tier)
            pool = [s for s in pool if Tier.rank(s.tier) <= ceiling]
        if not pool:
            # Every filter is a preference, not a promise. Better to answer
            # with the wrong-tier model than to fail the request outright.
            pool = list(self._catalogue)

        # Ordered, never filtered: a model that is a poor fit still sorts last
        # rather than disappearing, so a caller walking this list always has
        # somewhere left to fall.
        return sorted(pool, key=lambda s: self._key(s, task))

    def _key(self, spec: ModelSpec, task: str) -> tuple:
        """Sort key: fit first, then whatever this task optimises for."""
        if task in spec.good_at:
            fit = 0                      # named for exactly this work
        elif not spec.good_at:
            fit = 1                      # general purpose
        else:
            fit = 2                      # specialised in something else
        preference = PREFERENCE.get(task, "quality")
        if preference == "speed":
            return (fit, 0 if spec.fast else 1, Tier.rank(spec.tier))
        if preference == "cost":
            return (fit, Tier.rank(spec.tier), 0 if spec.fast else 1)
        return (fit, -Tier.rank(spec.tier), 0 if spec.fast else 1)

    def pick(self, task: str, **kw) -> ModelSpec | None:
        """The single best candidate, or None when the catalogue is empty."""
        found = self.candidates(task, **kw)
        return found[0] if found else None

    def describe(self) -> str:
        lines = []
        for task in Task.ALL:
            chosen = self.pick(task)
            where = f"{chosen.provider}/{chosen.model}" if chosen else "—"
            lines.append(f"{task:<10} ({PREFERENCE[task]:<7}) -> {where}")
        return "\n".join(lines)


# ── automatic classification ────────────────────────────────────────────────
# Heuristics, on purpose. Asking a model which model to use costs a round trip
# on every request and gets the easy cases wrong often enough to be annoying;
# these patterns settle the easy cases for free. A caller who knows better
# should pass `task=` explicitly and skip this entirely.

_CODE_HINTS = re.compile(
    r"\b(traceback|stack ?trace|exception|compile|refactor|debug|syntax|"
    r"unit test|regex|api|sql|json|yaml|docker|git|npm|pip|"
    r"function|method|class|variable|import|def |async |=>|\{\}|\(\))\b"
    r"|\.(py|js|ts|tsx|jsx|go|rs|java|c|cpp|h|sh|sql|html|css|toml|ya?ml)\b"
    r"|```",
    re.IGNORECASE,
)

_REASONING_HINTS = re.compile(
    r"\b(why|how come|explain|compare|trade-?offs?|decide|design|plan|"
    r"strategy|analy[sz]e|evaluate|prove|derive|implications?|"
    r"step by step|reason|figure out|work out)\b",
    re.IGNORECASE,
)

# Only markers that are distinctive of mechanical work. Anything that also
# appears in ordinary speech is left out on purpose: "is this" and "does this"
# were both here at first and swallowed plain questions like "why is this
# deadlocking", sending real reasoning to the cheapest model on the shelf.
_BULK_HINTS = re.compile(
    r"\b(classify|categori[sz]e|extract|label it|tag it|"
    r"yes or no|true or false|one word answer|"
    r"return only|reply with only|respond with only|output only|"
    r"respond with json|answer with json)\b",
    re.IGNORECASE,
)

#: Below this, a request is almost always conversational rather than a task.
CHAT_LENGTH = 120


def classify(text: str, *, has_image: bool = False) -> str:
    """Guess the task class of a request. Heuristic — override with `task=`.

    Order matters: an image beats everything (only a vision model can even
    see it), then the mechanical-work markers, which are the most distinctive
    and the ones where sending a premium model would be pure waste.
    """
    if has_image:
        return Task.VISION
    body = (text or "").strip()
    if not body:
        return Task.CHAT
    if _BULK_HINTS.search(body):
        return Task.BULK
    if _CODE_HINTS.search(body):
        return Task.CODE
    if _REASONING_HINTS.search(body) and len(body) > 40:
        return Task.REASONING
    if len(body) > CHAT_LENGTH:
        # Long and none of the above: someone is explaining a problem.
        return Task.REASONING
    return Task.CHAT


def catalogue_from_config(entries: Sequence[dict]) -> Router:
    """Build a Router from plain config dicts (TOML `[[model]]` blocks).

    Unknown keys are ignored and a malformed entry is skipped with a warning
    rather than taking the process down: a typo in the model table must not
    stop an assistant from booting.
    """
    specs = []
    for entry in entries or ():
        try:
            specs.append(ModelSpec(
                provider=str(entry.get("provider", "")).strip(),
                model=str(entry.get("model", "")).strip(),
                good_at=frozenset(str(t).strip().lower()
                                  for t in entry.get("good_at", []) if str(t).strip()),
                tier=str(entry.get("tier", Tier.STANDARD)).strip().lower(),
                fast=bool(entry.get("fast", False)),
                vision=bool(entry.get("vision", False)),
            ))
        except (ValueError, AttributeError, TypeError) as exc:
            log.warning("routing: skipping bad model entry %r (%s)", entry, exc)
    return Router(specs)
