"""Voice tools that write files out — notes, spreadsheets, slide decks.

The formats and the safe-name handling live in `flint_core.documents`; this is
only the spoken surface over them. Every path is resolved inside one configured
folder, so a dictated filename can never escape it.
"""

from __future__ import annotations


def register_document_tools(reg, folder: str):
    """Writing things out: notes, spreadsheets, slide decks."""
    from flint_core.documents import (
        list_documents,
        read_document,
        write_document,
        write_presentation,
        write_spreadsheet,
    )

    @reg.tool(
        description=(
            "Writes a document — notes, a summary, a letter, a write-up. "
            "Markdown by default; pass a name ending .txt or .docx for those. "
            "Use for 'write this up', 'make me a note about...', 'draft a...'. "
            "Set overwrite only if he says to replace an existing file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename, e.g. 'meeting notes'"},
                "content": {"type": "string", "description": "The full body text"},
                "title": {"type": "string", "description": "Heading, for markdown"},
                "overwrite": {"type": "boolean",
                              "description": "true only if replacing on purpose"},
            },
            "required": ["name", "content"],
        },
    )
    def write_note(name: str, content: str, title: str = "",
                   overwrite: bool = False) -> str:
        return write_document(folder, name, content, title=title,
                              overwrite=overwrite).spoken()

    @reg.tool(
        description=(
            "Writes a spreadsheet from rows of values. CSV by default (opens "
            "in Excel); .xlsx if he asks for Excel specifically. Use for "
            "'make a spreadsheet of...', 'track my spending'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename"},
                "headers": {"type": "array", "items": {"type": "string"},
                            "description": "Column names"},
                "rows": {"type": "array",
                         "items": {"type": "array", "items": {"type": "string"}},
                         "description": "Each row as a list of cell values"},
                "overwrite": {"type": "boolean", "description": "Replace on purpose"},
            },
            "required": ["name", "rows"],
        },
    )
    def write_sheet(name: str, rows: list, headers: list | None = None,
                    overwrite: bool = False) -> str:
        return write_spreadsheet(folder, name, rows, headers=headers or (),
                                 overwrite=overwrite).spoken()

    @reg.tool(
        description=(
            "Writes a slide deck. Markdown by default (imports into any slide "
            "tool); .pptx if he asks for PowerPoint. Each slide needs a title "
            "and bullets. Use for 'make me a deck about...'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename"},
                "title": {"type": "string", "description": "Deck title"},
                "slides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                            "notes": {"type": "string"},
                        },
                    },
                    "description": "The slides, in order",
                },
                "overwrite": {"type": "boolean", "description": "Replace on purpose"},
            },
            "required": ["name", "slides"],
        },
    )
    def write_deck(name: str, slides: list, title: str = "",
                   overwrite: bool = False) -> str:
        return write_presentation(folder, name, slides, title=title,
                                  overwrite=overwrite).spoken()

    @reg.tool(
        description=("Lists the documents she's written, newest first. Use for "
                     "'what have you written?', 'show me my notes'."),
    )
    def list_notes() -> str:
        found = list_documents(folder)
        if not found:
            return "I haven't written anything out yet."
        return "Most recent first: " + ", ".join(found[:10]) + "."

    @reg.tool(
        description=("Reads back a document she wrote, so it can be read out "
                     "or edited. Use for 'read me the meeting notes'."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The filename"},
            },
            "required": ["name"],
        },
    )
    def read_note(name: str) -> str:
        from pathlib import Path

        from flint_core.documents import safe_name

        return read_document(Path(folder) / safe_name(name, "md"))
