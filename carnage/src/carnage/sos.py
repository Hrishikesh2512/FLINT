"""The emergency contact book on the phone.

The same document Venom keeps (`venom/sos.py`), field for field, so a book
copied from the Pi works here unchanged:

    {"contacts": [{"name", "to", "label", "message", "enabled"}], ...}

Only the part SMS needs is here — who to reach, and whether they are switched
on. Venom's repeat-every-ten-minutes loop rides WhatsApp; on the phone the
alert is an SMS that works with no internet, which is the reason this body
has an SOS at all (see `tools_phone.register_sos_sms`).
"""

from __future__ import annotations

import logging

from flint_core.stores import _JsonStore

log = logging.getLogger("carnage.sos")


class SosStore(_JsonStore):
    """Emergency contacts, on disk in the state dir. Never raises on read."""

    @staticmethod
    def _clean(raw: dict) -> dict:
        return {
            "name": str(raw.get("name", "")).strip(),
            "to": str(raw.get("to", "")).strip(),
            "label": str(raw.get("label", "")).strip(),
            "message": str(raw.get("message", "")).strip(),
            "enabled": bool(raw.get("enabled", True)),
        }

    def _document(self) -> dict:
        raw = self._load({})
        return raw if isinstance(raw, dict) else {}

    def contacts(self, enabled_only: bool = False) -> list[dict]:
        people = [self._clean(c) for c in (self._document().get("contacts") or [])
                  if isinstance(c, dict) and str(c.get("name", "")).strip()]
        return [c for c in people if c["enabled"]] if enabled_only else people

    def add(self, name: str, to: str) -> dict:
        """Add someone, or update their number — matched by name, so saying
        it twice edits one person instead of texting them twice."""
        name, to = (name or "").strip(), (to or "").strip()
        if not name or not to:
            return {}
        with self._lock:
            document = self._document()
            people = self.contacts()
            for person in people:
                if person["name"].lower() == name.lower():
                    person.update(to=to, enabled=True)
                    break
            else:
                people.append(self._clean({"name": name, "to": to}))
            document["contacts"] = people
            self._save(document)
        return next(p for p in people if p["name"].lower() == name.lower())

    def remove(self, name: str) -> bool:
        with self._lock:
            document = self._document()
            people = self.contacts()
            keep = [p for p in people if p["name"].lower() != (name or "").strip().lower()]
            if len(keep) == len(people):
                return False
            document["contacts"] = keep
            self._save(document)
        return True


def register_sos_contact_tools(reg, sos: SosStore) -> None:
    """Managing the book by voice — the phone has no settings page for it."""

    @reg.tool(
        description=("Adds someone to his emergency contacts, or updates their "
                     "number. These are the people sos_sms texts."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Who, e.g. 'Papa'"},
                "number": {"type": "string", "description": "Their phone number"},
            },
            "required": ["name", "number"],
        },
    )
    def add_emergency_contact(name: str, number: str) -> str:
        if not sos.add(name, number):
            return "I need both a name and a number for that."
        return f"{name} is an emergency contact now."

    @reg.tool(
        description="Removes someone from his emergency contacts.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    def remove_emergency_contact(name: str) -> str:
        if sos.remove(name):
            return f"Removed {name} from the emergency contacts."
        return f"{name} wasn't an emergency contact."

    @reg.tool(description="Says who his emergency contacts are.")
    def list_emergency_contacts() -> str:
        people = sos.contacts()
        if not people:
            return "There are no emergency contacts yet."
        return "; ".join(
            f"{p['name']} ({p['to']})" + ("" if p["enabled"] else " — switched off")
            for p in people) + "."
