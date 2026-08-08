"""The archive tier: remembering more than fits, and finding it again."""

from __future__ import annotations

import pytest

from flint_core.recall import (
    EPISODE,
    FACT,
    PERSON,
    PROJECT,
    Archive,
    archive_conversation,
    tokenise,
)


@pytest.fixture()
def archive(tmp_path, fake_clock):
    return Archive(tmp_path / "archive.db", clock=fake_clock)


DAY = 86400.0


# ── the ceiling this exists to remove ───────────────────────────────────────
def test_it_holds_far_more_than_the_prompt_tier(archive):
    """MemoryStore caps at 2200 characters. This does not."""
    for i in range(500):
        archive.remember(f"detail number {i} about something specific", FACT)
    assert len(archive) == 500
    found = archive.search("detail number 437")
    assert found and "437" in found[0].text


def test_something_filed_long_ago_is_still_findable(archive, fake_clock):
    archive.remember("Rahul's wedding is in Jaipur in December", FACT)
    fake_clock.advance(300 * DAY)
    found = archive.search("where is Rahul's wedding")
    assert found and "Jaipur" in found[0].text


# ── retrieval quality ───────────────────────────────────────────────────────
def test_a_rare_word_decides_the_match(archive):
    """"Rahul" should win it, not "wedding" appearing in everything."""
    for name in ("Priya", "Amit", "Sneha", "Vikram"):
        archive.remember(f"{name}'s wedding is coming up", FACT)
    archive.remember("Rahul's wedding is in Jaipur", FACT)
    found = archive.search("Rahul wedding")
    assert "Rahul" in found[0].text


def test_nothing_relevant_returns_nothing(archive):
    """A recall padded with the closest available entry teaches her to
    bring up things with no bearing on the question."""
    archive.remember("he prefers window seats", FACT)
    assert archive.search("quantum chromodynamics") == []
    assert archive.render_for_prompt("quantum chromodynamics") == ""


def test_an_empty_query_matches_nothing(archive):
    archive.remember("something", FACT)
    assert archive.search("") == []
    assert archive.search("the and of") == []       # stopwords only


def test_recency_nudges_but_does_not_decide(archive, fake_clock):
    archive.remember("the parser project uses recursive descent", PROJECT)
    fake_clock.advance(60 * DAY)
    archive.remember("bought milk today", EPISODE)
    found = archive.search("parser project")
    assert "recursive descent" in found[0].text


def test_between_two_equal_matches_the_recent_one_wins(archive, fake_clock):
    archive.remember("the venom project needs a kernel", PROJECT)
    fake_clock.advance(80 * DAY)
    archive.remember("the venom project needs a kernel", PROJECT)
    found = archive.search("venom kernel")
    assert found[0].ts > found[1].ts


def test_results_are_capped(archive):
    for i in range(30):
        archive.remember(f"venom note {i}", PROJECT)
    assert len(archive.search("venom", limit=5)) == 5


def test_search_can_be_limited_to_one_kind(archive):
    archive.remember("Rahul works at Infosys", PERSON)
    archive.remember("Rahul mentioned the deadline", EPISODE)
    found = archive.search("Rahul", kind=PERSON)
    assert len(found) == 1 and found[0].kind == PERSON


# ── the prompt block ────────────────────────────────────────────────────────
def test_the_prompt_block_dates_each_memory(archive, fake_clock):
    archive.remember("he switched to the new laptop", EPISODE)
    fake_clock.advance(3 * DAY)
    rendered = archive.render_for_prompt("laptop")
    assert "3 days ago" in rendered
    assert "new laptop" in rendered


def test_the_prompt_block_tells_her_it_may_not_fit(archive):
    archive.remember("he likes window seats", FACT)
    rendered = archive.render_for_prompt("window seat")
    assert "ignore them if they don't" in rendered


