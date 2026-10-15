"""Things he hands her: a photo, a PDF, a bit of text shared from another app.

Turned into one block of context that rides along with what he asks, the same
way his screen does (`carnage.screen`). A photo or a PDF is read once, by the
vision model, with his question in mind; the follow-ups in the same sitting
reuse that reading rather than paying for it again.

Shared text goes in as it is, it is already words, and a model paraphrasing
a message before she answers about it only loses the exact wording.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger("carnage.attachments")

#: Shared text beyond this is cut. A whole article pasted in is fine; a book
#: is not what a phone conversation is for.
MAX_TEXT = 8000

#: Gemini takes inline files up to about 20 MB per request, base64 included.
MAX_BYTES = 14 * 1024 * 1024

READ_SYSTEM = ("You read what someone shared from their phone so an assistant "
               "can help with it. Be exact with names, numbers, dates and "
               "amounts. Never guess at what you cannot read.")

HOW_TO_USE = (
    "\"This\", \"it\" and \"these\" mean what he shared. Answer from it "
    "directly; never ask him to type out what is already in front of you.")

_KINDS = {"application/pdf": "PDF"}


def attachment_context(items: list[dict], question: str = "",
                       describe: Callable | None = None) -> str:
    """One block of context about what he shared; "" when nothing usable.

    Each item is {"name", "mime", and either "text" or "data" (base64)}, or
    "too_big" for a file the app would not load into memory.
    `describe(prompt, b64, system, max_tokens=..., mime=...)` is the vision
    call; a file it cannot read is named rather than silently dropped, so she
    can say she couldn't open it instead of answering as if it were not there.
    """
    blocks: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip() or "attachment"
        mime = str(item.get("mime") or "").strip().lower()
        text = str(item.get("text") or "").strip()
        data = str(item.get("data") or "").strip()
        if item.get("too_big"):
            kind = _KINDS.get(mime, "photo" if mime.startswith("image/") else "file")
            blocks.append(f"A {kind} ({name}), too big to read; tell him so.")
        elif text:
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT] + " …(cut)"
            blocks.append(f"Shared text ({name}):\n{text}")
        elif data:
            blocks.append(_read_file(name, mime, data, question, describe))
    if not blocks:
        return ""
    return "\n\n".join(["[WHAT HE SHARED WITH YOU]", *blocks, HOW_TO_USE])


def _read_file(name: str, mime: str, data: str, question: str,
               describe: Callable | None) -> str:
    kind = _KINDS.get(mime, "photo" if mime.startswith("image/") else "file")
    if describe is None:
        # Not read separately: the file itself rides in the same message.
        return f"A {kind} ({name}), it is attached to this message; look at it."
    if len(data) * 3 // 4 > MAX_BYTES:
        return f"A {kind} ({name}), too big to read; tell him so."
    asked = f" He asks: \"{question.strip()}\"." if question.strip() else ""
    if kind == "PDF":
        prompt = (f"He shared this PDF.{asked} Say what it is, then give "
                  "everything needed to answer him: the key facts, figures, "
                  "dates and wording, in order. Plain prose.")
        budget = 1500
    else:
        prompt = (f"He shared this {kind}.{asked} Describe it so someone who "
                  "cannot see it could answer him: what it shows, any people, "
                  "products, places, and the exact wording of any text.")
        budget = 800
    try:
        seen = str(describe(prompt, data, READ_SYSTEM, max_tokens=budget,
                            mime=mime or "image/jpeg") or "").strip()
    except Exception as exc:                # noqa: BLE001, network / API
        log.warning("could not read %s: %s", name, exc)
        return f"A {kind} ({name}), reading it failed just now; say so."
    if not seen:
        return f"A {kind} ({name}) that came back unreadable."
    return f"A {kind} ({name}):\n{seen}"
