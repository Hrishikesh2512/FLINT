"""Doing things on the phone: calls, the phone book, alarms, the torch, apps.

Every one of these is a thing a person does with their thumb dozens of times
a day; saying it to Jarvis instead is the whole point of having a phone body.
Each answers honestly when Android says no — an alarm the clock app never
received is reported, and Jarvis sets a reminder of her own instead.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from carnage.platform import Phone

log = logging.getLogger("carnage.actions")

ACTIONS_PROMPT = (
    "PHONE ACTIONS: you can call people (call_person), look them up in his "
    "phone book (find_contact), set alarms (set_alarm), switch the torch "
    "(flashlight), open apps (open_app) and switch Do Not Disturb. Names "
    "come from his own phone contacts. If a name matches more than one "
    "person, ask which one — never guess who to call."
)


def find_number(who: str, phone: Phone, connections=None):
    """Who he means, as (number, name, choices).

    `number` is set when the answer is clear; `choices` (names) when several
    different people match and he has to say which. A typed-out number is
    taken as it is. Jarvis's own connections come first (they carry his
    nicknames), then the phone book.
    """
    who = (who or "").strip()
    if not who:
        return "", "", []
    if re.fullmatch(r"\+?[\d\s\-()]{5,}", who):
        return who, who, []                     # the dialer copes with spaces
    if connections is not None:
        try:
            number = str(connections.phone_for(who) or "")
        except Exception:                       # noqa: BLE001
            number = ""
        if number:
            return number, who, []
    lookup = getattr(phone, "find_contacts", None)
    matches = (lookup(who) if lookup else None) or []
    if not matches:
        return "", "", []
    exact = [m for m in matches if m["name"].lower() == who.lower()]
    pool = exact or matches
    names = list(dict.fromkeys(m["name"] for m in pool))
    if len(names) > 1:
        return "", "", names[:5]
    return pool[0]["number"], pool[0]["name"], []


def _parse_time(when: str, now: datetime) -> datetime | None:
    """"6", "6:30", "6:30 pm", "18:30", "in 20 minutes" -> the next such moment."""
    text = (when or "").strip().lower().replace(".", ":")
    m = re.fullmatch(r"in (\d+) ?(min|mins|minute|minutes|hour|hours|hr|hrs)", text)
    if m:
        amount = int(m.group(1))
        return now + (timedelta(hours=amount) if m.group(2).startswith("h")
                      else timedelta(minutes=amount))
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))? ?(am|pm)?", text)
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if hour > 23 or minute > 59:
        return None
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= now:
        # "6" said at 7 pm means 6 tomorrow morning; "6" at 5 am means 6 today.
        if not half and hour < 12 and moment + timedelta(hours=12) > now:
            moment += timedelta(hours=12)
        else:
            moment += timedelta(days=1)
    return moment


def register_action_tools(reg, phone: Phone, connections=None, reminders=None,
                          clock=time.time) -> None:

    def _who_question(choices):
        return f"Which one — {', '.join(choices[:-1])} or {choices[-1]}?"

    @reg.tool(
        description=("Calls someone on the phone. Give a name from his "
                     "contacts ('Papa', 'Rahul') or a number. 'Call Ma', "
                     "'Rahul ko phone lagao'."),
        parameters={"type": "object",
                    "properties": {"who": {"type": "string"}},
                    "required": ["who"]},
    )
    def call_person(who: str) -> str:
        number, name, choices = find_number(who, phone, connections)
        if choices:
            return _who_question(choices)
        if not number:
            return f"I can't find {who} in your contacts."
        if not phone.place_call(number):
            return ("I couldn't place the call. Allow Carnage to make phone "
                    "calls, then ask again.")
        return f"Calling {name}."

    @reg.tool(
        description=("Looks someone up in his phone book and says their "
                     "number. 'What's Papa's number?'"),
        parameters={"type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"]},
    )
    def find_contact(name: str) -> str:
        found = phone.find_contacts(name)
        if not found:
            return f"No one called {name} in your contacts."
        return "; ".join(f"{c['name']}: {c['number']}" for c in found[:4]) + "."

    @reg.tool(
        description=("Sets an alarm in his clock app. 'Alarm for 6:30', "
                     "'wake me up at 7 am', 'alarm in 20 minutes'."),
        parameters={"type": "object",
                    "properties": {
                        "time": {"type": "string",
                                 "description": "e.g. 6:30, 7 am, 18:45, in 20 minutes"},
                        "label": {"type": "string"}},
                    "required": ["time"]},
    )
    def set_alarm(time: str, label: str = "") -> str:            # noqa: A002
        moment = _parse_time(time, datetime.fromtimestamp(clock()))
        if moment is None:
            return f"I didn't understand the time '{time}'."
        spoken = moment.strftime("%-I:%M %p")
        if phone.set_alarm(moment.hour, moment.minute, label):
            return f"Alarm set for {spoken}."
        # The clock app can only be reached while Carnage is on screen.
        if reminders is not None:
            reminders.add(label or "Alarm", moment.timestamp())
            return (f"I couldn't reach the clock app from the background, so "
                    f"I'll remind you myself at {spoken}.")
        return "I couldn't set that alarm."

    @reg.tool(
        description="Turns the phone's flashlight on or off.",
        parameters={"type": "object",
                    "properties": {"on": {"type": "boolean"}},
                    "required": ["on"]},
    )
    def flashlight(on: bool) -> str:
        if not phone.torch(bool(on)):
            return "I can't reach the flashlight right now."
        return "Flashlight on." if on else "Flashlight off."

    @reg.tool(
        description=("Opens an app on the phone by name. 'Open Instagram', "
                     "'YouTube kholo'."),
        parameters={"type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"]},
    )
    def open_app(name: str) -> str:
        label = phone.open_app(name)
        if label is None:
            return (f"I couldn't open {name}. Either it isn't installed, or "
                    f"Carnage needs to be on screen to open other apps.")
        return f"Opening {label}."

    @reg.tool(
        description="Turns Do Not Disturb on or off.",
        parameters={"type": "object",
                    "properties": {"on": {"type": "boolean"}},
                    "required": ["on"]},
    )
    def do_not_disturb(on: bool) -> str:
        if not phone.do_not_disturb(bool(on)):
            return "Turn on notification access for Carnage first, then ask again."
        return "Do Not Disturb is on." if on else "Do Not Disturb is off."
