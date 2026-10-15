"""The voice model, for everything that is not a voice call.

Carnage thinks with one model only: the native-audio Live model her voice
calls use (`config.live_model`). It lives on the Live API's websocket and
answers in audio, never text — so a typed question opens a short Live
session, and her reply is the transcript of what she would have said.

Everything that needs a model goes through here:

- **typed conversation** (`LiveConversation`), with her tools as Live
  function calls, exactly as in a voice call
- **seeing**: the camera, his screen, photos and PDFs he shares (`describe`)
- **web search**, with Google Search switched on in the session (`search`)
- **using his apps** (`decide`): each move is a function call, so nothing
  has to be spoken as JSON

One session per question. A session is cheap to open and keeps nothing
between questions; the conversation's memory of the last few turns is sent
as earlier turns in each one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("carnage.livetext")

LIVE_URL = ("wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService."
            "BidiGenerateContent")

#: How many tool calls one typed question may make before she must answer.
MAX_TOOL_CALLS = 8

#: A typed answer that has not finished in this long is given up on.
TIMEOUT = 60.0


class LiveError(RuntimeError):
    """The session could not be opened, or ended without an answer."""


def _connect(url: str):
    from websockets.sync.client import connect

    # Live frames carry base64 audio; the default 1 MB cap is too small.
    return connect(url, open_timeout=15, max_size=None)


class LiveModel:
    """Short, text-out sessions with the voice model."""

    def __init__(self, api_key: str, model: str, connect: Callable = _connect,
                 timeout: float = TIMEOUT, clock: Callable = time.monotonic):
        self._key = api_key
        self._model = model
        self._connect = connect
        self._timeout = timeout
        self._clock = clock

    # ── the one primitive ───────────────────────────────────────────────────
    def run(self, turns: list[dict], **options) -> str:
        """One question in, her answer out, as text.

        `turns` are Live `clientContent` turns, the last one being his.
        `on_call(name, args)` answers each function call she makes. With
        `first_call_only`, the session ends at her first function call and
        returns "" — the call itself is the answer (see `decide`).

        Never waits longer than the timeout and a margin, whatever the network
        does: the session runs on its own thread and is abandoned, not waited
        on, if it hangs somewhere a socket timeout does not reach. A question
        he asked must always get an answer, even if the answer is "too long".
        """
        box: dict = {}

        def session():
            try:
                box["answer"] = self._session(turns, **options)
            except BaseException as exc:    # noqa: BLE001 — carried to the caller
                box["error"] = exc

        worker = threading.Thread(target=session, name="carnage-live-text", daemon=True)
        worker.start()
        worker.join(self._timeout + 15)
        if worker.is_alive():
            log.warning("live: gave up waiting on a session that hung")
            raise LiveError("the voice model took too long to answer")
        if "error" in box:
            raise box["error"]
        return box.get("answer", "")

    def _session(self, turns: list[dict], *, system: str = "",
                 functions: list[dict] | None = None,
                 on_call: Callable[[str, dict], Any] | None = None,
                 search: bool = False, first_call_only: bool = False,
                 on_text: Callable[[str], None] | None = None) -> str:
        tools: list[dict] = []
        if functions:
            tools.append({"functionDeclarations": functions})
        if search:
            tools.append({"googleSearch": {}})
        setup: dict[str, Any] = {
            "model": self._model,
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "outputAudioTranscription": {},
        }
        if system:
            setup["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            setup["tools"] = tools

        deadline = self._clock() + self._timeout
        started = self._clock()
        log.info("live: connecting (%d turns, %d functions, search=%s)",
                 len(turns), len(functions or []), search)
        try:
            socket = self._connect(f"{LIVE_URL}?key={self._key}")
        except Exception as exc:            # noqa: BLE001
            raise LiveError(f"could not reach the voice model: {exc}") from exc
        log.info("live: connected in %.1fs", self._clock() - started)
        with socket:
            socket.send(json.dumps({"setup": setup}))
            self._until(socket, deadline, lambda m: "setupComplete" in m)
            log.info("live: set up in %.1fs", self._clock() - started)
            socket.send(json.dumps(
                {"clientContent": {"turns": turns, "turnComplete": True}}))

            said: list[str] = []
            calls = 0
            while True:
                message = self._next(socket, deadline)
                if "toolCall" in message:
                    answered = []
                    for call in (message["toolCall"].get("functionCalls") or []):
                        name = str(call.get("name", ""))
                        args = call.get("args") or {}
                        calls += 1
                        if calls > MAX_TOOL_CALLS:
                            result = "Out of steps — answer him now with what you have."
                        else:
                            result = on_call(name, dict(args)) if on_call else "Unavailable."
                        if first_call_only:
                            return ""
                        answered.append({"id": call.get("id"), "name": name,
                                         "response": {"result": str(result or "Done.")}})
                    socket.send(json.dumps(
                        {"toolResponse": {"functionResponses": answered}}))
                    continue
                content = message.get("serverContent") or {}
                text = (content.get("outputTranscription") or {}).get("text")
                if text:
                    said.append(text)
                    # Her words arrive as she would speak them: pass each
                    # growth on, so the app can show them as they come.
                    if on_text is not None:
                        try:
                            on_text(_tidy("".join(said)))
                        except Exception:   # noqa: BLE001 — the screen went away
                            log.debug("live: could not show words so far")
                # A turn can end with only a tool call in it; her words come
                # in the turn after the result, so keep listening for them.
                if content.get("turnComplete") and (said or calls == 0):
                    break
            answer = _tidy("".join(said))
            log.info("live: answered in %.1fs (%d chars, %d tool calls)",
                     self._clock() - started, len(answer), calls)
            return answer

    def _next(self, socket, deadline: float) -> dict:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise LiveError("the voice model took too long to answer")
        try:
            raw = socket.recv(timeout=remaining)
        except TimeoutError as exc:
            raise LiveError("the voice model took too long to answer") from exc
        except Exception as exc:            # noqa: BLE001 — closed, refused, quota
            raise LiveError(_closed_reason(exc)) from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except ValueError:
            return {}
        return message if isinstance(message, dict) else {}

    def _until(self, socket, deadline: float, done: Callable[[dict], bool]) -> None:
        while not done(self._next(socket, deadline)):
            pass

    # ── what the rest of Carnage calls ──────────────────────────────────────
    def describe(self, prompt: str, data_b64: str, system: str = "",
                 max_tokens: int = 300, mime: str = "image/jpeg") -> str:
        """Look at a photo, a screenshot or a PDF and say what is there.
        `max_tokens` is accepted for the old signature; a spoken answer is
        as long as it needs to be."""
        return self.run([{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": data_b64}},
            {"text": prompt}]}], system=system)

    def search(self, query: str) -> str:
        """A real web search, answered from what it found."""
        return self.run(
            [{"role": "user", "parts": [{"text": query}]}],
            system=("Search the web for this and answer from what you find: "
                    "the facts, with numbers, names and dates. No preamble."),
            search=True)

    def decide(self, prompt: str, system: str, function: dict) -> dict:
        """Have her answer by calling `function` once; its arguments back."""
        caught: dict = {}

        def keep(name: str, args: dict):
            if name == function["name"]:
                caught.update(args)
            return "ok"

        self.run([{"role": "user", "parts": [{"text": prompt}]}],
                 system=system, functions=[function], on_call=keep,
                 first_call_only=True)
        return caught


class LiveConversation:
    """A typed conversation with her, through the voice model."""

    def __init__(self, model: LiveModel, registry, instruction: Callable[[], str],
                 history: int = 12):
        self._model = model
        self._registry = registry
        self._instruction = instruction
        self._history_limit = max(2, history)
        self.history: list[dict] = []

    def ask(self, said: str, context: str = "", files: list[dict] | None = None,
            on_text: Callable[[str], None] | None = None) -> str:
        """What she says back. Never raises.

        `context` (his screen, or what he shared) and `files` ({"mime",
        "data"}: a screenshot, a photo, a PDF — looked at in this same
        session, not described first) go into this turn only, not into the
        remembered history: the next turn brings them again if they still
        apply. `on_text` hears her words as they arrive.
        """
        said = (said or "").strip()
        if not said:
            return "You didn't say anything."
        turns = [{"role": "user" if t["who"] == "him" else "model",
                  "parts": [{"text": t["text"]}]} for t in self.history]
        now = [{"inlineData": {"mimeType": f.get("mime") or "image/jpeg",
                               "data": f["data"]}}
               for f in files or [] if f.get("data")]
        if context:
            now.append({"text": context})
        now.append({"text": said})
        turns.append({"role": "user", "parts": now})
        try:
            answer = self._model.run(
                turns, system=self._instruction(),
                functions=self._registry.gemini_declarations(uppercase_types=True),
                on_call=self._call, on_text=on_text)
        except Exception as exc:            # noqa: BLE001
            log.warning("conversation: %s", exc)
            return trouble(exc)
        answer = answer or "…"
        self._remember("him", said)
        self._remember("her", answer)
        return answer

    def _call(self, name: str, args: dict) -> str:
        if name not in self._registry:
            return f"There is no tool called {name}."
        try:
            result = self._registry.dispatch(name, args)
        except Exception as exc:            # noqa: BLE001
            log.info("conversation: %s failed: %s", name, exc)
            return f"{name} did not work: {exc}"
        text = "" if result is None else str(result)
        return text[:2000] + (" …(cut)" if len(text) > 2000 else "")

    def _remember(self, who: str, text: str) -> None:
        self.history.append({"who": who, "text": text})
        del self.history[:-self._history_limit]


def trouble(exc: Exception) -> str:
    """What she says when the model would not answer — the real reason, in
    words he can act on, never a vague "couldn't think"."""
    text = str(exc).lower()
    if "quota" in text or "resource_exhausted" in text or "429" in text:
        return ("Gemini says this key's free quota is used up for now. Give "
                "it a minute, or add billing to the key to lift the limit.")
    if "api key" in text or "permission" in text or "403" in text or "401" in text:
        return "Gemini refused the API key — check it in Settings."
    if "too long" in text:
        return "That took too long to think through. Ask me again?"
    return "I couldn't reach Gemini just now — check the internet and ask again."


def _closed_reason(exc: Exception) -> str:
    """The Live API puts its refusal in the close frame's reason."""
    reason = getattr(getattr(exc, "rcvd", None), "reason", "") or ""
    return f"the voice model closed the session: {reason or exc}"


def _tidy(text: str) -> str:
    return " ".join(text.split())
