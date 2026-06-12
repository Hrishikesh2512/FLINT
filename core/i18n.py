"""Language support for FLINT.

The first-run overlay lets the user pick a language; the chosen code is saved
to config/api_keys.json under "language" and turned into a system-prompt block
by directive() so the Gemini Live model always replies in that language.

Scope: English + the major Indian languages, matching FLINT's voice persona
(a young Indian woman, fluently multilingual in Indian languages).
"""

from __future__ import annotations

# (code, English name, native name)
LANGUAGES: list[tuple[str, str, str]] = [
    ("en",       "English",   "English"),
    ("hi",       "Hindi",     "हिन्दी"),
    ("hinglish", "Hinglish",  "Hinglish"),
    ("ta",       "Tamil",     "தமிழ்"),
    ("te",       "Telugu",    "తెలుగు"),
    ("bn",       "Bengali",   "বাংলা"),
    ("mr",       "Marathi",   "मराठी"),
    ("gu",       "Gujarati",  "ગુજરાતી"),
    ("pa",       "Punjabi",   "ਪੰਜਾਬੀ"),
    ("kn",       "Kannada",   "ಕನ್ನಡ"),
    ("ml",       "Malayalam", "മലയാളം"),
]

_BY_CODE = {code: (eng, native) for code, eng, native in LANGUAGES}

DEFAULT_CODE = "en"


def is_valid(code: str) -> bool:
    return code in _BY_CODE


def normalize(code: str | None) -> str:
    code = (code or "").strip().lower()
    return code if code in _BY_CODE else DEFAULT_CODE


def english_name(code: str) -> str:
    return _BY_CODE.get(normalize(code), _BY_CODE[DEFAULT_CODE])[0]


def label(code: str) -> str:
    """e.g. 'Hindi (हिन्दी)' — for menus."""
    eng, native = _BY_CODE.get(normalize(code), _BY_CODE[DEFAULT_CODE])
    return eng if eng == native else f"{eng} ({native})"


def directive(code: str) -> str:
    """A system-prompt block instructing FLINT which language to reply in."""
    code = normalize(code)
    name = english_name(code)

    if code == "en":
        body = ("Always reply in English (with your natural Indian accent). "
                "If the user clearly switches to another language, you may "
                "follow them, but English is the default.")
    elif code == "hinglish":
        body = ("Always reply in Hinglish — a natural, conversational mix of "
                "Hindi and English, the way young Indians actually speak. Do "
                "not switch to pure English or pure Hindi unless the user does.")
    else:
        body = (f"Always reply in {name} with a natural, native accent, "
                f"regardless of which language the user types or speaks in, "
                f"unless the user explicitly asks you to switch. Keep widely "
                f"used technical terms in English where that sounds natural.")

    return f"[LANGUAGE]\nThe user has selected {label(code)} as their language. {body}\n"
