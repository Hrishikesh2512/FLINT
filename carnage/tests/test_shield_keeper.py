"""Her memory in Shield, both ways; and Shield's Inbox sorting itself."""

import itertools
from dataclasses import replace
from datetime import datetime

import pytest
from flint_core.memory.store import MemoryStore
from flint_core.recall import EPISODE, Archive
from flint_core.stores import ListStore, NoteStore, ReminderStore
from shield_client import Item

from carnage.shield_keeper import Mirror, Organizer

NOW = datetime(2026, 10, 15, 12, 0).timestamp()


class Shield:
    """Shield, in memory: enough to see what the keeper does to it."""

    def __init__(self):
        self.items: dict[str, Item] = {}
        self.ids = (f"id{n}" for n in itertools.count())
        self.downloads = {}

    def add(self, folder, name, kind="note", text=None, **kw):
        item = Item(id=next(self.ids), kind=kind, name=name, parent=folder, text=text,
                    created_by="jarvis", updated_by="jarvis", created=int(NOW * 1000) - 3_600_000,
                    **kw)
        self.items[item.id] = item
        return item

    def save_note(self, folder, name, text, tags=None):
        return self.add(folder, name, text=text, tags=tags or [])

    def get(self, ref):
        return self.items[ref]

    def update(self, ref, **changes):
        item = self.items[ref]
        if "path" in changes:
            changes["parent"] = changes.pop("path")
        tags = item.tags + changes.pop("add_tags", [])
        item = replace(item, tags=tags, updated_by="jarvis", **changes)
        self.items[ref] = item
        return item

    def he_edits(self, ref, text):
        self.items[ref] = replace(self.items[ref], text=text, updated_by="you")

    def list(self, folder):
        where = None if folder == "/" else folder
        return [i for i in self.items.values() if i.parent == where]

    def download(self, ref):
        return self.downloads[ref]

    def note(self, folder, name):
        return next(i for i in self.items.values() if i.parent == folder and i.name == name)


class Carnage:
    def __init__(self, tmp):
        self.memory = MemoryStore(tmp / "memory.json")
        self.lists = ListStore(tmp / "lists.json")
        self.notes = NoteStore(tmp / "notes.json", clock=lambda: NOW)
        self.reminders = ReminderStore(tmp / "reminders.json")
        self.archive = Archive(tmp / "archive.db")


@pytest.fixture()
def world(tmp_path):
    c, shield = Carnage(tmp_path), Shield()
    return c, shield, Mirror(c, shield, tmp_path / "mirror.json", clock=lambda: NOW)


def test_her_memory_appears_in_shield(world):
    c, shield, mirror = world
    c.memory.remember("places", "home", "Kothrud, Pune")
    c.lists.add_item("milk", "shopping")
    c.notes.add("call the plumber")
    mirror.sync()
    assert "home: Kothrud, Pune" in shield.note("/Jarvis/Memory", "Places").text
    assert "- milk" in shield.note("/Jarvis/Lists", "Shopping").text
    assert "- call the plumber" in shield.note("/Jarvis", "Notes").text


def test_his_edits_in_shield_flow_back(world):
    c, shield, mirror = world
    c.memory.remember("places", "home", "Kothrud, Pune")
    c.memory.remember("places", "office", "Baner")
    c.lists.add_item("milk", "shopping")
    mirror.sync()

    places = shield.note("/Jarvis/Memory", "Places")
    shield.he_edits(places.id, "> header\nhome: Aundh, Pune\ngym: Cult, Baner\n")
    shopping = shield.note("/Jarvis/Lists", "Shopping")
    shield.he_edits(shopping.id, "- bread\n- eggs")
    mirror.sync()

    facts = c.memory.load()["places"]
    assert facts["home"]["value"] == "Aundh, Pune" and "office" not in facts
    assert facts["gym"]["value"] == "Cult, Baner"
    assert c.lists.show("shopping") == ["bread", "eggs"]


def test_her_own_changes_overwrite_nothing_he_did_not_touch(world):
    c, shield, mirror = world
    mirror.sync()
    c.memory.remember("wishes", "trip", "Ladakh in June")
    mirror.sync()
    assert "trip: Ladakh in June" in shield.note("/Jarvis/Memory", "Wishes").text
    assert "trip" in c.memory.load()["wishes"]


