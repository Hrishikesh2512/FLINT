"""Calendar (secret iCal) + Gmail (IMAP) — offline: injected feeds/clients."""

import datetime as dt

from venom.config import CalendarConfig, MailConfig, VenomConfig, load_config
from venom.gcal import CalendarFeed, CalendarWatcher, parse_events
from venom.gmail import Mailbox, _decode, _sender_name

TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))  # IST, like the Pi
NOW = dt.datetime(2026, 7, 12, 9, 0, tzinfo=TZ)

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:one@test
DTSTART;TZID=Asia/Kolkata:20260712T110000
DTEND;TZID=Asia/Kolkata:20260712T120000
SUMMARY:Physics seminar
END:VEVENT
BEGIN:VEVENT
UID:daily@test
DTSTART;TZID=Asia/Kolkata:20260710T180000
DTEND;TZID=Asia/Kolkata:20260710T183000
RRULE:FREQ=DAILY;COUNT=10
SUMMARY:Evening walk
END:VEVENT
END:VCALENDAR
"""


def make_feed(ics=ICS):
    feed = CalendarFeed("https://example/secret.ics",
                        fetch=lambda _url: ics, now=lambda: NOW)
    feed.refresh()
    return feed


def test_parse_expands_recurrences():
    events = parse_events(ICS, NOW - dt.timedelta(days=1),
                          NOW + dt.timedelta(days=3))
    walks = [e for e in events if e.summary == "Evening walk"]
    assert len(walks) >= 3  # daily RRULE expanded, not just the first
    assert any(e.summary == "Physics seminar" for e in events)


def test_agenda_today_and_tomorrow():
    feed = make_feed()
    today = feed.agenda("today")
    assert "Physics seminar" in today and "11:00 AM" in today
    assert "Evening walk" in today
    assert "Evening walk" in feed.agenda("tomorrow")
    assert "Physics seminar" not in feed.agenda("tomorrow")


def test_next_event_reports_gap():
    feed = make_feed()
    nxt = feed.next_event()
    assert "Physics seminar" in nxt and "in 2h 00m" in nxt


def test_feed_failure_degrades_gracefully():
    def boom(_url):
        raise OSError("offline")

    feed = CalendarFeed("https://example/secret.ics", fetch=boom,
                        now=lambda: NOW)
    feed.refresh()
    assert "couldn't load" in feed.agenda()
    assert "couldn't load" in feed.next_event()


def test_watcher_announces_each_event_once():
    feed = make_feed()
    clock = {"now": dt.datetime(2026, 7, 12, 10, 35, tzinfo=TZ)}
    watch = CalendarWatcher(feed, lead_minutes=30,
                            now=lambda: clock["now"])
    due = watch.pop_due()  # 25 min before the seminar → inside the lead
    assert len(due) == 1 and "Physics seminar" in due[0] and "25 minutes" in due[0]
    assert watch.pop_due() == []          # announced once, never again
    clock["now"] += dt.timedelta(minutes=5)
    assert watch.pop_due() == []


# ── gmail ─────────────────────────────────────────────────────────────────────
UNREAD_HEADERS = (
    b"From: Placement Cell <placements@college.edu>\r\n"
    b"Subject: =?UTF-8?B?SW50ZXJ2aWV3IHNsb3Q=?=\r\n\r\n"
)
FULL_MESSAGE = (
    b"From: Rohan <rohan@gmail.com>\r\n"
    b"Subject: Trip plan\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Bhai check this https://maps.example/xyz and confirm\r\n\r\n\r\n"
    b"by tonight.\r\n"
)


class FakeIMAP:
    def __init__(self, unseen=b"1 2", message=FULL_MESSAGE):
        self.unseen = unseen
        self.message = message
        self.calls = []

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"2"]

    def search(self, _charset, query):
        self.calls.append(("search", query))
        return "OK", [self.unseen]

    def fetch(self, mid, spec):
        self.calls.append(("fetch", mid, spec))
        data = UNREAD_HEADERS if "HEADER" in spec else self.message
        return "OK", [(b"1 (BODY[] {%d}" % len(data), data)]

    def logout(self):
        self.calls.append(("logout",))


def make_mailbox(fake):
    config = MailConfig(address="a@b.c", app_password="x")
    return Mailbox(config, connect=lambda _cfg: fake)


def test_unread_summary_reads_headers_readonly():
    fake = FakeIMAP()
    out = make_mailbox(fake).unread_summary()
    assert "2 unread" in out
    assert "Placement Cell" in out and "Interview slot" in out  # RFC2047 ✓
    assert ("select", "INBOX", True) in fake.calls  # readonly — never mutates
    assert ("logout",) in fake.calls


def test_read_latest_speaks_clean_body():
    out = make_mailbox(FakeIMAP()).read_latest()
    assert "From Rohan" in out and "Trip plan" in out
    assert "(link)" in out and "https://" not in out  # URLs unspeakable
    assert "confirm" in out


def test_read_latest_from_sender_uses_filter():
    fake = FakeIMAP()
    make_mailbox(fake).read_latest("rohan")
    assert ("search", 'FROM "rohan"') in fake.calls


def test_mail_failure_degrades_gracefully():
    def boom(_cfg):
        raise OSError("no network")

    box = Mailbox(MailConfig(address="a@b.c", app_password="x"), connect=boom)
    assert "couldn't check" in box.unread_summary()
    assert "couldn't fetch" in box.read_latest()


def test_header_helpers():
    assert _sender_name("Some Name <x@y.z>") == "Some Name"
    assert _sender_name("plain@addr.com") == "plain"
    assert _decode("=?UTF-8?B?SGVsbG8=?=") == "Hello"


# ── config + registry wiring ──────────────────────────────────────────────────
def test_calendar_mail_config(tmp_path):
    assert VenomConfig().calendar.ready is False
    assert VenomConfig().mail.ready is False

    path = tmp_path / "venom.toml"
    path.write_text(
        '[calendar]\nical_url = "https://x/secret.ics"\nlead_minutes = 20\n'
        '[mail]\naddress = "a@b.c"\napp_password = "ab cd ef"\n')
    config = load_config(path)
    assert config.calendar.ready and config.calendar.lead_minutes == 20
    assert config.mail.ready
    assert config.mail.app_password == "abcdef"  # spaces stripped


def test_tools_registered_only_when_configured(tmp_path):
    from flint_core.memory import MemoryStore
    from venom.tools_pi import TimerBoard, build_pi_registry

    base = dict(gemini_api_key="k", memory_path=tmp_path / "m.json")
    off = build_pi_registry(VenomConfig(**base),
                            MemoryStore(base["memory_path"]), TimerBoard())
    for name in ("calendar_agenda", "next_event", "check_inbox",
                 "read_latest_email"):
        assert name not in off.names()

    class StubWatcher:
        class feed:
            @staticmethod
            def agenda(day="today"):
                return "agenda"

            @staticmethod
            def next_event():
                return "next"

    on = build_pi_registry(VenomConfig(**base),
                           MemoryStore(base["memory_path"]), TimerBoard(),
                           calendar=StubWatcher(),
                           mailbox=make_mailbox(FakeIMAP()))
    assert on.dispatch("calendar_agenda", {"day": "today"}) == "agenda"
    assert on.dispatch("next_event", {}) == "next"
    assert "unread" in on.dispatch("check_inbox", {})
