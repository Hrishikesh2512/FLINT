"""The tools that only make sense on a body with a cellular radio in it.

Everything else Carnage can do comes from `flint_core.skills` unchanged — she
tracks projects, searches the archive and commits code with the same code the
Pi runs, because none of those answers depend on which device is asking. What
is here is the remainder: the four things a phone knows that a Pi does not.

The important one is `sos_sms`. Venom's SOS reaches people over WhatsApp,
which needs the internet, which means the feature is unavailable in exactly
the situation it exists for — no signal, bad area, dead hotspot. A phone can
put a message on the cellular network with no data connection at all. That is
not a better version of the same feature; it is the first version that holds
up under its own premise.
"""

from __future__ import annotations

import logging

from carnage.platform import Phone

log = logging.getLogger("carnage.tools")

#: How many recent notifications are worth reading out before it stops being
#: an answer and starts being a list.
NOTIFICATION_LIMIT = 6


def register_phone_tools(reg, phone: Phone) -> None:
    """Battery, location, and the notification shade — reading, not sending."""

    @reg.tool(
        description=("Says how much battery the phone has left. Use for "
                     "'how much battery?', 'kitni battery hai?', and before "
                     "anything long-running she should warn him about."),
    )
    def phone_battery() -> str:
        reading = phone.battery()
        if reading is None:
            return "I can't read the battery right now."
        if reading.percent <= 15 and not reading.charging:
            return (f"{reading.spoken()} — worth plugging in before you go "
                    f"anywhere.")
        return reading.spoken()

    @reg.tool(
        description=("Says where he is right now, from the phone's GPS — a "
                     "real position, not a city guess. Use for 'where am I?', "
                     "'main kahan hoon?', and whenever a task needs to know "
                     "whether he's still at home or already out."),
    )
    def where_am_i() -> str:
        fix = phone.locate()
        if fix is None:
            return ("I can't get a location fix — location may be off, or "
                    "there's no signal indoors.")
        return fix.spoken()

    @reg.tool(
        description=("Reads what's waiting in the notification shade. Use for "
                     "'what did I miss?', 'koi message aaya?'. Summarise; "
                     "never read every one out."),
    )
    def check_notifications() -> str:
        waiting = phone.notifications()
        if not waiting:
            return "Nothing waiting."
        recent = sorted(waiting, key=lambda n: n.when, reverse=True)
        lines = [f"{n.app.split('.')[-1]}: {n.title} — {n.text}".strip(" —")
                 for n in recent[:NOTIFICATION_LIMIT]]
        extra = len(recent) - len(lines)
        tail = f" and {extra} more" if extra > 0 else ""
        return "; ".join(lines) + tail + "."


def register_sms_tools(reg, phone: Phone, contacts=None) -> None:
    """Sending a text. Separate from reading the phone, because sending is the
    half that can embarrass him — the permission and the capability that
    carries it should be switchable on their own."""

    @reg.tool(
        description=(
            "Sends a text message over the cellular network. Use this when "
            "there's no internet, or when it genuinely matters that it "
            "arrives — SMS goes through when WhatsApp cannot. Give the number "
            "or a name from his contacts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string",
                       "description": "Phone number, or a name he'd recognise"},
                "text": {"type": "string", "description": "The message itself"},
            },
            "required": ["to", "text"],
        },
    )
    def send_text(to: str, text: str) -> str:
        number = _resolve(to, contacts)
        if not number:
            return f"I don't have a number for {to}."
        if not phone.send_sms(number, text):
            return (f"I couldn't send that to {to} — the message did not go. "
                    f"Worth trying another way.")
        if getattr(phone, "sends_directly", True):
            return f"Sent to {to}."
        # A body that can only pre-fill the messaging app. Saying "sent" here
        # would be a lie he would not discover until it mattered.
        return (f"I've opened it for {to} — tap send and it's away.")


def register_sos_sms(reg, phone: Phone, sos) -> None:
    """The emergency path that does not need the internet.

    Kept separate from the ordinary phone tools so it can be registered on its
    own: a device with no contact book has nothing to offer here, and a device
    with one should have this even if every other phone skill is switched off.
    """

    @reg.tool(
        description=(
            "Sends an emergency SMS to every emergency contact, over the "
            "cellular network so it works with no internet at all. Use when "
            "he says SOS, help me, bachao — or when WhatsApp alerts could not "
            "be delivered. Include where he is if you know it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "note": {"type": "string",
                         "description": "What is happening, in his words"},
            },
        },
    )
    def sos_sms(note: str = "") -> str:
        contacts = _sos_numbers(sos)
        if not contacts:
            return ("I don't have any emergency contacts set up — add one and "
                    "I'll be able to reach them.")
        fix = phone.locate()
        where = f" Location: {fix.spoken()}." if fix else ""
        body = (f"EMERGENCY — {note.strip() or 'he needs help'}.{where} "
                f"Sent automatically.")

        sent, failed = [], []
        for name, number in contacts:
            (sent if phone.send_sms(number, body) else failed).append(name)

        direct = getattr(phone, "sends_directly", True)
        if not direct and sent:
            # This body can only open the messaging app. In an emergency the
            # difference between "I told them" and "you still have to press
            # send" is the entire message, so it leads rather than trails.
            tail = (f" I couldn't even open one for {', '.join(failed)}."
                    if failed else "")
            return (f"Tap send — I've opened a message to {', '.join(sent)}, "
                    f"one after another. They are NOT sent until you do.{tail}")
        if sent and not failed:
            return f"Texted {', '.join(sent)}. Stay where you are."
        if sent:
            # Naming who was *not* reached is the whole point: "sent" that
            # quietly means "sent to two of five" is worse than a failure,
            # because he will stop trying.
            return (f"Texted {', '.join(sent)}. Could not reach "
                    f"{', '.join(failed)} — try them another way.")
        return ("I could not send any of those texts. There may be no "
                "cellular signal at all — try calling.")


def _sos_numbers(sos) -> list[tuple[str, str]]:
    """(name, number) for each *enabled* emergency contact.

    `enabled_only` matters: someone switched off in the SOS panel was switched
    off deliberately, and an emergency is the worst possible moment to
    rediscover an old number that should not have been used.
    """
    if sos is None:
        return []
    try:
        people = sos.contacts(enabled_only=True)
    except Exception:                       # noqa: BLE001
        log.exception("could not read the emergency contact book")
        return []
    out = []
    for person in people or ():
        name = str(person.get("name", "") or "someone")
        number = str(person.get("to", "") or "").strip()
        if number:
            out.append((name, number))
    return out


def _resolve(who: str, contacts) -> str:
    """A number from whatever he said — a number stays a number."""
    who = (who or "").strip()
    if not who:
        return ""
    if who.replace("+", "").replace(" ", "").replace("-", "").isdigit():
        return who
    if contacts is None:
        return ""
    try:
        # ConnectionStore already resolves nicknames and aliases to one record
        # and hands back a usable number, so name lookup is its problem, not
        # this module's.
        return str(contacts.phone_for(who) or "")
    except Exception:                       # noqa: BLE001
        log.exception("contact lookup failed for %r", who)
        return ""
