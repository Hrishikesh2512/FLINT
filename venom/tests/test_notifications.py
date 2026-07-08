"""Phone-notification hub: parsing, dedupe, DND-gated chime, on-demand read."""

from venom.config import VenomConfig
from venom.notifications import NotificationHub
from venom.tools_pi import TimerBoard, build_pi_registry


class _DummyMem:
    def render_for_prompt(self) -> str:
        return ""


def _hub(dnd=False):
    h = NotificationHub("https://ntfy.sh", "venom-notifs-test", is_dnd=lambda: dnd)
    h._chime_path = None  # never actually play audio in tests
    return h


def _msg(mid, title, message):
    import json
    return json.dumps({"id": mid, "event": "message", "title": title,
                       "message": message})


def test_disabled_without_topic():
    assert not NotificationHub("https://ntfy.sh", "").enabled
    assert NotificationHub("https://ntfy.sh", "x").enabled


def test_arrival_is_stored_and_read_once():
    h = _hub()
    h._handle(_msg("1", "Amit", "chai?"))
    h._handle(_msg("2", "Mom", "call me"))
    out = h.read_unread()
    assert "2 new WhatsApp" in out and "Amit says: chai?" in out and "Mom says: call me" in out
    # Reading marks them seen — the next read is empty.
    assert h.read_unread() == "No new notifications."


def test_duplicate_ids_are_ignored():
    h = _hub()
    h._handle(_msg("dup", "Amit", "hi"))
    h._handle(_msg("dup", "Amit", "hi"))  # ntfy replayed on reconnect
    assert "one new WhatsApp" in h.read_unread()


def test_non_message_events_skipped():
    import json
    h = _hub()
    h._handle(json.dumps({"event": "open"}))
    h._handle(json.dumps({"event": "keepalive"}))
    assert h.read_unread() == "No new notifications."


def test_chime_suppressed_during_dnd(monkeypatch):
    played = []
    for dnd in (False, True):
        h = _hub(dnd=dnd)
        h._chime_path = "x"  # pretend a chime exists
        monkeypatch.setattr(h, "_chime", lambda: played.append(True))
        h._handle(_msg(f"m{dnd}", "A", "hi"))
    assert played == [True]  # only the non-DND arrival chimed


def test_tool_registered_only_when_enabled():
    off = build_pi_registry(VenomConfig(), _DummyMem(), TimerBoard(),
                            notifications=NotificationHub("s", ""))
    assert "read_notifications" not in off.names()

    on = build_pi_registry(VenomConfig(), _DummyMem(), TimerBoard(),
                           notifications=_hub())
    assert "read_notifications" in on.names()
    assert on.dispatch("read_notifications", {}) == "No new notifications."
