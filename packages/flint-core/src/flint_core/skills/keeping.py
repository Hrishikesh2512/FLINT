"""Voice tools over the small stores: reminders, notes, lists.

All three are the same shape — say a thing, have it survive a reboot, ask for
it back — and all three are now shared, so a list added to on the walk home is
the list on the laptop. See `flint_core.stores`.
"""

from __future__ import annotations

import time

from flint_core.skills.everyday import parse_reminder_time


def register_reminder_tools(reg, reminders):
    """Wall-clock reminders that outlive a reboot (timers do not)."""
    @reg.tool(
        description=(
            "Sets a persistent reminder that survives reboots and fires at "
            "a wall-clock time (unlike set_timer, which is a short relative "
            "countdown). Use for 'remind me...' at a date/time or later "
            "today/tomorrow. When it's due, a chime plays and you announce "
            "it. Pass EITHER minutes_from_now for short delays, OR at_time "
            "as 'YYYY-MM-DD HH:MM' (24-hour, local) computed from the "
            "current date/time you were given."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "What to remind about, e.g. 'call mom'"},
                "minutes_from_now": {"type": "number",
                                     "description": "Delay in minutes (for soon)"},
                "at_time": {"type": "string",
                            "description": "Absolute 'YYYY-MM-DD HH:MM' local"},
            },
            "required": ["text"],
        },
    )
    def set_reminder(text: str, minutes_from_now: float | None = None,
                     at_time: str | None = None) -> str:
        try:
            due, phrase = parse_reminder_time(minutes_from_now, at_time)
        except ValueError as exc:
            return f"I couldn't set that reminder: {exc}."
        reminders.add(text, due)
        return f"Reminder set: '{text.strip()}' {phrase}."

    @reg.tool(description="Lists all upcoming persistent reminders.")
    def list_reminders() -> str:
        pending = reminders.pending()
        if not pending:
            return "No reminders are set."
        return "; ".join(
            f"'{r['text']}' at "
            f"{time.strftime('%a %I:%M %p', time.localtime(r['due']))}"
            for r in pending)

    @reg.tool(
        description="Cancels reminders matching some text.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to match"},
            },
            "required": ["text"],
        },
    )
    def cancel_reminder(text: str) -> str:
        n = reminders.cancel(text)
        return f"Cancelled {n} reminder(s)." if n else "No matching reminder."


def register_note_tools(reg, notes):
    """Quick voice notes."""
    @reg.tool(
        description=("Saves a quick voice note for the user to review later. "
                     "Use for 'note that...', 'take a note', 'jot down...'."),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note content"},
            },
            "required": ["text"],
        },
    )
    def add_note(text: str) -> str:
        notes.add(text)
        return "Noted."

    @reg.tool(description="Reads back all saved voice notes.")
    def read_notes() -> str:
        items = notes.all()
        if not items:
            return "You have no notes."
        return " • ".join(n["text"] for n in items if n.get("text"))

    @reg.tool(description="Deletes all saved voice notes.")
    def clear_notes() -> str:
        return f"Cleared {notes.clear()} note(s)."


def register_list_tools(reg, lists):
    """Named lists — shopping, todo, whatever he calls one."""
    @reg.tool(
        description=("Adds an item to a named list (default 'shopping'). "
                     "Use for 'add milk to my shopping list', 'add X to "
                     "todo'."),
        parameters={
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item to add"},
                "list_name": {"type": "string",
                              "description": "List name, e.g. shopping, todo"},
            },
            "required": ["item"],
        },
    )
    def add_to_list(item: str, list_name: str = "shopping") -> str:
        return lists.add_item(item, list_name)

    @reg.tool(
        description="Removes an item from a named list (default 'shopping').",
        parameters={
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item to remove"},
                "list_name": {"type": "string", "description": "List name"},
            },
            "required": ["item"],
        },
    )
    def remove_from_list(item: str, list_name: str = "shopping") -> str:
        return lists.remove_item(item, list_name)

    @reg.tool(
        description="Reads back a named list (default 'shopping').",
        parameters={
            "type": "object",
            "properties": {
                "list_name": {"type": "string", "description": "List name"},
            },
        },
    )
    def show_list(list_name: str = "shopping") -> str:
        items = lists.show(list_name)
        if not items:
            return f"The {list_name} list is empty."
        return f"{list_name}: " + ", ".join(items)

    @reg.tool(
        description="Empties a named list (default 'shopping').",
        parameters={
            "type": "object",
            "properties": {
                "list_name": {"type": "string", "description": "List name"},
            },
        },
    )
    def clear_list(list_name: str = "shopping") -> str:
        return f"Cleared {lists.clear(list_name)} item(s) from {list_name}."
