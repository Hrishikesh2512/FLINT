"""Shield: his own store, which every one of his AIs shares.

Shield is a separate app on the phone, a file explorer he browses by hand ,
that holds his folders, notes, files and links, and exposes them to his AIs.
Jarvis reaches it on the same phone (`http://127.0.0.1:8791`), with a token
the Shield app hands only to apps signed by him.

What she adds is marked "jarvis" in Shield, so he always sees what she put
there; she never deletes anything for good (the API cannot).
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable

log = logging.getLogger("flint.shield")

SHIELD_PROMPT = (
    "SHIELD is his own store of files, notes and links, a separate app that "
    "all his AIs share and he browses himself. When he says save, keep, file "
    "or note something down for later, put it in Shield (shield_save), in a "
    "folder that fits; when he asks about his documents, scans, notes or "
    "anything he kept, look there (shield_search, then shield_read). Say "
    "where you saved it. Never claim something is in Shield without checking.")

#: Where she files things when he does not say.
DEFAULT_FOLDER = "/Jarvis"

#: How much of an item's text comes back to her in one read.
MAX_READ = 6000


def register_shield_tools(reg, connect: Callable, describe: Callable | None = None) -> None:
    """`connect()` gives a `shield_client.Shield` (or None if Shield is not
    there); `describe(prompt, b64, system, mime=)` looks at photos and PDFs."""

    def _shield():
        client = connect()
        if client is None:
            raise LookupError("Shield isn't installed or isn't running on this phone.")
        return client

    def _say(error: Exception) -> str:
        if isinstance(error, LookupError):
            return str(error)
        log.warning("shield: %s", error)
        return f"Shield didn't answer: {error}"

    @reg.tool(
        description=("Searches his Shield store, names, the words inside notes and "
                     "text files, and tags. 'Find my passport scan', 'what did I save "
                     "about the Goa trip?'. Every word must match; use the key words."),
        parameters={"type": "object",
                    "properties": {"query": {"type": "string"},
                                   "tag": {"type": "string",
                                           "description": "Only things with this tag"}},
                    "required": ["query"]},
    )
    def shield_search(query: str, tag: str = "") -> str:
        try:
            found = _shield().search(query, tag=tag or None, limit=10)
        except Exception as e:              # noqa: BLE001
            return _say(e)
        if not found:
            return f"Nothing in Shield matches \"{query}\"."
        def line(i) -> str:
            by = f", by {i.created_by}" if i.created_by not in ("you", "shield") else ""
            gist = f": {i.preview[:120]}" if i.preview else ""
            return f"- {i.name} ({i.kind}{by}) id={i.id}{gist}"

        return "\n".join(line(i) for i in found)

    @reg.tool(
        description=("Reads one thing from Shield by its id (from shield_search or "
                     "shield_list) or its path ('/College/Notes/DBMS'). Notes, links and "
                     "text files come back as text; photos and PDFs are looked at."),
        parameters={"type": "object",
                    "properties": {"item": {"type": "string"},
                                   "question": {"type": "string",
                                                "description": "What he wants to know from it"}},
                    "required": ["item"]},
    )
    def shield_read(item: str, question: str = "") -> str:
        try:
            shield = _shield()
            found = shield.get(item.strip())
            if found.kind == "folder":
                return _listing(shield, found.id, found.name)
            if found.text:
                text = found.text
                return text[:MAX_READ] + (" …(cut)" if len(text) > MAX_READ else "")
            if describe is not None and (found.mime.startswith("image/")
                                         or found.mime == "application/pdf"):
                data = base64.b64encode(shield.download(found.id)).decode()
                ask = question.strip() or "What is this? Give the important details exactly."
                return describe(f"This is \"{found.name}\" from his files. {ask}", data,
                                "Read it exactly; never guess at what you cannot see.",
                                mime=found.mime)
            return f"\"{found.name}\" is a {found.mime or 'file'} I can't read as text."
        except Exception as e:              # noqa: BLE001
            return _say(e)

    @reg.tool(
        description=("Lists what is in a Shield folder by its path ('/' for the top, "
                     "'/College'). 'What's in my College folder?'"),
        parameters={"type": "object",
                    "properties": {"folder": {"type": "string"}}},
    )
    def shield_list(folder: str = "/") -> str:
        try:
            shield = _shield()
            folder = folder.strip() or "/"
            if folder == "/":
                return _listing(shield, "/", "the top of Shield")
            found = shield.get(folder)
            return _listing(shield, found.id, found.name)
        except Exception as e:              # noqa: BLE001
            return _say(e)

    @reg.tool(
        description=("Saves a note into Shield, in a folder by path (made if missing; "
                     f"'{DEFAULT_FOLDER}' if he doesn't say). For 'save this', 'note this "
                     "down', 'keep this for later'. Tags are optional."),
        parameters={"type": "object",
                    "properties": {"name": {"type": "string"},
                                   "text": {"type": "string"},
                                   "folder": {"type": "string"},
                                   "tags": {"type": "array", "items": {"type": "string"}}},
                    "required": ["name", "text"]},
    )
    def shield_save(name: str, text: str, folder: str = "", tags: list | None = None) -> str:
        where = (folder or DEFAULT_FOLDER).strip()
        if not where.startswith("/"):
            where = "/" + where
        try:
            saved = _shield().save_note(where, name.strip() or "Note", text, tags=list(tags or []))
        except Exception as e:              # noqa: BLE001
            return _say(e)
        return f"Saved \"{saved.name}\" in Shield › {where.strip('/').replace('/', ' › ')}."

    @reg.tool(
        description=("Moves, renames or tags something in Shield, by id or path. "
                     "'Move that to College', 'tag it urgent'."),
        parameters={"type": "object",
                    "properties": {"item": {"type": "string"},
                                   "to_folder": {"type": "string"},
                                   "new_name": {"type": "string"},
                                   "add_tags": {"type": "array", "items": {"type": "string"}}},
                    "required": ["item"]},
    )
    def shield_organise(item: str, to_folder: str = "", new_name: str = "",
                        add_tags: list | None = None) -> str:
        changes: dict = {}
        if to_folder.strip():
            where = to_folder.strip()
            changes["path"] = where if where.startswith("/") else "/" + where
        if new_name.strip():
            changes["name"] = new_name.strip()
        if add_tags:
            changes["add_tags"] = list(add_tags)
        if not changes:
            return "Tell me where to move it, what to call it, or how to tag it."
        try:
            done = _shield().update(item.strip(), **changes)
        except Exception as e:              # noqa: BLE001
            return _say(e)
        where = f" is in {changes['path']}" if "path" in changes else ""
        return f"Done, \"{done.name}\"{where}."


def _listing(shield, folder: str, label: str) -> str:
    inside = shield.list(folder)
    if not inside:
        return f"{label} is empty."
    lines = [f"- {i.name}{'/' if i.kind == 'folder' else ''} id={i.id}" for i in inside[:40]]
    more = f"\n…and {len(inside) - 40} more" if len(inside) > 40 else ""
    return f"In {label}:\n" + "\n".join(lines) + more


def shield_for(url: str, token: str, agent: str) -> Callable:
    """A `connect()` for `register_shield_tools`, from an address and token
    (Shield → Settings shows both). None until both are set."""

    def connect():
        if not (url and token):
            return None
        from shield_client import Shield

        return Shield(url, token=token, agent=agent)

    return connect
