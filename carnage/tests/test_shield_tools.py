"""Jarvis and Shield: search, read, list, save, organise."""

from shield_client import Item

from carnage.tools_shield import register_shield_tools, shield_connector


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self, **_):
        def keep(fn):
            self.tools[fn.__name__] = fn
            return fn
        return keep


def item(**kw):
    base = {"id": "i1", "kind": "note", "name": "Goa plan", "parent": None, "created_by": "you"}
    base.update(kw)
    return Item.from_json(base)


class Shield:
    """Shield as far as her tools can tell."""

    def __init__(self):
        self.saved, self.updated = [], []
        self.things = {"i1": item(text="Beach day 1"),
                       "p1": item(id="p1", kind="file", name="passport.pdf",
                                  mime="application/pdf"),
                       "f1": item(id="f1", kind="folder", name="College")}

    def search(self, query, tag=None, limit=10):
        return [i for i in self.things.values() if query.lower() in i.name.lower()]

    def get(self, ref):
        return self.things[ref] if not ref.startswith("/") else self.things["f1"]

    def list(self, folder):
        return [self.things["i1"]] if folder == "f1" else list(self.things.values())

    def download(self, ref):
        return b"%PDF-1.4"

    def save_note(self, folder, name, text, tags=None):
        self.saved.append((folder, name, text, tags))
        return item(name=name)

    def update(self, ref, **changes):
        self.updated.append((ref, changes))
        return item(name=changes.get("name", "Goa plan"))


def tools(shield, describe=None):
    reg = Registry()
    register_shield_tools(reg, lambda: shield, describe)
    return reg.tools


def test_search_says_what_was_found_with_ids():
    found = tools(Shield())["shield_search"]("goa")
    assert "Goa plan" in found and "id=i1" in found
    assert "Nothing in Shield" in tools(Shield())["shield_search"]("tax")


def test_reading_a_note_gives_its_text_and_a_pdf_is_looked_at():
    seen = {}

    def describe(prompt, data, system, mime="image/jpeg"):
        seen.update(prompt=prompt, mime=mime)
        return "Passport, expires 2031."

    t = tools(Shield(), describe)
    assert t["shield_read"]("i1") == "Beach day 1"
    assert t["shield_read"]("p1", "when does it expire?") == "Passport, expires 2031."
    assert seen["mime"] == "application/pdf" and "expire" in seen["prompt"]
    assert "In College" in t["shield_read"]("f1")


def test_saving_goes_to_jarvis_folder_unless_told_and_says_where():
    shield = Shield()
    said = tools(shield)["shield_save"]("Wifi", "password is on the router")
    assert shield.saved[0][0] == "/Jarvis" and "Shield › Jarvis" in said
    tools(shield)["shield_save"]("Exam", "DBMS on 12th", folder="College/Exams", tags=["exam"])
    assert shield.saved[1][:2] == ("/College/Exams", "Exam") and shield.saved[1][3] == ["exam"]


def test_organise_moves_renames_and_tags():
    shield = Shield()
    tools(shield)["shield_organise"]("i1", to_folder="College", new_name="Goa", add_tags=["trip"])
    assert shield.updated == [("i1", {"path": "/College", "name": "Goa", "add_tags": ["trip"]})]


def test_no_shield_on_the_phone_is_said_plainly():
    reg = Registry()
    register_shield_tools(reg, lambda: None)
    assert "isn't installed" in reg.tools["shield_search"]("x")


def test_the_connector_uses_the_token_shield_hands_over():
    class Phone:
        def shield_access(self):
            return {"token": "abc", "port": 8791}

    client = shield_connector(Phone())()
    assert client._url == "http://127.0.0.1:8791"
    assert client._http.headers["X-Shield-Agent"] == "jarvis"
    assert shield_connector(object())() is None
