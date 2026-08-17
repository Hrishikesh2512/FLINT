"""Handing a piece of work from one of her bodies to another.

The roster lets her *know* the phone could send that text. This lets her
actually send it. Without both halves the roster is worse than nothing — an
assistant that says "my phone could do that" and then does not is more
annoying than one that simply says no.

The mechanism is deliberately small, and it reuses the hub rather than adding
a second network. A leaf posts a request; the hub holds it until the target
device next syncs; the target runs it and posts the answer back the same way.
That means:

  * **No new transport, no new auth, no new open port.** The one connection
    that already exists is the one this rides on.
  * **Delivery is eventual, not immediate.** A request to a device that is
    asleep is not an error; it runs when that device wakes.

The second point is the design constraint everything else follows from, and it
is the honest shape of the problem: these devices genuinely are not all awake
at once, and pretending otherwise would mean inventing timeouts that lie. So a
request has three ends — done, failed, or still waiting — and *waiting is a
normal outcome she has to be able to say out loud*, which is why `Request`
carries the target's presence rather than just a status code.

Requests are plain text, not tool calls. The far device is not a function
being invoked; it is her, with her own tools and her own judgement about how
to carry the thing out. Serialising a tool name and arguments would mean every
device needed the same tool belt — exactly the coupling `capabilities.py`
exists to avoid.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock

log = logging.getLogger("flint.relay")

PENDING = "pending"
DONE = "done"
FAILED = "failed"

#: A request nobody has picked up by now is not going to be useful when it
#: finally lands. "Text Ma I'm running late" delivered four hours later is
#: worse than never delivered, so requests expire rather than queue forever.
EXPIRY_SECONDS = 2 * 3600

#: Keep finished requests around long enough to be asked about ("did that go
#: through?"), then drop them.
KEEP_FINISHED = 24 * 3600


@dataclass
class Request:
    """One thing one body asked another to do."""

    id: str
    sender: str
    target: str
    text: str
    created: float
    status: str = PENDING
    answer: str = ""
    finished: float = 0.0

    @property
    def expired(self) -> bool:
        return self.status == PENDING and (
            time.time() - self.created > EXPIRY_SECONDS)

    def spoken(self) -> str:
        if self.status == DONE:
            return self.answer or "Done."
        if self.status == FAILED:
            return self.answer or "That didn't work."
        return ""


class RelayStore:
    """Requests in flight, on whichever device is holding them.

    On the hub this is the queue every device reads from. On a leaf it is just
    the record of what this device asked for, so it can report back when the
    answer arrives.
    """

    def __init__(self, path: Path | None = None,
                 clock: Callable[[], float] = time.time):
        self._path = Path(path) if path else None
        self._clock = clock
        self._lock = Lock()
        self._memory: dict[str, Request] = {}
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("relay: could not read %s — starting empty", self._path)
            return
        for entry in raw.get("requests", []):
            try:
                self._memory[str(entry["id"])] = Request(**entry)
            except (KeyError, TypeError):
                continue

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(
                {"requests": [asdict(r) for r in self._memory.values()]}),
                encoding="utf-8")
        except OSError:
            log.warning("relay: could not write %s", self._path)

    def _prune(self) -> None:
        now = self._clock()
        for key, request in list(self._memory.items()):
            if request.expired:
                request.status = FAILED
                request.answer = ("Nobody picked that up in time — the device "
                                  "was away.")
                request.finished = now
            elif (request.status != PENDING
                  and now - request.finished > KEEP_FINISHED):
                del self._memory[key]

    # ── writing ─────────────────────────────────────────────────────────────
    def submit(self, sender: str, target: str, text: str) -> Request:
        if not target.strip() or not text.strip():
            raise ValueError("a relayed request needs a target and something to do")
        request = Request(id=uuid.uuid4().hex[:12], sender=sender.strip(),
                          target=target.strip(), text=text.strip(),
                          created=self._clock())
        with self._lock:
            self._prune()
            self._memory[request.id] = request
            self._save()
        return request

    def complete(self, request_id: str, answer: str, ok: bool = True) -> bool:
        with self._lock:
            request = self._memory.get(request_id)
            if request is None or request.status != PENDING:
                return False
            request.status = DONE if ok else FAILED
            request.answer = (answer or "").strip()[:2000]
            request.finished = self._clock()
            self._save()
        return True

    def merge(self, entries: list[dict]) -> int:
        """Take requests and answers arriving from a peer.

        A completion always wins over a pending copy — the only device that
        can complete a request is the one that ran it, so its version is
        authoritative by construction.
        """
        applied = 0
        with self._lock:
            for entry in entries:
                try:
                    incoming = Request(**entry)
                except (TypeError, KeyError):
                    continue
                held = self._memory.get(incoming.id)
                if held is None or (held.status == PENDING
                                    and incoming.status != PENDING):
                    self._memory[incoming.id] = incoming
                    applied += 1
            if applied:
                self._prune()
                self._save()
        return applied

    # ── reading ─────────────────────────────────────────────────────────────
    def waiting_for(self, device: str) -> list[Request]:
        """Requests `device` should carry out, oldest first."""
        with self._lock:
            self._prune()
            return sorted((r for r in self._memory.values()
                           if r.target == device and r.status == PENDING),
                          key=lambda r: r.created)

    def answers_for(self, device: str) -> list[Request]:
        """Finished requests `device` asked for and has not been told about."""
        with self._lock:
            self._prune()
            return sorted((r for r in self._memory.values()
                           if r.sender == device and r.status != PENDING),
                          key=lambda r: r.finished)

    def all_dicts(self) -> list[dict]:
        with self._lock:
            return [asdict(r) for r in self._memory.values()]

    def get(self, request_id: str) -> Request | None:
        return self._memory.get(request_id)


def register_relay_tools(reg, relay: RelayStore, roster, me: str,
                         clock: Callable[[], float] = time.time) -> None:
    """Asking another of her bodies to do something, and hearing back.

    Note what is *not* here: a tool to list her devices. She already has that
    in the prompt, every session, from `roster.render_for_prompt` — and a fact
    that is always true does not deserve a round trip.
    """

    @reg.tool(
        description=(
            "Hands a task to one of her OTHER devices — the phone, the "
            "desktop — when this one cannot do it. Use whenever the thing he "
            "wants needs a body you are not currently in: sending a text or "
            "reaching someone by phone (the phone), anything with his screen, "
            "files or repos (the desktop). Say what you want done in plain "
            "words, exactly as you would say it to him. Tell him you are "
            "doing it there; the answer comes back in a moment, or later if "
            "that device is asleep."
        ),
        parameters={
            "type": "object",
            "properties": {
                "device": {"type": "string",
                           "description": "Which one — 'phone', 'desktop', or its name"},
                "task": {"type": "string",
                         "description": "What to do, in full, in plain words"},
            },
            "required": ["device", "task"],
        },
    )
    def ask_other_device(device: str, task: str) -> str:
        target = roster.find(device)
        if target is None:
            known = ", ".join(d.name for d in roster.others())
            return (f"I don't have a device called {device}. "
                    f"I've got: {known or 'none set up'}.")
        try:
            relay.submit(me, target.name, task)
        except ValueError as exc:
            return str(exc)
        now = clock()
        if target.fresh(now):
            return (f"Passed it to {target.body or target.name} — it'll pick "
                    f"it up in a moment.")
        # Not a failure. Saying *when* it was last seen is what stops her
        # promising something that may not happen for hours.
        return (f"Queued it for {target.body or target.name}, but that one's "
                f"not awake right now — {target.presence(now).lower()} "
                f"It'll run as soon as it's back.")

    @reg.tool(
        description=("Checks whether something she handed to another device "
                     "has come back yet. Use for 'did that go through?', "
                     "'kya hua us kaam ka?'."),
    )
    def check_other_device() -> str:
        answers = relay.answers_for(me)
        if not answers:
            outstanding = [r for r in relay.all_dicts()
                           if r.get("sender") == me and r.get("status") == PENDING]
            if outstanding:
                return (f"Still waiting on {len(outstanding)} thing(s) from "
                        f"my other devices.")
            return "Nothing outstanding."
        return " ".join(
            f"{r.target}: {r.spoken()}" for r in answers[-3:])


def carry_out(relay: RelayStore, me: str,
              run: Callable[[str], str]) -> list[Request]:
    """Run everything waiting for this device. Returns what was handled.

    `run` takes the request text and returns what to say back. It is a
    callable rather than a tool dispatch because the far side is *her* — the
    natural implementation hands the text to a conversation turn and captures
    the reply, so the receiving device applies its own judgement and its own
    tools rather than being remote-controlled.
    """
    handled = []
    for request in relay.waiting_for(me):
        try:
            answer = run(request.text)
            relay.complete(request.id, answer or "Done.", ok=True)
        except Exception as exc:            # noqa: BLE001 — never kill the loop
            log.exception("relay: could not carry out %s", request.id)
            relay.complete(request.id, f"I couldn't do that: {exc}", ok=False)
        handled.append(relay.get(request.id) or request)
    return handled
