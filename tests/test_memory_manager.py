"""Tests for memory.memory_manager — persistence, trimming, prompt formatting."""

import json

import pytest

from memory import memory_manager as mm


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Point the module at a throwaway file so tests never touch real memory."""
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")


def test_load_missing_file_returns_empty_schema():
    memory = mm.load_memory()
    assert set(memory) == {
        "identity", "preferences", "projects", "relationships", "wishes", "notes"
    }
    assert all(v == {} for v in memory.values())


def test_update_and_reload_round_trip():
    mm.update_memory({"identity": {"name": {"value": "Tushar"}}})
    reloaded = mm.load_memory()
    assert reloaded["identity"]["name"]["value"] == "Tushar"
    assert "updated" in reloaded["identity"]["name"]


def test_update_ignores_empty_values():
    mm.update_memory({"identity": {"name": {"value": "Tushar"}}})
    mm.update_memory({"identity": {"name": None, "city": ""}})
    reloaded = mm.load_memory()
    assert reloaded["identity"]["name"]["value"] == "Tushar"
    assert "city" not in reloaded["identity"]


def test_long_values_are_truncated():
    mm.update_memory({"notes": {"essay": {"value": "x" * 1000}}})
    value = mm.load_memory()["notes"]["essay"]["value"]
    assert len(value) <= mm.MAX_VALUE_LENGTH + 1  # +1 for the ellipsis


def test_memory_is_trimmed_to_char_limit():
    for i in range(60):
        mm.update_memory({"notes": {f"key_{i:02d}": {"value": "v" * 100}}})
    serialized = json.dumps(mm.load_memory(), ensure_ascii=False)
    assert len(serialized) <= mm.MEMORY_MAX_CHARS


def test_corrupt_file_recovers_to_empty():
    mm.MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    mm.MEMORY_PATH.write_text("{not json", encoding="utf-8")
    assert mm.load_memory()["identity"] == {}


def test_format_memory_for_prompt():
    mm.update_memory({
        "identity": {"name": {"value": "Tushar"}},
        "preferences": {"favorite_editor": {"value": "VS Code"}},
    })
    text = mm.format_memory_for_prompt(mm.load_memory())
    assert "Name: Tushar" in text
    assert "Favorite Editor: VS Code" in text
    assert text.startswith("[WHAT YOU KNOW ABOUT THIS PERSON")


def test_format_empty_memory_is_blank():
    assert mm.format_memory_for_prompt(mm.load_memory()) == ""
    assert mm.format_memory_for_prompt(None) == ""


def test_remember_and_forget():
    mm.remember("test_key", "test value", category="notes")
    assert mm.load_memory()["notes"]["test_key"]["value"] == "test value"
    assert mm.forget("test_key", category="notes").startswith("Forgotten")
    assert "test_key" not in mm.load_memory()["notes"]
    assert mm.forget("test_key", category="notes").startswith("Not found")


def test_invalid_category_falls_back_to_notes():
    mm.remember("weird", "thing", category="nonsense")
    assert mm.load_memory()["notes"]["weird"]["value"] == "thing"
