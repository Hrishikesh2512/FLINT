"""Writing documents — and the two things that must never happen."""

from __future__ import annotations

import pytest

from flint_core.documents import (
    list_documents,
    read_document,
    safe_name,
    write_document,
    write_presentation,
    write_spreadsheet,
)


# ── never escape the folder ─────────────────────────────────────────────────
@pytest.mark.parametrize("asked", [
    "../../.ssh/authorized_keys",
    "/etc/passwd",
    "..\\..\\Windows\\System32\\hosts",
    "notes/../../../secrets.txt",
])
def test_a_filename_is_a_filename_not_a_path(asked):
    """Path separators are stripped, never followed."""
    cleaned = safe_name(asked)
    assert "/" not in cleaned and "\\" not in cleaned
    assert not cleaned.startswith("..")


def test_writing_cannot_escape_the_folder(tmp_path):
    outside = tmp_path / "outside.txt"
    inside = tmp_path / "docs"
    inside.mkdir()
    result = write_document(inside, "../outside.txt", "gotcha")
    assert result.ok is True
    assert not outside.exists()                  # it did not escape
    assert (inside / "outside.txt").exists()


def test_a_nameless_document_still_gets_a_name():
    assert safe_name("") == "document.txt"
    assert safe_name("   ") == "document.txt"


def test_an_extension_is_added_when_missing():
    assert safe_name("meeting notes", "md") == "meeting notes.md"


def test_an_existing_extension_is_respected():
    assert safe_name("report.csv", "md") == "report.csv"


# ── never overwrite silently ────────────────────────────────────────────────
def test_an_existing_file_is_not_replaced_by_accident(tmp_path):
    (tmp_path / "notes.md").write_text("yesterday's notes", encoding="utf-8")
    result = write_document(tmp_path, "notes.md", "today's notes")
    assert result.ok is False
    assert "already exists" in result.detail
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "yesterday's notes"


def test_overwriting_works_when_actually_asked(tmp_path):
    (tmp_path / "notes.md").write_text("old", encoding="utf-8")
    result = write_document(tmp_path, "notes.md", "new", overwrite=True)
    assert result.ok is True
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "new"


def test_a_missing_folder_is_reported(tmp_path):
    result = write_document(tmp_path / "nope", "x.md", "content")
    assert result.ok is False and "no folder" in result.detail


# ── the native formats ──────────────────────────────────────────────────────
def test_a_markdown_document_gets_its_title(tmp_path):
    write_document(tmp_path, "notes", "The body.", title="Monday Standup")
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == (
        "# Monday Standup\n\nThe body.")


def test_a_text_document_gets_no_markdown_heading(tmp_path):
    write_document(tmp_path, "notes.txt", "The body.", title="Ignored")
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "The body."


def test_a_spreadsheet_is_written_as_csv(tmp_path):
    write_spreadsheet(tmp_path, "spend", [["chai", 40], ["bus", 25]],
                      headers=["item", "rupees"])
    written = (tmp_path / "spend.csv").read_text(encoding="utf-8")
    assert written.splitlines()[0] == "item,rupees"
    assert "chai,40" in written


def test_rows_are_not_separated_by_blank_lines(tmp_path):
    """Regression: csv.writer's "\\r\\n" plus the text write's own translation
    produced "\\r\\r\\n" — a blank line between every row."""
    write_spreadsheet(tmp_path, "spend", [["a", 1], ["b", 2]], headers=["x", "y"])
    raw = (tmp_path / "spend.csv").read_bytes()
    assert b"\r\r" not in raw
    lines = [ln for ln in (tmp_path / "spend.csv").read_text(
        encoding="utf-8").splitlines() if ln.strip()]
    assert lines == ["x,y", "a,1", "b,2"]


def test_a_tsv_uses_tabs(tmp_path):
    write_spreadsheet(tmp_path, "spend.tsv", [["a", 1]])
    assert "a\t1" in (tmp_path / "spend.tsv").read_text(encoding="utf-8")


def test_an_empty_spreadsheet_is_refused(tmp_path):
    result = write_spreadsheet(tmp_path, "empty", [])
    assert result.ok is False and "nothing to put in it" in result.detail


def test_slides_are_written_as_separated_markdown(tmp_path):
    write_presentation(tmp_path, "deck", [
        {"title": "The Problem", "bullets": ["it's slow", "it's expensive"]},
        {"title": "The Fix", "bullets": ["do less"], "notes": "keep it short"},
    ], title="Q3 Review")
    written = (tmp_path / "deck.md").read_text(encoding="utf-8")
    assert "# Q3 Review" in written
    assert written.count("\n---\n") == 2
    assert "- it's slow" in written
    assert "> keep it short" in written


def test_an_empty_deck_is_refused(tmp_path):
    assert write_presentation(tmp_path, "deck", []).ok is False


def test_something_absurdly_long_is_refused(tmp_path):
    from flint_core.documents import MAX_CHARS

    result = write_document(tmp_path, "huge.md", "x" * (MAX_CHARS + 1))
    assert result.ok is False and "far too long" in result.detail


# ── binary formats degrade clearly ──────────────────────────────────────────
def missing(monkeypatch, module):
    import builtins

    real = builtins.__import__

    def blocked(name, *args, **kw):
        if name == module or name.startswith(f"{module}."):
            raise ImportError(f"no module named {module}")
        return real(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)


def test_word_without_the_library_suggests_what_it_can_do(tmp_path, monkeypatch):
    missing(monkeypatch, "docx")
    result = write_document(tmp_path, "report.docx", "text")
    assert result.ok is False
    assert "python-docx isn't installed" in result.detail
    assert "markdown" in result.detail          # offers the alternative


def test_excel_without_the_library_suggests_csv(tmp_path, monkeypatch):
    missing(monkeypatch, "openpyxl")
    result = write_spreadsheet(tmp_path, "book.xlsx", [["a", 1]])
    assert result.ok is False and "CSV" in result.detail


def test_powerpoint_without_the_library_suggests_markdown(tmp_path, monkeypatch):
    missing(monkeypatch, "pptx")
    result = write_presentation(tmp_path, "deck.pptx", [{"title": "x"}])
    assert result.ok is False and "markdown" in result.detail


# ── reading back ────────────────────────────────────────────────────────────
def test_a_document_reads_back(tmp_path):
    write_document(tmp_path, "notes.md", "what was said")
    assert "what was said" in read_document(tmp_path / "notes.md")


def test_a_missing_document_says_so(tmp_path):
    assert "no file at" in read_document(tmp_path / "ghost.md")


def test_a_binary_format_says_it_cannot_be_read_back(tmp_path):
    (tmp_path / "report.docx").write_bytes(b"PK\x03\x04")
    assert "not read them back" in read_document(tmp_path / "report.docx")


def test_listing_shows_documents_newest_first(tmp_path):
    import os
    import time

    write_document(tmp_path, "old.md", "a")
    write_document(tmp_path, "new.md", "b")
    os.utime(tmp_path / "new.md", (time.time() + 100, time.time() + 100))
    (tmp_path / "ignored.bin").write_bytes(b"x")
    listed = list_documents(tmp_path)
    assert listed[0] == "new.md"
    assert "ignored.bin" not in listed


def test_listing_a_missing_folder_is_empty(tmp_path):
    assert list_documents(tmp_path / "nope") == []


def test_the_spoken_result_names_the_file(tmp_path):
    assert write_document(tmp_path, "notes.md", "x").spoken() == "Saved as notes.md."
