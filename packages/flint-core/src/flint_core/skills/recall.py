"""Voice tools over an Archive — the searchable memory tier.

The hot tier (`flint_core.memory`) rides in every prompt and is small on
purpose. This is the other half: unbounded, never in the prompt, searched only
when something actually calls for it.
"""

from __future__ import annotations


def register_recall_tools(reg, archive):
    """The searchable memory tier — everything that doesn't fit in the prompt."""

    @reg.tool(
        description=(
            "Searches everything she's ever filed away — old conversations, "
            "details about people and projects, things from months ago. Use "
            "when he refers to something you don't already have in front of "
            "you: 'that thing we discussed', 'what did I say about X?', "
            "'us project ka kya scene tha?'. Report only what comes back; if "
            "nothing does, say you don't remember rather than guessing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "about": {"type": "string",
                          "description": "What to look for — names and specifics work best"},
            },
            "required": ["about"],
        },
    )
    def remember_about(about: str) -> str:
        found = archive.search(about)
        if not found:
            return f"I've got nothing filed about {about}."
        return " ".join(entry.line() for entry in found)

    @reg.tool(
        description=(
            "Files something away for the long term — bigger than a one-line "
            "preference (that's save_memory). Use for details about projects, "
            "people, or anything he says he'll want later."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remember, in full"},
                "subject": {"type": "string",
                            "description": "Who or what it's about"},
                "kind": {"type": "string",
                         "description": "fact, project, person, or episode"},
            },
            "required": ["text"],
        },
    )
    def file_away(text: str, subject: str = "", kind: str = "fact") -> str:
        if archive.remember(text, kind=kind, subject=subject) is None:
            return "There was nothing there to file."
        return "Filed that away."

    @reg.tool(
        description=("Forgets everything filed about something. Use for "
                     "'forget about X', 'X ke baare mein bhool ja'."),
        parameters={
            "type": "object",
            "properties": {
                "about": {"type": "string", "description": "What to forget"},
            },
            "required": ["about"],
        },
    )
    def forget_about(about: str) -> str:
        dropped = archive.forget_matching(about)
        if not dropped:
            return f"I had nothing filed about {about} anyway."
        return f"Forgotten — dropped {dropped} thing{'s' if dropped > 1 else ''}."
