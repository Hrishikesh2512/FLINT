"""Shared files, Maps, replies, and using his other apps."""

from carnage.attachments import attachment_context
from carnage.hands import register_hands_tools
from carnage.tools_extras import register_navigation_tools, register_reply_tools


class Registry:
    """Just enough of the tool registry: keeps each tool by name."""

    def __init__(self):
        self.tools = {}

    def tool(self, **_):
        def keep(fn):
            self.tools[fn.__name__] = fn
            return fn
        return keep


# ── attachments ─────────────────────────────────────────────────────────────
def test_shared_text_goes_in_as_it_is():
    ctx = attachment_context([{"name": "message", "text": "Meet at 7, Gate 3"}])
    assert "Meet at 7, Gate 3" in ctx and "WHAT HE SHARED" in ctx


def test_a_pdf_is_read_with_his_question_and_its_type():
    seen = {}

    def describe(prompt, data, system, max_tokens=300, mime="image/jpeg"):
        seen.update(prompt=prompt, mime=mime, budget=max_tokens)
        return "An electricity bill for ₹2,140, due 20 October."

    ctx = attachment_context(
        [{"name": "bill.pdf", "mime": "application/pdf", "data": "JVBERi0x"}],
        "when is this due?", describe)
    assert seen["mime"] == "application/pdf" and seen["budget"] == 1500
    assert "when is this due?" in seen["prompt"]
    assert "due 20 October" in ctx


def test_an_unreadable_file_is_named_not_dropped():
    def describe(*_, **__):
        raise RuntimeError("quota")

    ctx = attachment_context([{"name": "a.jpg", "mime": "image/jpeg", "data": "abc"}],
                             "", describe)
    assert "a.jpg" in ctx and "failed" in ctx


def test_nothing_shared_is_no_context():
    assert attachment_context([]) == ""
    assert attachment_context([{"name": "x", "text": "  "}]) == ""


# ── maps and replies ───────────────────────────────────────────────────────
class Phone:
    def __init__(self):
        self.calls = []

    def navigate(self, destination, mode):
        self.calls.append(("navigate", destination, mode))
        return True

    def reply_to(self, who, text):
        self.calls.append(("reply", who, text))
        if who == "Rahul":
            return {"status": "sent", "to": "Rahul Verma", "app": "WhatsApp"}
        if who == "Bank":
            return {"status": "no_reply_box", "app": "HDFC"}
        return {"status": "not_found"}


def test_navigation_hands_the_place_and_mode_to_maps():
    reg, phone = Registry(), Phone()
    register_navigation_tools(reg, phone)
    assert "Navigating to Phoenix Mall" in reg.tools["navigate"]("Phoenix Mall", "two-wheeler")
    assert phone.calls == [("navigate", "Phoenix Mall", "l")]


def test_replies_say_who_was_answered_and_why_not():
    reg, phone = Registry(), Phone()
    register_reply_tools(reg, phone)
    reply = reg.tools["reply_to_message"]
    assert reply("Rahul", "on my way") == "Replied to Rahul Verma on WhatsApp."
    assert "no reply box" in reply("Bank", "ok")
    assert "no message from Neha" in reply("Neha", "hi")


# ── using his other apps ────────────────────────────────────────────────────
class Hands:
    """A phone whose screen is a script of element lists, one per look."""

    def __init__(self, screens):
        self.screens = list(screens)
        self.done = []
        self.begun = self.ended = 0

    def screen_elements(self):
        return {"app": "Swiggy", "elements": self.screens[0]}

    def hands_act(self, action, id=None, text=""):          # noqa: A002
        self.done.append((action, id, text))
        if action == "tap" and len(self.screens) > 1:
            self.screens.pop(0)
        return True

    def hands_begin(self):
        self.begun += 1

    def hands_end(self):
        self.ended += 1


class Brain:
    def __init__(self, moves):
        self.moves = list(moves)
        self.prompts = []

    def decide(self, prompt, system, function):
        assert function["name"] == "move"
        self.prompts.append(prompt)
        return self.moves.pop(0)


MENU = [{"id": 1, "kind": "button", "label": "Cold Coffee"}]
CART = [{"id": 7, "kind": "button", "label": "Place order ₹180"}]


def hands(phone, brain, clock=lambda: 0):
    reg = Registry()
    register_hands_tools(reg, phone, brain.decide, clock=clock, sleep=lambda _: None)
    return reg.tools["use_phone"], reg.tools["use_phone_confirm"]


def test_it_stops_before_a_tap_that_commits_even_if_the_model_forgets():
    phone = Hands([MENU, CART])
    brain = Brain([{"action": "tap", "id": 1},
                   {"action": "tap", "id": 7, "commits": False}])
    use, _ = hands(phone, brain)
    said = use("order a cold coffee on Swiggy")
    assert "Stopped before tapping \"Place order ₹180\"" in said
    assert phone.done == [("tap", 1, "")]             # the order was never placed
    assert phone.begun == phone.ended == 1


def test_a_yes_makes_that_one_tap_and_then_it_finishes():
    phone = Hands([MENU, CART, [{"id": 9, "kind": "text", "label": "Order placed"}]])
    brain = Brain([{"action": "tap", "id": 1},
                   {"action": "tap", "id": 7, "commits": True},
                   {"action": "tap", "id": 7, "commits": True},
                   {"action": "done", "summary": "Ordered, arriving in 25 min."}])
    use, confirm = hands(phone, brain)
    use("order a cold coffee")
    assert confirm() == "Done. Ordered, arriving in 25 min."
    assert phone.done == [("tap", 1, ""), ("tap", 7, "")]


def test_a_yes_covers_only_the_tap_it_was_given_for():
    pay = [{"id": 8, "kind": "button", "label": "Pay now"}]
    phone = Hands([CART, pay])
    brain = Brain([{"action": "tap", "id": 7, "commits": True},
                   {"action": "tap", "id": 7, "commits": True},
                   {"action": "tap", "id": 8, "commits": True}])
    use, confirm = hands(phone, brain)
    use("order")
    assert "\"Pay now\"" in confirm()                 # stops again for the second
    assert phone.done == [("tap", 7, "")]


def test_a_stale_yes_is_refused():
    now = [0]
    phone = Hands([CART])
    use, confirm = hands(phone, Brain([{"action": "tap", "id": 7, "commits": True}]),
                         clock=lambda: now[0])
    use("order")
    now[0] = 600
    assert "start again" in confirm()
    assert phone.done == []


def test_without_the_accessibility_service_it_says_how_to_turn_it_on():
    class Blind:
        def screen_elements(self):
            return None

    use, _ = hands(Blind(), Brain([]))
    assert "Accessibility" in use("open settings")


def test_password_fields_are_marked_never_to_type_into():
    phone = Hands([[{"id": 2, "kind": "text field", "label": "PIN",
                     "editable": True, "password": True}]])
    brain = Brain([{"action": "ask", "question": "What is the PIN?"}])
    use, _ = hands(phone, brain)
    assert "Stopped to ask him" in use("log in")
    assert "never type into it" in brain.prompts[0]


def test_a_file_too_big_to_send_is_said_so():
    ctx = attachment_context([{"name": "scan.pdf", "mime": "application/pdf", "too_big": True}])
    assert "scan.pdf" in ctx and "too big" in ctx