@pytest.mark.parametrize("days,expected", [
    (0, "today"), (1, "yesterday"), (5, "5 days ago"),
    (45, "1 month ago"), (200, "6 months ago"), (400, "1 year ago"),
])
def test_ages_are_described_the_way_people_say_them(archive, fake_clock,
                                                    days, expected):
    archive.remember("a thing happened", EPISODE)
    fake_clock.advance(days * DAY)
    assert archive.search("thing happened")[0].when(fake_clock.now) == expected


# ── writing and forgetting ──────────────────────────────────────────────────
def test_blank_entries_are_ignored(archive):
    assert archive.remember("   ") is None
    assert len(archive) == 0


def test_an_unknown_kind_becomes_a_fact(archive):
    archive.remember("something", kind="nonsense")
    assert archive.recent()[0].kind == FACT


def test_forgetting_one_entry(archive):
    entry_id = archive.remember("something embarrassing", EPISODE)
    assert archive.forget(entry_id) is True
    assert len(archive) == 0
    assert archive.forget(entry_id) is False


def test_forgetting_everything_about_something(archive):
    archive.remember("Rahul owes me money", FACT)
    archive.remember("Rahul is bad at chess", PERSON)
    archive.remember("Priya likes chess", PERSON)
    assert archive.forget_matching("Rahul") == 2
    assert len(archive) == 1


def test_it_survives_a_restart(tmp_path, fake_clock):
    path = tmp_path / "archive.db"
    first = Archive(path, clock=fake_clock)
    first.remember("the kernel landed on Tuesday", PROJECT)
    first.close()

    second = Archive(path, clock=fake_clock)
    assert second.search("kernel")[0].text == "the kernel landed on Tuesday"


def test_summary_counts_what_is_filed(archive):
    archive.remember("a", FACT)
    archive.remember("b", FACT)
    archive.remember("c", EPISODE)
    summary = archive.summary()
    assert "2 facts" in summary and "1 episode" in summary


def test_summary_when_empty(archive):
    assert "haven't got anything filed" in archive.summary()


# ── archiving conversations ─────────────────────────────────────────────────
def test_a_conversation_is_filed_and_findable(archive):
    turns = [{"who": "you", "text": "the deploy failed again"},
             {"who": "jarvis", "text": "it was the missing env var"}]
    archive_conversation(archive, turns, subject="the deploy")
    found = archive.search("deploy failed")
    assert found and found[0].kind == EPISODE


def test_a_summariser_is_used_when_it_works(archive):
    turns = [{"who": "you", "text": "lots and lots of words about the parser"}]
    archive_conversation(archive, turns, summarise=lambda text: "Talked about the parser.")
    assert archive.recent()[0].text == "Talked about the parser."


def test_a_broken_summariser_falls_back_to_the_transcript(archive):
    """A rate-limited summariser must not silently lose the conversation."""
    def explode(text):
        raise RuntimeError("rate limited")

    turns = [{"who": "you", "text": "something worth keeping"}]
    archive_conversation(archive, turns, summarise=explode)
    assert "something worth keeping" in archive.recent()[0].text


def test_tool_calls_are_not_filed_as_conversation(archive):
    turns = [{"who": "action", "text": "play_music(query=x) -> ok"},
             {"who": "you", "text": "nice one"}]
    archive_conversation(archive, turns)
    assert "play_music" not in archive.recent()[0].text


def test_an_empty_conversation_is_not_filed(archive):
    assert archive_conversation(archive, []) is None
    assert len(archive) == 0


# ── tokenising ──────────────────────────────────────────────────────────────
def test_stopwords_are_dropped_including_hinglish():
    assert tokenise("what is the kya hai scene") == ["scene"]


def test_possessives_fold_onto_their_root():
    """Regression: "Rahul's wedding" filed under `rahul's` was unfindable by
    "Rahul" — and possessives are most of how people refer to people."""
    assert tokenise("Rahul's") == ["rahul"]
    assert tokenise("my mom's birthday") == ["mom", "birthday"]


def test_numbers_are_kept():
    assert "2026" in tokenise("in 2026")


def test_searching_by_a_name_finds_the_possessive(archive):
    archive.remember("Rahul's wedding is in Jaipur", FACT)
    assert archive.search("Rahul")[0].text.startswith("Rahul's")
