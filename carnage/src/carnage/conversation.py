"""One turn of conversation, with tools, over plain text.

Venom holds conversations through the Gemini Live API: audio in, audio out,
tool calls dispatched by the session. That is the right shape for a wearable
with a headset and no screen, and the wrong shape for a page in someone's
hand — a phone already has a keyboard, a scrollback and a lock screen, and a
live audio socket that has to survive all three is a much bigger problem than
the one being solved here.

So this is the text half: a small loop over the shared `LLMGateway`, which has
`chat` and `chat_json` but no tool calling of its own. The loop supplies it —

    1. the model is shown the tools, generated from the registry, never
       hand-written, so a tool it is told about always exists
    2. it answers with JSON: either something to say, or a tool to call
    3. a tool call is dispatched, its result appended, and round we go
    4. a budget stops the loop, because a model that has decided to call
       `check_timers` forever should cost a handful of calls, not a month

The reason it answers in JSON rather than a native tool-call format is that
the gateway can route to any provider, and their tool-call schemas disagree.
JSON is the format every one of them can produce, and `chat_json` already
strips the code fences they all wrap it in anyway.

**Her words are never JSON.** The `say` field is what reaches the person, and
it is plain prose in her own voice — the structure exists between the loop and
the model, and stops at the edge of it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

log = logging.getLogger("carnage.conversation")

#: How many tool calls one turn may make before she has to answer with what
#: she has. Enough for "look up where he is, then check the weather there";
#: not enough for a loop to become a bill.
MAX_STEPS = 5

#: How much of a tool's output goes back to the model. A web search can return
#: pages; the model needs the gist and the person needs an answer this decade.
MAX_RESULT = 2000

PROTOCOL = """
You reply with ONE JSON object and nothing else. Two shapes:

  {"say": "what you actually say out loud, in your own voice"}
  {"tool": "<tool name>", "args": {...}, "say": "a few words first"}

Use a tool whenever the answer depends on something real — the time, the
weather, where he is, his lists, his people, anything you would otherwise be
guessing at. Never invent a tool that is not listed. Never put JSON, braces or
field names into `say`; that is the part he reads, and it is prose.

When you call a tool, `say` is the half-second acknowledgement that stops dead
air ("ruk, dekhti hoon"), not the answer. You get the result back and then say
the real thing.
""".strip()


class Conversation:
    """A running text conversation with one person, on one device."""

    def __init__(self, gateway, registry, instruction: Callable[[], str],
                 max_steps: int = MAX_STEPS, history: int = 12):
        self._gateway = gateway
        self._registry = registry
        self._instruction = instruction
        self._max_steps = max(1, max_steps)
        self._history_limit = max(2, history)
        self.history: list[dict] = []

    # ── the turn ────────────────────────────────────────────────────────────
    def ask(self, said: str) -> str:
        """What she says back. Never raises, never returns JSON."""
        said = (said or "").strip()
        if not said:
            return "You didn't say anything."
        self._remember("him", said)

        spoken: list[str] = []
        notes: list[str] = []
        for step in range(self._max_steps):
            try:
                move = self._next_move(said, notes)
            except Exception as exc:            # noqa: BLE001
                log.exception("conversation: the model call failed")
                return self._fallback(spoken, exc)

            aside = str(move.get("say", "")).strip()
            tool = str(move.get("tool", "")).strip()

            if not tool:
                answer = aside or "…"
                self._remember("her", answer)
                return " ".join([*spoken, answer]).strip()

            if aside:
                spoken.append(aside)
            notes.append(self._run(tool, move.get("args")))
            log.debug("conversation: step %d called %s", step + 1, tool)

        # Out of budget. Answer with what was actually gathered rather than
        # silently truncating — she has real results in hand at this point.
        closing = self._summarise(said, notes)
        self._remember("her", closing)
        return " ".join([*spoken, closing]).strip()

    # ── the model ───────────────────────────────────────────────────────────
    def _next_move(self, said: str, notes: list[str]) -> dict:
        prompt = [f"He said: {said}"]
        if notes:
            prompt.append("What your tools just came back with:")
            prompt.extend(notes)
            prompt.append("Answer him now unless you genuinely need another "
                          "tool.")
        reply = self._gateway.chat_json(
            "\n\n".join(prompt),
            system=self._system(),
            temperature=0.6,
            task="chat")
        return reply if isinstance(reply, dict) else {"say": str(reply)}

    def _system(self) -> str:
        return "\n\n".join([
            self._instruction(),
            self._recent(),
            "[YOUR TOOLS]\n" + self._registry.planner_documentation(),
            "[HOW TO REPLY]\n" + PROTOCOL,
        ])

    def _summarise(self, said: str, notes: list[str]) -> str:
        try:
            reply = self._gateway.chat(
                f"He said: {said}\n\nYour tools came back with:\n"
                + "\n".join(notes)
                + "\n\nAnswer him now, in one or two spoken sentences.",
                system=self._instruction(), temperature=0.6, task="chat")
            return reply.text.strip() or "I've got what I need."
        except Exception:                       # noqa: BLE001
            log.exception("conversation: could not summarise")
            return "I looked into it but couldn't put it together just now."

    # ── tools ───────────────────────────────────────────────────────────────
    def _run(self, name: str, args) -> str:
        if name not in self._registry:
            # Being specific matters: the model can correct a wrong name, and
            # cannot correct "that failed".
            return f"[{name}] there is no such tool. Use one from the list."
        try:
            result = self._registry.dispatch(
                name, args if isinstance(args, dict) else {})
        except Exception as exc:                # noqa: BLE001
            log.info("conversation: %s failed: %s", name, exc)
            return f"[{name}] did not work: {exc}"
        text = "" if result is None else str(result)
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + " …(cut)"
        return f"[{name}] {text}"

    # ── memory of the last few turns ────────────────────────────────────────
    def _remember(self, who: str, text: str) -> None:
        self.history.append({"who": who, "text": text})
        del self.history[:-self._history_limit]

    def _recent(self) -> str:
        if len(self.history) <= 1:
            return ""
        lines = [f"{'He' if t['who'] == 'him' else 'You'}: {t['text']}"
                 for t in self.history[:-1]]
        return "[THE LAST FEW THINGS SAID]\n" + "\n".join(lines)

    def _fallback(self, spoken: list[str], exc: Exception) -> str:
        # She has already said the acknowledgement out loud by this point, so
        # going silent would leave a dangling "ruk, dekhti hoon" and nothing
        # after it.
        said = " ".join(spoken).strip()
        excuse = "Sorry — I couldn't get through to think just then."
        log.info("conversation: falling back after %s", exc)
        return f"{said} {excuse}".strip()


def transcript(history: list[dict]) -> str:
    """The conversation as plain text, for the page to render."""
    return json.dumps(history, ensure_ascii=False)