def test_notes_and_reminders_follow_his_deletions(world):
    c, shield, mirror = world
    c.notes.add("one")
    c.notes.add("two")
    c.reminders.add("pay rent", NOW + 86_400)
    c.reminders.add("dentist", NOW + 2 * 86_400)
    mirror.sync()
    shield.he_edits(shield.note("/Jarvis", "Notes").id, "- two\n- three")
    reminders = shield.note("/Jarvis", "Reminders")
    shield.he_edits(reminders.id, "\n".join(line for line in reminders.text.splitlines()
                                            if "dentist" not in line))
    mirror.sync()
    assert [n["text"] for n in c.notes.all()] == ["two", "three"]
    assert [r["text"] for r in c.reminders.pending()] == ["pay rent"]


def test_whatsapp_chats_are_shown_and_never_edited_back(world):
    c, shield, mirror = world
    c.archive.remember("12 Mar 2026, with Rahul: Goa plan final", kind=EPISODE,
                       subject="WhatsApp Rahul")
    mirror.sync()
    chat = shield.note("/Jarvis/WhatsApp", "Rahul")
    assert "Goa plan final" in chat.text
    shield.he_edits(chat.id, "nothing")
    mirror.sync()
    assert "Goa plan final" in shield.get(chat.id).text      # put back as it was
    assert len(c.archive.about("WhatsApp Rahul")) == 1


def test_a_mirrored_note_he_trashes_comes_back(world):
    c, shield, mirror = world
    mirror.sync()
    notes = shield.note("/Jarvis", "Notes")
    shield.items[notes.id] = replace(notes, trashed=1)
    mirror.sync()
    assert any(i.name == "Notes" and not i.trashed for i in shield.list("/Jarvis"))


# ── the organizer ───────────────────────────────────────────────────────────
def organizer(shield, decision, reminders):
    seen = {}

    def decide(prompt, system, function, files=None):
        seen.update(prompt=prompt, files=files)
        return decision

    return Organizer(shield, decide, reminders, clock=lambda: NOW), seen


def test_an_inbox_photo_is_named_filed_tagged_and_its_dates_remembered(tmp_path):
    shield = Shield()
    shield.add(None, "College", kind="folder")
    scan = shield.add("/Inbox", "IMG_2041.jpg", kind="file", mime="image/jpeg", size=10,
                      tags=["auto", "screenshots"])
    shield.downloads[scan.id] = b"jpeg"
    reminders = ReminderStore(tmp_path / "r.json")
    sort, seen = organizer(shield, {
        "name": "Passport.jpg", "folder": "Documents/IDs", "tags": ["id", "travel"],
        "about": "Indian passport, expires 10 Jan 2027.",
        "dates": [{"what": "Passport expires", "date": "2027-01-10", "kind": "expiry"}]}, reminders)

    assert sort.sort_one() == "Passport.jpg"
    done = shield.get(scan.id)
    assert done.parent == "/Documents/IDs" and "sorted" in done.tags and "id" in done.tags
    assert done.about.startswith("Indian passport") and "Reminders set" in done.about
    assert seen["files"][0]["mime"] == "image/jpeg"
    assert "/College" in seen["prompt"]
    due = sorted(r["due"] for r in reminders.pending())
    assert [datetime.fromtimestamp(d).strftime("%Y-%m-%d %H") for d in due] == \
        ["2026-12-11 09", "2027-01-10 09"]


def test_past_dates_make_no_reminders_and_inbox_is_never_the_answer(tmp_path):
    shield = Shield()
    note = shield.add("/Inbox", "bill.txt", kind="file", mime="text/plain", text="Due 2026-01-01")
    reminders = ReminderStore(tmp_path / "r.json")
    old = [{"what": "Bill due", "date": "2026-01-01", "kind": "due"}]
    sort, _ = organizer(shield, {"name": "Bill.txt", "folder": "/Inbox", "about": "Old bill.",
                                 "dates": old}, reminders)
    sort.sort_one()
    assert shield.get(note.id).parent == "/Sorted" and reminders.pending() == []


def test_a_thing_that_keeps_failing_is_left_for_him(tmp_path):
    shield = Shield()
    stuck = shield.add("/Inbox", "weird.bin", kind="file")

    def broken(*_, **__):
        raise RuntimeError("quota")

    sort = Organizer(shield, broken, ReminderStore(tmp_path / "r.json"), clock=lambda: NOW)
    for _ in range(3):
        sort.sort_one()
    assert "unsorted" in shield.get(stuck.id).tags
    assert sort.sort_one() is None
