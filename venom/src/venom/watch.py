"""Delegated watches — the jobs she goes away and does, then reports back on.

Everything else in Venom answers when asked, and `ambient.py` volunteers
things she noticed on her own. This is the third mode: you hand her a job
and stop thinking about it, and some time later *she* comes back with the
answer. "Watch the match and tell me when India needs under twenty." "Tell
me when that result is out." She checks on a schedule, in the background,
and opens a conversation the moment the condition is actually met.

Each check is two model calls, and the split is the whole design:

    look   ->  a Google-grounded search, so the facts are fresh off the web
               rather than recalled from training
    judge  ->  a small JSON completion that compares those facts against the
               user's condition and the previous observation, and says only
               met / not met plus what changed

Keeping the judging separate is what makes a watch trustworthy. A single
"search and tell me if it happened" call will cheerfully fire on a stale or
half-read page; a verdict that must name the current observation, in JSON,
next to the previous one, mostly won't.

Two things are deliberately strict, because both failure modes are the kind
that get a feature switched off for good:

  * **Cost.** A watch is a loop that calls a paid API forever if you let it.
    Every watch therefore carries a minimum interval, a check budget and a
    wall-clock expiry, and only a handful may run at once. A watch that
    never fires dies quietly instead of billing you all month.

  * **Interrupting.** Watches were *requested*, so unlike an ambient nudge
    they are not rationed by the nag-gate — you asked to be told, you get
    told. But a result landing at 3am is still a bad result: a watch that
    fires during quiet hours holds its line and delivers it when quiet
    hours end, unless it was set up as urgent.

She speaks through `queue_proactive`, the same single door the ambient loop
and WhatsApp announcements use — there is still exactly one path by which
Venom opens its mouth unprompted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from flint_core.llm.base import ChatMessage

from venom.stores import _JsonStore

log = logging.getLogger("venom.watch")

# The judge runs on facts someone else already fetched, so it needs no room to
# think and very little room to answer — this is a classification, not an essay.
_VERDICT_TOKENS = 400

_JUDGE_SYSTEM = (
    "You decide whether a watch condition has been met. You are given fresh "
    "facts from a web search, what the user asked you to watch for, and what "
    "was observed on the previous check.\n\n"
    "Reply with JSON only:\n"
    '  {"met": true|false, "observation": "<the current state, one short '
    'factual line>", "say": "<what to tell the user, one short line — only '
    'if met>"}\n\n'
    "Rules:\n"
    "- met = true ONLY if the condition is clearly and currently satisfied by "
    "the facts. If the facts are stale, vague, missing or don't actually "
    "answer the question, met = false. Never guess to be helpful.\n"
    "- If no condition was given, met = true when the current state differs "
    "MATERIALLY from the previous observation (a real change, not rewording).\n"
    "- If there is no previous observation, met = false: that check is only "
    "establishing the baseline.\n"
    "- 'observation' is always required, met or not — it becomes the previous "
    "observation for the next check.\n"
    "- 'say' is plain facts, not a greeting and not a full sentence of "
    "narration. Empty string when met is false."
)


def _verdict_prompt(what: str, condition: str, previous: str) -> str:
    return (
        f"WATCHING: {what}\n"
        f"CONDITION: {condition or '(none given — fire on a material change)'}\n"
        f"PREVIOUS OBSERVATION: {previous or '(none yet — this is the first check)'}"
    )


def check_watch(provider, watch: dict) -> dict | None:
    """Run one check. Returns the verdict dict, or None if the check failed.

    Pure with respect to storage — the caller records the result. Any
    provider or parsing failure returns None so the watch simply tries again
    on its next tick, rather than firing on a half-read answer.
    """
    what = watch.get("what", "")
    condition = watch.get("condition", "")
    previous = watch.get("observation", "")

    try:
        facts = provider.grounded_search(
            f"{what}. Give the current facts only, briefly, with times or "
            "numbers where they exist. If you cannot find current information, "
            "say exactly that."
        )
    except Exception as exc:  # noqa: BLE001 — a flaky search is not a crash
        log.warning("watch %s: search failed: %s", watch.get("id"), exc)
        return None

    messages = (
        ChatMessage("system", _JUDGE_SYSTEM),
        ChatMessage("user", f"FACTS FROM THE WEB:\n{facts}\n\n"
                            f"{_verdict_prompt(what, condition, previous)}"),
    )
    try:
        raw = provider.complete(messages, provider.models[0],
                                max_tokens=_VERDICT_TOKENS, temperature=0.0,
                                json_mode=True)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — bad JSON, rate limit, anything
        log.warning("watch %s: verdict failed: %s", watch.get("id"), exc)
        return None
    if not isinstance(data, dict):
        return None

    observation = str(data.get("observation") or "").strip()
    met = bool(data.get("met")) and bool(observation)
    say = str(data.get("say") or "").strip()
    if met and not say:  # met with nothing to report is not a usable result
        say = observation
    return {"met": met, "observation": observation, "say": say}


class WatchStore(_JsonStore):
    """The watch list on disk, so a job outlives a reboot like a reminder does.

    A watch moves through: active -> (fires) -> held -> delivered & gone. It
    is removed once spoken, because a watch is a question asked once; a
    standing subscription would be a different feature with a different
    off-switch.
    """

    MAX_ACTIVE = 5             # concurrent watches — each one costs API calls
    MIN_INTERVAL = 120.0       # seconds; below this the bill outruns the value
    DEFAULT_INTERVAL = 600.0
    DEFAULT_TTL_HOURS = 24.0
    MAX_CHECKS = 120

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        super().__init__(path)
        self._clock = clock

    # ── writing ─────────────────────────────────────────────────────────────
    def add(self, what: str, condition: str = "", interval: float | None = None,
            ttl_hours: float | None = None, urgent: bool = False) -> dict:
        """Start a watch. Raises ValueError when the limits say no."""
        what = (what or "").strip()
        if not what:
            raise ValueError("a watch needs something to watch")
        now = self._clock()
        with self._lock:
            data = [w for w in self._load([]) if not self._dead(w, now)]
            if len([w for w in data if w.get("status") == "active"]) >= self.MAX_ACTIVE:
                raise ValueError(
                    f"already watching {self.MAX_ACTIVE} things — that's the "
                    "limit. Drop one first.")
            entry = {
                "id": uuid.uuid4().hex[:8],
                "what": what,
                "condition": (condition or "").strip(),
                "interval": max(self.MIN_INTERVAL,
                                float(interval or self.DEFAULT_INTERVAL)),
                "urgent": bool(urgent),
                "created": now,
                "last_check": 0.0,
                "checks": 0,
                "expires": now + float(ttl_hours or self.DEFAULT_TTL_HOURS) * 3600,
                "observation": "",
                "say": "",
                "status": "active",
            }
            data.append(entry)
            self._save(data)
        return entry

    def _update(self, watch_id: str, **fields) -> None:
        with self._lock:
            data = self._load([])
            for w in data:
                if w.get("id") == watch_id:
                    w.update(fields)
                    break
            else:
                return
            self._save(data)

    def record_check(self, watch_id: str, observation: str) -> None:
        """A check ran and did not fire — remember what it saw."""
        with self._lock:
            data = self._load([])
            for w in data:
                if w.get("id") == watch_id:
                    w["last_check"] = self._clock()
                    w["checks"] = int(w.get("checks", 0)) + 1
                    if observation:
                        w["observation"] = observation
                    break
            else:
                return
            self._save(data)

    def mark_touched(self, watch_id: str) -> None:
        """A check was attempted but failed — don't retry it instantly."""
        self._update(watch_id, last_check=self._clock())

    def mark_held(self, watch_id: str, say: str, observation: str) -> None:
        """Condition met: hold the line until it's a decent time to say it."""
        self._update(watch_id, status="held", say=say, observation=observation,
                     last_check=self._clock(), fired_at=self._clock())

    def drop(self, watch_id: str) -> None:
        with self._lock:
            data = [w for w in self._load([]) if w.get("id") != watch_id]
            self._save(data)

    def cancel(self, text: str = "") -> int:
        """Stop watches whose description matches `text` — all of them if blank."""
        needle = (text or "").strip().lower()
        with self._lock:
            data = self._load([])
            if needle:
                keep = [w for w in data if needle not in w.get("what", "").lower()]
            else:
                keep = []
            removed = len(data) - len(keep)
            if removed:
                self._save(keep)
        return removed

    # ── reading ─────────────────────────────────────────────────────────────
    def _dead(self, watch: dict, now: float) -> bool:
        """Expired or out of budget — true for anything that should stop."""
        if watch.get("status") == "held":
            return False       # a held result is still owed to the user
        return (now >= watch.get("expires", 0)
                or int(watch.get("checks", 0)) >= self.MAX_CHECKS)

    def all(self) -> list[dict]:
        return self._load([])

    def active(self) -> list[dict]:
        now = self._clock()
        return [w for w in self._load([])
                if w.get("status") == "active" and not self._dead(w, now)]

    def due(self) -> list[dict]:
        """Active watches whose interval has elapsed."""
        now = self._clock()
        return [w for w in self.active()
                if now - float(w.get("last_check", 0)) >= float(w.get("interval", 600))]

    def held(self) -> list[dict]:
        """Fired watches waiting for a decent moment to be spoken."""
        return [w for w in self._load([]) if w.get("status") == "held"]

    def prune(self) -> int:
        """Bin watches that ran out of time or checks without ever firing."""
        now = self._clock()
        with self._lock:
            data = self._load([])
            keep = [w for w in data if not self._dead(w, now)]
            removed = len(data) - len(keep)
            if removed:
                self._save(keep)
        return removed

    # ── spoken summary ──────────────────────────────────────────────────────
    def summary(self) -> str:
        watches = self.active() + self.held()
        if not watches:
            return "I'm not watching anything for you right now."
        lines = []
        for w in watches:
            bit = w.get("what", "")
            if w.get("condition"):
                bit += f" — until {w['condition']}"
            if w.get("status") == "held":
                bit += " (done, waiting to tell you)"
            lines.append(bit)
        head = "I'm watching " if len(lines) == 1 else f"I'm watching {len(lines)} things: "
        return head + "; ".join(lines) + "."


