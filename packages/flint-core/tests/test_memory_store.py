import json

from flint_core.memory import MemoryStore


def store(tmp_path, **kw):
    return MemoryStore(tmp_path / "memory.json", **kw)


def test_empty_load(tmp_path):
    memory = store(tmp_path).load()
    assert set(memory) == {
        "identity", "preferences", "projects", "relationships", "places",
        "wishes", "notes"
    }


def test_remember_and_reload(tmp_path):
    s = store(tmp_path)
    assert s.remember("identity", "name", "Tushar") == "remembered identity/name"
    assert s.load()["identity"]["name"]["value"] == "Tushar"
    assert "updated" in s.load()["identity"]["name"]


def test_places_category_renders(tmp_path):
    s = store(tmp_path)
    s.remember("places", "gym", "Gold's Gym near his flat")
    rendered = s.render_for_prompt()
    assert "Places they know" in rendered
    assert "Gold's Gym" in rendered


def test_invalid_category_goes_to_notes(tmp_path):
    s = store(tmp_path)
    s.remember("bogus", "thing", "stuff")
    assert s.load()["notes"]["thing"]["value"] == "stuff"


def test_forget(tmp_path):
    s = store(tmp_path)
    s.remember("wishes", "trip", "visit Japan")
    assert s.forget("wishes", "trip").startswith("forgot")
    assert "trip" not in s.load()["wishes"]
    assert s.forget("wishes", "trip").startswith("not found")


def test_long_values_truncated(tmp_path):
    s = store(tmp_path)
    s.remember("notes", "essay", "x" * 1000)
    assert len(s.load()["notes"]["essay"]["value"]) <= 381


def test_trims_oldest_when_over_budget(tmp_path):
    s = store(tmp_path, max_chars=800)
    for i in range(30):
        s.remember("notes", f"key_{i:02d}", "v" * 60)
    serialized = json.dumps(s.load(), ensure_ascii=False)
    assert len(serialized) <= 800


def test_corrupt_file_recovers(tmp_path):
    s = store(tmp_path)
    s.path.parent.mkdir(parents=True, exist_ok=True)
    s.path.write_text("{broken", encoding="utf-8")
    assert s.load()["identity"] == {}
    s.remember("identity", "name", "T")  # and it can write again
    assert s.load()["identity"]["name"]["value"] == "T"


def test_render_for_prompt(tmp_path):
    s = store(tmp_path)
    assert s.render_for_prompt() == ""
    s.remember("identity", "name", "Tushar")
    s.remember("preferences", "favorite_editor", "VS Code")
    text = s.render_for_prompt()
    assert text.startswith("[WHAT YOU KNOW ABOUT THIS PERSON")
    assert "Name: Tushar" in text
    assert "Favorite Editor: VS Code" in text
