"""Tests for core.i18n — language normalization and prompt directives."""

from core import i18n


def test_normalize_known_codes():
    assert i18n.normalize("hi") == "hi"
    assert i18n.normalize("  HINGLISH ") == "hinglish"


def test_normalize_unknown_falls_back_to_english():
    assert i18n.normalize("klingon") == "en"
    assert i18n.normalize(None) == "en"
    assert i18n.normalize("") == "en"


def test_every_language_has_a_directive_and_label():
    for code, eng, _native in i18n.LANGUAGES:
        assert i18n.is_valid(code)
        assert i18n.english_name(code) == eng
        directive = i18n.directive(code)
        assert directive.startswith("[LANGUAGE]")
        assert i18n.label(code) in directive


def test_directive_for_unknown_code_is_english():
    assert "English" in i18n.directive("xx")
