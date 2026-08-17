"""Voice tools over the connection book — who someone is, and how to reach them.

The store resolves nicknames and aliases to one record, so this layer never
does name matching of its own. Worth keeping shared rather than per-device:
a phone number written down on the Pi is the same number the phone needs, and
duplicating the book is how two devices end up disagreeing about a person.
"""

from __future__ import annotations


def register_connection_tools(reg, connections):
    """Saving, recalling and forgetting people."""
    @reg.tool(
        description=(
            "Saves or updates what you know about a PERSON in the user's "
            "Connections — their phone number, nickname, Instagram, an "
            "interest, or any note. Call SILENTLY whenever the user shares "
            "someone's details, even one detail at a time: 'Rahul ka number "
            "98765 43210 hai', 'my friend Priya loves painting', 'Amit's "
            "insta is amit.k', 'Rahul ko bhai bulao'. Everything merges into "
            "that one person, so partial info is fine. Pass the person's "
            "name plus whichever fields were mentioned."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name":      {"type": "string",
                              "description": "The person's name"},
                "phone":     {"type": "string",
                              "description": "Phone number, any format"},
                "nickname":  {"type": "string",
                              "description": "A nickname/alias for them"},
                "instagram": {"type": "string",
                              "description": "Instagram handle"},
                "interest":  {"type": "string",
                              "description": "Something they're into"},
                "note":      {"type": "string",
                              "description": "Any other fact to remember"},
            },
            "required": ["name"],
        },
    )
    def save_connection(name: str, phone: str = "", nickname: str = "",
                        instagram: str = "", interest: str = "",
                        note: str = "") -> str:
        rec = connections.save(name, phone=phone, nickname=nickname,
                               instagram=instagram, interest=interest,
                               note=note)
        if not rec:
            return "I need a name to save that against."
        return f"Saved — {rec['name']} is in your connections."

    @reg.tool(
        description=(
            "Recalls everything saved about a person in Connections — their "
            "number, nickname, Instagram, interests and notes. Use for 'what "
            "do you know about Rahul', 'Priya ka number kya hai', 'tell me "
            "about Amit', 'Rahul ka insta?'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Who to look up (name or nickname)"},
            },
            "required": ["name"],
        },
    )
    def get_connection(name: str) -> str:
        return connections.describe(name)

    @reg.tool(
        description=("Lists the people saved in the user's Connections. Use "
                     "for 'who all do you have saved', 'list my contacts'."),
    )
    def list_connections() -> str:
        names = connections.all_names()
        if not names:
            return "You haven't saved anyone in connections yet."
        return "You have saved: " + ", ".join(names) + "."

    @reg.tool(
        description=("Removes a person from the user's Connections. Use for "
                     "'forget Rahul', 'delete Amit from contacts'."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Who to remove"},
            },
            "required": ["name"],
        },
    )
    def forget_connection(name: str) -> str:
        return (f"Removed {name} from your connections."
                if connections.forget(name)
                else f"I don't have anyone saved as {name}.")
