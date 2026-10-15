"""Getting somewhere, and answering a message.

Two small things a phone assistant is expected to do:

- **navigate** hands a destination to Google Maps, already navigating. Maps
  works out the route and the time; she does not pretend to.
- **reply_to_message** answers a message straight from its notification, the
  way the "Reply" button in the shade does, no app opened, nothing tapped.
  Only apps that put a reply box on their notifications (WhatsApp, Messages,
  Telegram, Instagram…) can be answered this way, and she says so otherwise.
Both reach Android through `phone` methods that only `AndroidPhone`
has; looked up with getattr so the other phones need not grow stubs.
"""

from __future__ import annotations

import logging

log = logging.getLogger("carnage.extras")

NAVIGATION_PROMPT = (
    "GETTING SOMEWHERE: navigate starts Google Maps navigation to a place, "
    "'take me home', 'office chalo', 'directions to the airport'. If he says "
    "home or work and you know the address from memory, pass the address. "
    "Maps gives the route and the time; never invent one.")

REPLY_PROMPT = (
    "REPLYING: reply_to_message answers a message from its notification "
    "(WhatsApp, Messages, Telegram and others). Send only words he actually "
    "gave you, or that he agreed to after you read them back, never "
    "improvise a message in his name.")

_MODES = {"drive": "d", "driving": "d", "car": "d", "walk": "w", "walking": "w",
          "bike": "b", "cycle": "b", "two-wheeler": "l", "scooter": "l",
          "motorcycle": "l"}


def register_navigation_tools(reg, phone) -> None:

    @reg.tool(
        description=("Starts turn-by-turn navigation in Google Maps. 'Take me "
                     "home', 'directions to Phoenix Mall', 'office chalo'."),
        parameters={"type": "object",
                    "properties": {
                        "destination": {"type": "string",
                                        "description": "A place name or an address"},
                        "mode": {"type": "string",
                                 "description": "drive, walk, bike or two-wheeler"}},
                    "required": ["destination"]},
    )
    def navigate(destination: str, mode: str = "drive") -> str:
        destination = (destination or "").strip()
        if not destination:
            return "Where to?"
        start = getattr(phone, "navigate", None)
        code = _MODES.get((mode or "drive").strip().lower(), "d")
        if start is None or not start(destination, code):
            return ("I couldn't open Maps, it may not be installed, or I "
                    "need to be on screen to open it.")
        return f"Navigating to {destination} in Maps."


def register_reply_tools(reg, phone) -> None:

    @reg.tool(
        description=("Replies to a message from its notification, without "
                     "opening the app. 'Reply to Rahul: on my way', 'Ma ko "
                     "bolo 10 minute'. `who` is the sender or chat name."),
        parameters={"type": "object",
                    "properties": {"who": {"type": "string"},
                                   "text": {"type": "string"}},
                    "required": ["who", "text"]},
    )
    def reply_to_message(who: str, text: str) -> str:
        who, text = (who or "").strip(), (text or "").strip()
        if not who or not text:
            return "I need who to reply to and what to say."
        reply = getattr(phone, "reply_to", None)
        result = reply(who, text) if reply is not None else None
        if not isinstance(result, dict):
            return ("I can't reach notifications, turn on notification "
                    "access for Carnage first.")
        status = result.get("status")
        if status == "sent":
            return f"Replied to {result.get('to') or who} on {result.get('app') or 'their app'}."
        if status == "no_reply_box":
            return (f"{result.get('app') or 'That app'}'s notification has no "
                    f"reply box, so I can't answer it from here.")
        return f"There's no message from {who} waiting in your notifications."
