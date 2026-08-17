"""The device seam: what happens when a phone answers, and when it won't.

The second half matters more. A Pi's camera is wired in or it is not, decided
once at boot; a phone's location permission can be withdrawn while she is
mid-sentence. Every one of these has to degrade to "I can't see that" rather
than raise into the voice loop.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from carnage.platform import (
    AbsentPhone,
    AndroidPhone,
    Battery,
    Fix,
    TermuxPhone,
    detect,
)


class FakeRunner:
    """Stands in for the termux-* binaries."""

    def __init__(self, **replies):
        # name -> (returncode, stdout) or an exception to raise
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, command, timeout):
        self.calls.append(list(command))
        reply = self.replies.get(command[0])
        if isinstance(reply, Exception):
            raise reply
        code, out = reply if reply else (1, "")
        return subprocess.CompletedProcess(command, code, stdout=out, stderr="")


def termux(**replies) -> TermuxPhone:
    return TermuxPhone(runner=FakeRunner(**replies), has=lambda tool: True)


# ── battery ─────────────────────────────────────────────────────────────────
def test_battery_is_read(  ):
    phone = termux(**{"termux-battery-status":
                      (0, json.dumps({"percentage": 42, "status": "DISCHARGING",
                                      "temperature": 31.2}))})
    reading = phone.battery()
    assert reading == Battery(percent=42, charging=False, temperature=31.2)
    assert "42%" in reading.spoken()


def test_charging_is_anything_that_is_not_discharging():
    phone = termux(**{"termux-battery-status":
                      (0, json.dumps({"percentage": 80, "status": "FULL"}))})
    assert phone.battery().charging is True


def test_a_refused_permission_reads_as_no_answer_not_a_crash():
    phone = termux(**{"termux-battery-status": (1, "")})
    assert phone.battery() is None


def test_output_that_is_not_json_is_survived():
    phone = termux(**{"termux-battery-status": (0, "<html>permission denied")})
    assert phone.battery() is None


def test_a_missing_binary_is_survived():
    phone = termux(**{"termux-battery-status": FileNotFoundError("no such tool")})
    assert phone.battery() is None


# ── location ────────────────────────────────────────────────────────────────
def test_gps_is_preferred():
    phone = termux(**{"termux-location":
                      (0, json.dumps({"latitude": 18.52, "longitude": 73.85,
                                      "accuracy": 8.0}))})
    fix = phone.locate()
    assert fix.provider == "gps"
    assert not fix.coarse


def test_it_falls_back_to_the_network_indoors():
    """Asking only for GPS is how a wearable goes blind inside a building."""
    runner = FakeRunner()
    calls = {"n": 0}

    def reply(command, timeout):
        calls["n"] += 1
        runner.calls.append(list(command))
        if "gps" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"latitude": 18.5, "longitude": 73.8,
                                           "accuracy": 1200.0}), stderr="")

    phone = TermuxPhone(runner=reply, has=lambda tool: True)
    fix = phone.locate()
    assert fix.provider == "network"
    assert calls["n"] == 2


def test_a_rough_fix_says_so_rather_than_sounding_certain():
    """'You're at home' off a cell-tower fix is wrong for a whole suburb."""
    coarse = Fix(latitude=18.5, longitude=73.8, accuracy=1500.0)
    assert coarse.coarse
    assert "rough" in coarse.spoken()

    precise = Fix(latitude=18.5, longitude=73.8, accuracy=6.0)
    assert "rough" not in precise.spoken()


def test_no_fix_at_all_is_none():
    phone = termux(**{"termux-location": (1, "")})
    assert phone.locate() is None


# ── sms ─────────────────────────────────────────────────────────────────────
def test_a_text_is_sent():
    runner = FakeRunner(**{"termux-sms-send": (0, "")})
    phone = TermuxPhone(runner=runner, has=lambda tool: True)
    assert phone.send_sms("+919812345678", "on my way") is True
    assert runner.calls[0][:3] == ["termux-sms-send", "-n", "+919812345678"]


def test_an_empty_message_is_not_sent():
    runner = FakeRunner(**{"termux-sms-send": (0, "")})
    phone = TermuxPhone(runner=runner, has=lambda tool: True)
    assert phone.send_sms("+91981", "   ") is False
    assert runner.calls == []          # never reached the radio


def test_a_failed_send_reports_failure():
    phone = termux(**{"termux-sms-send": (1, "")})
    assert phone.send_sms("+91981", "hello") is False


# ── the fallback body ───────────────────────────────────────────────────────
def test_a_dev_box_has_no_phone_and_says_so():
    phone = AbsentPhone()
    assert phone.available() is False
    assert phone.battery() is None
    assert phone.locate() is None
    assert phone.send_sms("+91981", "hi") is False
    assert phone.notifications() == []


def test_detect_falls_through_to_absent_on_a_laptop():
    assert isinstance(detect(), AbsentPhone) or detect().available()


def test_a_host_bridge_wins_outright():
    phone = detect(bridge=lambda method, **kw: {"percent": 55})
    assert isinstance(phone, AndroidPhone)
    assert phone.battery().percent == 55


# ── the android bridge ──────────────────────────────────────────────────────
def test_a_java_side_failure_is_not_fatal():
    """Anything can come back across JNI, including things that aren't errors."""
    def explode(method, **kwargs):
        raise RuntimeError("SecurityException: permission denied")

    phone = AndroidPhone(explode)
    assert phone.battery() is None
    assert phone.locate() is None
    assert phone.send_sms("+91981", "hi") is False


def test_the_bridge_is_one_call_not_one_method_per_skill():
    """A method per capability puts new skills behind an app release."""
    seen = []

    def bridge(method, **kwargs):
        seen.append((method, kwargs))
        return {"latitude": 1.0, "longitude": 2.0, "accuracy": 5.0}

    AndroidPhone(bridge).locate()
    assert seen == [("locate", {})]


def test_an_unbridged_android_is_unavailable():
    assert AndroidPhone(None).available() is False


@pytest.mark.parametrize("garbage", [None, "not a dict", 42, []])
def test_rubbish_from_the_bridge_is_not_believed(garbage):
    phone = AndroidPhone(lambda method, **kw: garbage)
    assert phone.battery() is None
    assert phone.locate() is None
