"""Voice tools over a calendar feed and a mailbox.

Neither knows what body it is running in: an iCal URL and an IMAP mailbox
answer the same on a Pi, a phone or a laptop. What differs per device is
whether they were configured at all, which is a capability question rather
than a code one.
"""

from __future__ import annotations


def register_calendar_tools(reg, calendar):
    """Agenda and next event, from a subscribed calendar feed."""
    @reg.tool(
        description=(
            "Reads the user's Google Calendar agenda. Use for 'what's on "
            "today?', 'kal kya hai?', 'am I free tomorrow?', 'what's my "
            "schedule?'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "day": {"type": "string",
                        "description": "'today' (default) or 'tomorrow'"},
            },
        },
    )
    def calendar_agenda(day: str = "today") -> str:
        return calendar.feed.agenda(day)

    @reg.tool(
        description=(
            "Tells the user their next upcoming calendar event and how "
            "long until it. Use for 'what's next?', 'when's my next "
            "class/meeting?'."
        ),
    )
    def next_event() -> str:
        return calendar.feed.next_event()


def register_mail_tools(reg, mailbox):
    """Reading the inbox. Strictly read-only — nothing is marked read."""
    @reg.tool(
        description=(
            "Checks the user's Gmail inbox and summarises unread mail "
            "(count, senders, subjects). Use for 'any new mail?', 'koi "
            "email aaya?', 'check my inbox'. Read-only — nothing is "
            "marked as read."
        ),
    )
    def check_inbox() -> str:
        return mailbox.unread_summary()

    @reg.tool(
        description=(
            "Reads an email aloud: the latest unread one, or the latest "
            "from a specific sender if given. Use for 'read the email', "
            "'what did the placement cell send?'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_sender": {"type": "string",
                                "description": "Optional sender name or "
                                               "address to filter by"},
            },
        },
    )
    def read_latest_email(from_sender: str = "") -> str:
        return mailbox.read_latest(from_sender)
