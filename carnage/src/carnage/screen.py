"""What is on his screen, as something she can read.

When he long-presses for her over another app, Android hands the assistant two
things: the screen's view tree (every piece of text the app is showing) and a
screenshot. The Kotlin side flattens the tree to text; this turns both into one
block of context that rides along with what he says, so "reply to this",
"what's this?" and "add that to my calendar" mean the thing in front of him.

The text is the reliable half — exact names, numbers and messages, cheap to
send. The screenshot is for what text cannot carry: a photo, a product, a
chart, or an app that draws its own pixels and exposes no text at all. It is
described once, when the screen arrives, not on every turn: the follow-ups in
the same sitting are about the same screen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger("carnage.screen")

#: How much screen text goes into the prompt. A long chat or article can run
#: to tens of thousands of characters; what he is asking about is almost always
#: on the part he can see, which is the part the tree lists first.
MAX_TEXT = 6000

DESCRIBE_SCREEN = (
    "This is a screenshot of his phone. Describe what is on it so someone who "
    "cannot see it could help him with it: which app and what it is showing, "
    "any people, products, places, images or charts, and the exact wording of "
    "anything that looks important. Plain prose, no preamble.")

DESCRIBE_SYSTEM = ("You describe phone screenshots precisely and briefly. "
                   "Never guess at text you cannot read.")

HOW_TO_USE = (
    "\"This\", \"it\", \"here\", \"this message\" and \"that\" mean what is on "
    "this screen. Answer from it directly. If he asks you to act on it — reply, "
    "save, remind, look something up — use the details on the screen; never "
    "ask him to read out what is already in front of you.")


def screen_context(app: str, text: str, jpeg_b64: str = "",
                   describe: Callable | None = None) -> str:
    """One block of context about his screen; "" when there is nothing to say.

    `describe(prompt, jpeg_b64, system, max_tokens=...)` is the vision call.
    Without it, or when it fails, the screen text alone is used — a missing
    description makes her less sharp, never wrong.
    """
    app = (app or "").strip()
    text = _tidy(text or "")
    seen = ""
    if jpeg_b64 and describe is not None:
        try:
            seen = str(describe(DESCRIBE_SCREEN, jpeg_b64, DESCRIBE_SYSTEM,
                                max_tokens=700) or "").strip()
        except Exception as exc:            # noqa: BLE001 — network / API
            log.warning("could not describe the screen: %s", exc)
    if not (text or seen):
        return ""

    lines = ["[WHAT IS ON HIS SCREEN RIGHT NOW — he called you up while "
             "looking at it]"]
    if app:
        lines.append(f"App: {app}")
    if text:
        lines += ["Text on the screen:", text]
    if seen:
        lines += ["What the screen shows:", seen]
    lines.append(HOW_TO_USE)
    return "\n".join(lines)


def _tidy(text: str) -> str:
    """Blank lines and repeats out, then cut to size."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    joined = "\n".join(out)
    if len(joined) > MAX_TEXT:
        joined = joined[:MAX_TEXT] + " …(cut)"
    return joined
