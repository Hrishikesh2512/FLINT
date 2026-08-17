"""The page as a phone body: what it can do, and what it must not claim.

The most important tests here are the ones about SMS. A browser cannot send
one, and the whole design turns on her saying so — a "sent" that means "sitting
in a composer" is a confident false report about the one thing nobody
double-checks until it matters.
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from flint_core.tools import ToolRegistry

from carnage.browserphone import FRESH_SECONDS, BrowserPhone
from carnage.config import CarnageConfig, HubConfig, WebConfig
from carnage.platform import Phone
from carnage.runtime import Carnage
from carnage.tools_phone import register_sms_tools, register_sos_sms

NOW = 1_700_000_000.0


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def phone(clock):
    body = BrowserPhone(clock=clock)
    body.report({"location": {"latitude": 18.52, "longitude": 73.85,
                              "accuracy": 8.0},
                 "battery": {"percent": 74, "charging": False}})
    return body


# ── the seam ────────────────────────────────────────────────────────────────
def test_it_satisfies_the_phone_protocol():
    assert isinstance(BrowserPhone(), Phone)


def test_it_is_a_phone_body_even_with_no_page_open():
    """Capabilities resolve once. Tying them to an open tab would mean the
    phone skills exist or not depending on what was open at start-up."""
    body = BrowserPhone()
    assert body.available() is True
    assert body.connected() is False


def test_readings_are_what_go_quiet_instead(phone, clock):
    assert phone.locate() is not None
    clock.advance(FRESH_SECONDS + 1)
    assert phone.locate() is None
    assert phone.battery() is None
    assert phone.connected() is False


def test_a_reading_survives_a_missed_tick(phone, clock):
    """One dropped post on a bad connection must not blind her."""
    clock.advance(FRESH_SECONDS - 5)
    assert phone.locate() is not None


def test_the_shade_is_never_readable():
    """No web API exposes it, and pretending otherwise would be a lie."""
    assert BrowserPhone().notifications() == []


@pytest.mark.parametrize("rubbish", [
    {}, {"location": "somewhere"}, {"location": {}},
    {"location": {"latitude": "north", "longitude": 1}},
    {"battery": {"percent": "lots"}}, {"battery": None},
])
def test_a_page_cannot_break_her_by_posting_nonsense(rubbish):
    body = BrowserPhone()
    body.report(rubbish)          # must not raise
    assert body.connected() is True


# ── SMS: the honest limit ───────────────────────────────────────────────────
def test_a_browser_never_claims_to_send_directly():
    assert BrowserPhone().sends_directly is False


def test_send_text_says_tap_send_rather_than_sent(phone):
    class Book:
        def phone_for(self, who):
            return "919812345678"

    reg = ToolRegistry()
    register_sms_tools(reg, phone, contacts=Book())
    said = reg.dispatch("send_text", {"to": "Ma", "text": "on my way"})
    assert "tap send" in said.lower()
    assert "sent to" not in said.lower()


def test_the_message_is_queued_for_the_page(phone):
    assert phone.send_sms("919812345678", "on my way") is True
    assert [m["to"] for m in phone.take_outbox()] == ["919812345678"]
    assert phone.take_outbox() == []          # draining is once


def test_nothing_is_queued_when_no_page_is_holding_the_phone(phone, clock):
    """A queue that fills up with nobody to drain it fails silently."""
    clock.advance(FRESH_SECONDS + 1)
    assert phone.send_sms("919812345678", "on my way") is False
    assert phone.take_outbox() == []


def test_an_empty_message_is_not_queued(phone):
    assert phone.send_sms("919812345678", "   ") is False
    assert phone.send_sms("", "hello") is False


# ── SOS: where the wording matters most ─────────────────────────────────────
class Sos:
    def contacts(self, enabled_only=False):
        return [{"name": "Papa", "to": "+9111"}, {"name": "Ananya", "to": "+9122"}]


def test_sos_leads_with_the_fact_that_he_must_press_send(phone):
    reg = ToolRegistry()
    register_sos_sms(reg, phone, Sos())
    said = reg.dispatch("sos_sms", {"note": "bike accident"})

    assert said.lower().startswith("tap send")
    assert "NOT sent" in said
    assert "Papa" in said and "Ananya" in said


def test_sos_still_includes_where_he_is(phone):
    reg = ToolRegistry()
    register_sos_sms(reg, phone, Sos())
    reg.dispatch("sos_sms", {"note": "help"})
    assert "18.52" in phone.take_outbox()[0]["text"]


def test_sos_with_no_page_open_reports_total_failure(phone, clock):
    clock.advance(FRESH_SECONDS + 1)
    reg = ToolRegistry()
    register_sos_sms(reg, phone, Sos())
    said = reg.dispatch("sos_sms", {"note": "help"})
    assert "could not send" in said.lower()
    assert "calling" in said


# ── the server ──────────────────────────────────────────────────────────────
@pytest.fixture()
def served(tmp_path):
    """A real Carnage on a real socket, torn down after."""
    port = 8796
    carnage = Carnage(CarnageConfig(
        device="carnage", user_name="Hrishikesh", state_dir=tmp_path,
        devices=({"name": "venom", "body": "the wearable"},),
        hub=HubConfig(enabled=False),
        web=WebConfig(enabled=True, host="127.0.0.1", port=port,
                      token="secret")),
        phone=BrowserPhone())
    assert carnage.web.start()
    try:
        yield carnage, f"http://127.0.0.1:{port}"
    finally:
        carnage.web.stop()


def fetch(base, path, body=None, token="secret"):
    req = urllib.request.Request(
        base + path, method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def raw(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, r.read(), r.headers.get("Content-Type", "")


@pytest.mark.parametrize("path,kind", [
    ("/", "text/html"),
    ("/app.js", "text/javascript"),
    ("/sw.js", "text/javascript"),
    ("/manifest.webmanifest", "application/manifest+json"),
    ("/icon.svg", "image/svg+xml"),
])
def test_the_shell_is_served_without_a_token(served, path, kind):
    """It has to be: the browser fetches these before it can send a header."""
    _, base = served
    status, body, content_type = raw(base, path)
    assert status == 200
    assert kind in content_type
    assert body


def test_the_api_is_not(served):
    _, base = served
    assert fetch(base, "/api/status", token=None)[0] == 401
    assert fetch(base, "/api/status", token="a-guess")[0] == 401
    assert fetch(base, "/api/report", {"location": None}, token=None)[0] == 401


def test_status_describes_her_and_her_other_bodies(served):
    _, base = served
    status, payload = fetch(base, "/api/status")
    assert status == 200
    assert payload["body"] == "browser"
    assert payload["sends_directly"] is False
    assert payload["tools"] > 0
    assert [d["name"] for d in payload["devices"]] == ["venom"]


def test_the_page_feeds_her_senses_over_the_wire(served):
    carnage, base = served
    status, _ = fetch(base, "/api/report", {
        "location": {"latitude": 18.52043, "longitude": 73.85674,
                     "accuracy": 8.0},
        "battery": {"percent": 74, "charging": False}})
    assert status == 200
    assert "18.52043" in carnage.registry.dispatch("where_am_i", {})
    assert "74%" in carnage.registry.dispatch("phone_battery", {})


def test_a_queued_message_comes_back_to_the_page(served):
    carnage, base = served
    fetch(base, "/api/report", {"location": None})
    carnage.connections.save("Ma", phone="919812345678")
    carnage.registry.dispatch("send_text", {"to": "Ma", "text": "on my way"})

    _, payload = fetch(base, "/api/report", {})
    assert [m["to"] for m in payload["outbox"]] == ["919812345678"]


def test_asking_without_a_key_says_so_rather_than_failing(served):
    _, base = served
    status, payload = fetch(base, "/api/ask", {"text": "hello"})
    assert status == 200
    assert "key" in payload["said"].lower()


def test_an_empty_question_is_not_sent_anywhere(served):
    _, base = served
    _, payload = fetch(base, "/api/ask", {"text": "   "})
    assert payload["ok"] is False


def test_rubbish_does_not_take_the_page_down(served):
    _, base = served
    assert fetch(base, "/api/report", {"location": "somewhere"})[0] == 200
    assert fetch(base, "/api/nonsense", {})[0] == 404
    assert fetch(base, "/api/status")[0] == 200      # still serving


def test_a_missing_file_is_a_404_not_a_traceback(served):
    _, base = served
    assert fetch(base, "/../secrets", token=None)[0] in (400, 404)


def test_the_manifest_makes_it_installable(served):
    """Without display:standalone it opens in a browser tab, not as an app."""
    _, base = served
    _, body, _ = raw(base, "/manifest.webmanifest")
    manifest = json.loads(body)
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["icons"]


def test_the_service_worker_never_caches_her_answers(served):
    """A cached reply is a remembered answer presented as a current one."""
    _, base = served
    _, body, _ = raw(base, "/sw.js")
    source = body.decode()
    assert "/api/" in source
    assert "return" in source.split("/api/")[1][:60]


def test_the_page_never_hardcodes_a_token():
    """It comes from the link and lives in localStorage — never in the source."""
    app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
        encoding="utf-8")
    assert "localStorage" in app
    assert "carnage.token" in app


def test_nothing_is_loaded_from_another_host():
    """A page for a private assistant should not phone anywhere else."""
    web = Path(__file__).resolve().parents[1] / "web"
    for name in ("index.html", "app.js", "sw.js"):
        source = (web / name).read_text(encoding="utf-8")
        assert "http://" not in source.replace("http://www.w3.org", "")
        assert "https://" not in source


def test_a_temporary_directory_is_enough_to_run_one():
    """No installation state, no registry, nothing outside its state dir."""
    root = Path(tempfile.mkdtemp())
    carnage = Carnage(CarnageConfig(state_dir=root, hub=HubConfig(enabled=False)),
                      phone=BrowserPhone())
    assert (root / "memory.json").parent.exists()
    assert carnage.describe()
