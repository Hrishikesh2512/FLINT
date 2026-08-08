"""Learning from what happened — and refusing to guess when it hasn't."""

from __future__ import annotations

import pytest

from flint_core.outcomes import MIN_EVIDENCE, OutcomeLog


@pytest.fixture()
def outcomes(tmp_path, fake_clock):
    return OutcomeLog(tmp_path / "outcomes.jsonl", clock=fake_clock)


def decide(log, times, what="which agent", chose="claude", **kw):
    for _ in range(times):
        log.record_decision(what, chose, reason="it is best at code", **kw)


def ran(log, times, action="send_whatsapp", ok=True, **kw):
    for _ in range(times):
        log.record_outcome(action, ok=ok, **kw)


# ── reliability ─────────────────────────────────────────────────────────────
def test_success_rates_are_counted_from_real_runs(outcomes):
    ran(outcomes, 8, "play_music", ok=True)
    ran(outcomes, 2, "play_music", ok=False)
    assert outcomes.reliability()["play_music"] == pytest.approx(0.8)


def test_an_action_seen_too_rarely_is_not_judged(outcomes):
    """Two failures is not evidence that something is broken."""
    ran(outcomes, 2, "take_photo", ok=False)
    assert "take_photo" not in outcomes.reliability()


def test_something_that_fails_more_than_it_works_is_flagged(outcomes):
    ran(outcomes, 7, "look_at_screen", ok=False)
    ran(outcomes, 1, "look_at_screen", ok=True)
    assert outcomes.unreliable() == ["look_at_screen"]


def test_a_reliable_action_is_not_flagged(outcomes):
    ran(outcomes, 10, "play_music", ok=True)
    assert outcomes.unreliable() == []


def test_typical_duration_uses_the_median(outcomes):
    """One ten-minute outlier must not become the expectation."""
    for seconds in (2, 3, 3, 4, 600):
        outcomes.record_outcome("web_search", ok=True, seconds=seconds)
    assert outcomes.typical_seconds("web_search") == 3


def test_duration_needs_a_few_samples(outcomes):
    outcomes.record_outcome("rare", ok=True, seconds=5)
    assert outcomes.typical_seconds("rare") == 0.0


# ── preferences ─────────────────────────────────────────────────────────────
def test_a_settled_habit_becomes_a_preference(outcomes):
    decide(outcomes, 9, "which agent", "claude")
    decide(outcomes, 1, "which agent", "codex")
    assert outcomes.preferences()["which agent"] == "claude"


def test_a_split_decision_is_not_a_preference(outcomes):
    """Half the time is not a habit, however many times you saw it."""
    decide(outcomes, 10, "which agent", "claude")
    decide(outcomes, 10, "which agent", "codex")
    assert "which agent" not in outcomes.preferences()


def test_too_little_evidence_yields_no_preference(outcomes):
    decide(outcomes, MIN_EVIDENCE - 1, "which agent", "claude")
    assert outcomes.preferences() == {}


def test_advice_is_empty_until_something_is_actually_known(outcomes):
    assert outcomes.advice() == []
    assert outcomes.render_for_prompt() == ""


def test_advice_reports_habits_and_broken_tools(outcomes):
    decide(outcomes, 8, "which agent", "claude")
    ran(outcomes, 8, "look_at_screen", ok=False)
    advice = outcomes.advice()
    assert any("almost always wants claude" in a for a in advice)
    assert any("look_at_screen has failed more often" in a for a in advice)


def test_the_prompt_block_tells_her_not_to_recite_it(outcomes):
    decide(outcomes, 8)
    rendered = outcomes.render_for_prompt()
    assert "WHAT YOU'VE LEARNED" in rendered
    assert "never recite" in rendered


# ── explainability ──────────────────────────────────────────────────────────
def test_the_recorded_reason_is_what_comes_back(outcomes):
    outcomes.record_decision("which model", "gemini-2.5-flash",
                             reason="the reasoning model was rate limited",
                             alternatives=["llama-3.3-70b"])
    explained = outcomes.explain("which model")
    assert "the reasoning model was rate limited" in explained
    assert "over llama-3.3-70b" in explained


def test_nothing_recorded_means_saying_so_not_inventing(outcomes):
    """A confabulated explanation is worse than admitting there isn't one."""
    assert "didn't record a reason" in outcomes.explain("which agent")
    assert "haven't recorded any decisions" in outcomes.explain()


def test_a_decision_without_a_reason_is_not_stored(outcomes):
    outcomes.record_decision("which agent", "claude", reason="  ")
    assert outcomes.rows("decision") == []


def test_explain_returns_the_most_recent_choices(outcomes):
    for i in range(5):
        outcomes.record_decision("which agent", f"agent{i}", reason=f"reason{i}")
    explained = outcomes.explain("which agent", limit=2)
    assert "agent4" in explained and "agent3" in explained
    assert "agent0" not in explained


# ── storage ─────────────────────────────────────────────────────────────────
def test_records_survive_a_restart(tmp_path, fake_clock):
    path = tmp_path / "outcomes.jsonl"
    first = OutcomeLog(path, clock=fake_clock)
    ran(first, 6, "play_music", ok=True)

    second = OutcomeLog(path, clock=fake_clock)
    assert second.reliability()["play_music"] == 1.0


def test_a_torn_line_does_not_break_the_log(tmp_path, fake_clock):
    path = tmp_path / "outcomes.jsonl"
    log = OutcomeLog(path, clock=fake_clock)
    log.record_outcome("first", ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "outcome", "action": "tor\n')
    log.record_outcome("second", ok=True)
    assert [r["action"] for r in log.rows("outcome")] == ["first", "second"]


def test_it_works_with_no_file_at_all(fake_clock):
    log = OutcomeLog(None, clock=fake_clock)
    ran(log, 6, "play_music", ok=True)
    assert log.reliability()["play_music"] == 1.0


def test_an_unwritable_log_never_stops_the_work(tmp_path, fake_clock):
    log = OutcomeLog(tmp_path / "outcomes.jsonl", clock=fake_clock)
    log._path = tmp_path                      # a directory: writing must fail
    log.record_outcome("play_music", ok=True)  # must not raise


def test_blank_actions_are_ignored(outcomes):
    outcomes.record_outcome("  ", ok=True)
    assert outcomes.rows("outcome") == []


def test_decisions_and_outcomes_do_not_mix(outcomes):
    outcomes.record_decision("which agent", "claude", reason="best at code")
    outcomes.record_outcome("play_music", ok=True)
    assert len(outcomes.rows("decision")) == 1
    assert len(outcomes.rows("outcome")) == 1
