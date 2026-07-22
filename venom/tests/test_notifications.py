"""Notification hub: local ingest, dedupe, DND-gated chime, on-demand read."""

from venom.config import VenomConfig
from venom.notifications import NotificationHub
from venom.tools_pi import TimerBoard, build_pi_registry


class _DummyMem:
    def render_for_prompt(self) -> str:
        return ""


def _hub(dnd=False):
    h = NotificationHub(is_dnd=lambda: dnd)
    h._chime_path = None  # never actually play audio in tests
    return h


def _msg(mid, title, message):
    import json
    return json.dumps({"id": mid, "title": title, "message": message})


def test_enabled_flag():
    assert not NotificationHub(enabled=False).enabled
    assert NotificationHub(enabled=True).enabled
    assert NotificationHub().enabled  # local ingest is on by default


def test_arrival_is_stored_and_read_once():
    h = _hub()
    h.ingest("Amit", "chai?", "1")
    h.ingest("Mom", "call me", "2")
    out = h.read_unread()
    assert "2 new WhatsApp" in out and "Amit says: chai?" in out and "Mom says: call me" in out
    # Reading marks them seen — the next read is empty.
    assert h.read_unread() == "No new notifications."


def test_ingest_json_from_bridge_payload():
    h = _hub()
    assert h.ingest_json(_msg("j1", "Ravi", "yo")) is True
    assert h.ingest_json("not json") is False
    assert "Ravi says: yo" in h.read_unread()


def test_duplicate_ids_are_ignored():
    h = _hub()
    h.ingest("Amit", "hi", "dup")
    h.ingest("Amit", "hi", "dup")  # a bridge retry replayed it
    assert "one new WhatsApp" in h.read_unread()


def test_empty_message_skipped():
    h = _hub()
    h.ingest("Amit", "", "e1")
    assert h.read_unread() == "No new notifications."


def test_chime_suppressed_during_dnd(monkeypatch):
    played = []
    for dnd in (False, True):
        h = _hub(dnd=dnd)
        h._chime_path = "x"  # pretend a chime exists
        monkeypatch.setattr(h, "_chime", lambda: played.append(True))
        h.ingest("A", "hi", f"m{dnd}")
    assert played == [True]  # only the non-DND arrival chimed


def test_tool_registered_only_when_enabled():
    off = build_pi_registry(VenomConfig(), _DummyMem(), TimerBoard(),
                            notifications=NotificationHub(enabled=False))
    assert "read_notifications" not in off.names()

    on = build_pi_registry(VenomConfig(), _DummyMem(), TimerBoard(),
                           notifications=_hub())
    assert "read_notifications" in on.names()
    assert on.dispatch("read_notifications", {}) == "No new notifications."