def watch_instruction(watch: dict) -> str:
    """The [Proactive] opening handed to the live session when a watch fires."""
    return (
        f"[Proactive] You were watching this for him: {watch.get('what', '')}. "
        f"It has now happened — {watch.get('say', '')} You are opening this "
        f"conversation yourself: he asked you to watch this a while ago, has "
        f"been doing something else since, and does not know you're about to "
        f"speak. Lead with ONE short, specific Hinglish sentence giving him "
        f"the actual result, and remind him in the same breath that this is "
        f"the thing he asked you to keep an eye on. Do not greet him, do not "
        f"ask how he is, do not offer generic help."
    )


class WatchLoop:
    """Runs due checks off the event loop and hands fired watches to the mouth.

    Mirrors AmbientLoop: blocking work in a thread, `speak` is the only way
    out, and any failure is logged and swallowed — a broken watch must never
    take down a working assistant.
    """

    def __init__(self, store: WatchStore, provider_factory: Callable[[], object],
                 speak: Callable[[str], None], is_busy: Callable[[], bool],
                 *, tick_seconds: float = 60.0,
                 in_quiet_hours: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.time):
        self._store = store
        self._provider_factory = provider_factory
        self._speak = speak
        self._is_busy = is_busy
        self._tick_seconds = float(tick_seconds)
        self._quiet = in_quiet_hours or (lambda: False)
        self._clock = clock

    # ── checking ────────────────────────────────────────────────────────────
    def _run_checks(self) -> None:
        """Blocking: check every due watch. Call via asyncio.to_thread."""
        due = self._store.due()
        if not due:
            return
        try:
            provider = self._provider_factory()
        except Exception as exc:  # noqa: BLE001 — no API key, no watches
            log.warning("watch: no model provider (%s)", exc)
            return
        for watch in due:
            verdict = check_watch(provider, watch)
            if verdict is None:
                self._store.mark_touched(watch["id"])
                continue
            if verdict["met"]:
                log.info("watch fired: %s (%s)", watch["id"], watch.get("what"))
                self._store.mark_held(watch["id"], verdict["say"],
                                      verdict["observation"])
            else:
                self._store.record_check(watch["id"], verdict["observation"])

    # ── speaking ────────────────────────────────────────────────────────────
    def _deliver(self) -> bool:
        """Speak one held result, if now is a reasonable moment. True if spoken."""
        held = self._store.held()
        if not held:
            return False
        if self._is_busy():
            return False
        for watch in held:
            if self._quiet() and not watch.get("urgent"):
                continue    # hold it — a result at 3am is still a bad result
            self._speak(watch_instruction(watch))
            self._store.drop(watch["id"])
            return True     # one interruption at a time
        return False

    async def tick(self) -> None:
        # Deliver first: a result already in hand beats spending API calls.
        if self._deliver():
            return
        self._store.prune()
        if self._is_busy():
            return          # mid-conversation, the checks can wait a tick
        await asyncio.to_thread(self._run_checks)
        self._deliver()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watch tick failed")
            await asyncio.sleep(self._tick_seconds)
