"""His screen as context: what the assistant overlay hands the brain."""

from carnage.livetext import LiveConversation
from carnage.screen import MAX_TEXT, screen_context


def test_text_and_app_make_the_context():
    ctx = screen_context("WhatsApp", "Ma\nAre you coming for dinner?")
    assert "App: WhatsApp" in ctx
    assert "Are you coming for dinner?" in ctx
    assert "this screen" in ctx            # she is told what "this" means


def test_nothing_on_screen_is_no_context():
    assert screen_context("Chrome", "  \n\n ") == ""
    assert screen_context("", "", "") == ""


def test_screenshot_is_described_once_with_room_to_say_it():
    calls = []

    def describe(prompt, jpeg, system, max_tokens=300):
        calls.append((jpeg, max_tokens))
        return "A red running shoe on a product page, ₹4,999."

    ctx = screen_context("Myntra", "", "abc123", describe)
    assert calls == [("abc123", 700)]
    assert "red running shoe" in ctx


def test_a_failed_description_falls_back_to_the_text():
    def describe(*_, **__):
        raise RuntimeError("network")

    ctx = screen_context("Gmail", "Your OTP is 4411", "abc", describe)
    assert "Your OTP is 4411" in ctx and "What the screen shows" not in ctx


def test_repeats_blank_lines_and_length_are_tidied():
    ctx = screen_context("X", "a\n\na\nb\n" + "z" * (MAX_TEXT * 2))
    assert "a\nb" in ctx
    assert "…(cut)" in ctx
    assert len(ctx) < MAX_TEXT + 1000


class Model:
    """The voice model, as far as a typed conversation can tell."""

    def __init__(self):
        self.asked = []

    def run(self, turns, **_):
        self.asked.append(turns)
        return "That's Ma asking about dinner."


class Registry:
    def gemini_declarations(self, uppercase_types=False):
        return []

    def __contains__(self, name):
        return False


def test_the_screen_rides_along_with_the_turn_but_is_not_remembered():
    model = Model()
    talk = LiveConversation(model, Registry(), lambda: "persona")
    ctx = screen_context("WhatsApp", "Ma: dinner?")

    assert talk.ask("what's this?", ctx) == "That's Ma asking about dinner."
    parts = model.asked[0][-1]["parts"]
    assert parts[0]["text"].startswith("[WHAT IS ON HIS SCREEN")
    assert parts[-1]["text"] == "what's this?"
    assert all("Ma: dinner?" not in turn["text"] for turn in talk.history)

    talk.ask("thanks")
    earlier = model.asked[1][:-1]
    assert [t["role"] for t in earlier] == ["user", "model"]
    assert all("WHAT IS ON HIS SCREEN" not in p["text"]
               for t in model.asked[1] for p in t["parts"])
