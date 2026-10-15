"""Shield's whole API, against the real app on a phone.

Set SHIELD_URL and SHIELD_TOKEN (e.g. over `adb forward tcp:8791 tcp:8791`).
Everything is made under one throwaway folder and trashed afterwards.
"""

import os
import uuid

import pytest

from shield_client import Shield, ShieldError

URL = os.environ.get("SHIELD_URL")
TOKEN = os.environ.get("SHIELD_TOKEN")
pytestmark = pytest.mark.skipif(not (URL and TOKEN), reason="no phone: set SHIELD_URL/SHIELD_TOKEN")


@pytest.fixture()
def shield():
    return Shield(URL, TOKEN, agent="shield-tests")


@pytest.fixture()
def space(shield):
    root = f"/Test {uuid.uuid4().hex[:6]}"
    folder = shield.folder(root)
    yield root
    shield.trash(folder.id)


def test_a_wrong_token_gets_nothing():
    with pytest.raises(ShieldError) as wrong:
        Shield(URL, "nope", agent="x").health()
    assert wrong.value.status == 401


def test_no_ai_may_call_itself_you():
    with pytest.raises(ShieldError) as posing:
        Shield(URL, TOKEN, agent="you").health()
    assert posing.value.status == 400


def test_notes_files_and_links_are_saved_where_asked_and_marked_as_ours(shield, space):
    note = shield.save_note(f"{space}/Goa", "Hotels", "Taj is ₹8,400 a night", tags=["Travel"])
    assert note.created_by == "shield-tests" and note.tags == ["travel"]
    assert shield.read(note.id) == "Taj is ₹8,400 a night"
    assert shield.get(f"{space}/Goa/Hotels").id == note.id

    data = bytes(range(256)) * 10
    file = shield.upload(f"{space}/Goa", "blob.bin", data)
    assert shield.download(file.id) == data

    text = shield.upload(f"{space}/Goa", "plan.md", b"# Day 1\nBeach at Baga")
    assert "Baga" in shield.read(text.id)

    link = shield.save_link(f"{space}/Goa", "Taj site", "https://example.com/taj")
    assert shield.read(link.id) == "https://example.com/taj"
    names = {i.name for i in shield.list(f"{space}/Goa")}
    assert names == {"Hotels", "blob.bin", "plan.md", "Taj site"}


def test_a_taken_name_is_kept_apart(shield, space):
    first = shield.save_note(space, "Same", "one")
    second = shield.save_note(space, "Same", "two")
    assert first.name == "Same" and second.name == "Same (2)"


def test_search_finds_words_inside_by_prefix_and_filters(shield, space):
    tag = f"t{uuid.uuid4().hex[:6]}"
    shield.save_note(space, "Passport scan notes", "renewal due in March, Pune office", tags=[tag])
    shield.upload(space, "visa.txt", b"embassy appointment friday")
    assert any(i.name == "Passport scan notes" for i in shield.search("pass pune"))
    assert any(i.name == "visa.txt" for i in shield.search("embassy"))
    assert [i.name for i in shield.search(tag=tag)] == ["Passport scan notes"]
    assert all(i.created_by == "shield-tests" for i in shield.search("passport", by="shield-tests"))
    assert any(i.name == "Passport scan notes" for i in shield.search("", by="ai", limit=200))


def test_updates_moves_tags_and_stars_are_recorded(shield, space):
    note = shield.save_note(space, "Draft", "v1")
    shield.folder(f"{space}/Archive")
    moved = shield.update(note.id, name="Final", text="v2", starred=True, add_tags=["done"])
    assert (moved.name, moved.text, moved.starred, moved.tags) == ("Final", "v2", True, ["done"])
    shield.move(note.id, f"{space}/Archive")
    assert shield.get(f"{space}/Archive/Final").id == note.id
    actions = [h["action"] for h in shield.history(note.id)]
    assert actions[-1] == "added" and "changed" in actions
    assert all(h["by"] == "shield-tests" for h in shield.history(note.id))


def test_a_folder_cannot_go_inside_itself(shield, space):
    outer = shield.folder(f"{space}/Outer")
    shield.folder(f"{space}/Outer/Inner")
    with pytest.raises(ShieldError) as loop:
        shield.move(outer.id, f"{space}/Outer/Inner")
    assert loop.value.status == 400


def test_trash_is_reversible_and_nothing_is_purged_through_the_api(shield, space):
    note = shield.save_note(space, "Keep me", "x")
    shield.trash(note.id)
    assert all(i.id != note.id for i in shield.list(space))
    assert all(i.id != note.id for i in shield.search("keep"))
    shield._call("POST", f"/v1/items/{note.id}/restore")
    assert any(i.id == note.id for i in shield.list(space))


def test_links_and_activity(shield, space):
    a = shield.save_note(space, "Trip", "Goa")
    b = shield.save_note(space, "Budget", "₹40k")
    shield.link(a.id, b.id, "costs")
    linked = shield._call("GET", f"/v1/items/{a.id}/links")["links"]
    assert [(i["name"], i["label"]) for i in linked] == [("Budget", "costs")]
    recent = shield.activity(limit=20, by="shield-tests")
    assert recent and all(r["by"] == "shield-tests" for r in recent)


def test_an_ai_can_never_delete_for_good(shield, space):
    note = shield.save_note(space, "Precious", "x")
    shield.trash(note.id)
    with pytest.raises(ShieldError) as refused:
        shield._call("DELETE", f"/v1/items/{note.id}", params={"forever": 1})
    assert refused.value.status == 409
    assert shield.get(note.id).name == "Precious"          # still there, in the Trash


def test_slash_means_the_top_and_nothing_means_the_inbox(shield, space):
    top = shield.folder(f"/{space.strip('/')} top")
    try:
        assert top.parent is None
        loose = shield._create({"kind": "note", "name": "loose", "text": "x"})
        assert shield.get(loose.parent).name == "Inbox"
        shield.trash(loose.id)
    finally:
        shield.trash(top.id)
