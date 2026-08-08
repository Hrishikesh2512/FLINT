"""Projects: dependencies, deadlines, and what can actually start now."""

from __future__ import annotations

import pytest

from flint_core.projects import ProjectError, ProjectStore


@pytest.fixture()
def store(tmp_path, fake_clock):
    return ProjectStore(tmp_path / "projects.json", clock=fake_clock)


HOUR = 3600.0


# ── the questions that earn the module ──────────────────────────────────────
def test_a_task_with_an_unfinished_blocker_is_not_ready(store):
    first = store.add_task("write the parser")
    store.add_task("write the tests", depends_on=[first["id"]])
    assert [t["title"] for t in store.ready()] == ["write the parser"]
    assert [t["title"] for t in store.blocked()] == ["write the tests"]


def test_finishing_a_blocker_unblocks_the_next_thing(store):
    first = store.add_task("write the parser")
    store.add_task("write the tests", depends_on=[first["id"]])
    store.complete(first["id"])
    assert [t["title"] for t in store.ready()] == ["write the tests"]
    assert store.blocked() == []


def test_a_chain_unblocks_one_link_at_a_time(store):
    a = store.add_task("design")
    b = store.add_task("build", depends_on=[a["id"]])
    store.add_task("ship", depends_on=[b["id"]])
    assert len(store.ready()) == 1
    store.complete(a["id"])
    assert [t["title"] for t in store.ready()] == ["build"]


def test_a_task_can_wait_on_several_things(store):
    a = store.add_task("backend")
    b = store.add_task("frontend")
    store.add_task("launch", depends_on=[a["id"], b["id"]])
    store.complete(a["id"])
    assert [t["title"] for t in store.blocked()] == ["launch"]
    store.complete(b["id"])
    assert "launch" in [t["title"] for t in store.ready()]


def test_dependencies_can_be_named_in_words(store):
    """Spoken input is "after the parser is done", not a hex id."""
    store.add_task("write the parser")
    blocked = store.add_task("write the tests", depends_on=["parser"])
    assert len(blocked["depends_on"]) == 1
    assert store.blocked()[0]["title"] == "write the tests"


def test_depending_on_nothing_recognisable_is_an_error(store):
    with pytest.raises(ProjectError, match="nothing here matches"):
        store.add_task("x", depends_on=["a task that does not exist"])


def test_a_dependency_can_be_added_after_the_fact(store):
    """"Actually, the tests can't start until the parser is done." """
    store.add_task("write the parser")
    store.add_task("write the tests")
    store.block_on("tests", "parser")
    assert [t["title"] for t in store.blocked()] == ["write the tests"]


def test_a_dependency_cycle_is_refused(store):
    """Every task in a cycle is permanently unstartable, and the symptom
    ("nothing is ever ready") is miles from the cause."""
    a = store.add_task("a")
    store.add_task("b", depends_on=[a["id"]])
    with pytest.raises(ProjectError, match="circular"):
        store.block_on("a", "b")            # closes the loop


def test_a_refused_cycle_leaves_the_data_untouched(store):
    a = store.add_task("a")
    store.add_task("b", depends_on=[a["id"]])
    with pytest.raises(ProjectError):
        store.block_on("a", "b")
    assert store.ready()[0]["title"] == "a"      # still startable


def test_a_task_cannot_wait_on_itself(store):
    store.add_task("a")
    with pytest.raises(ProjectError, match="wait on itself"):
        store.block_on("a", "a")


def test_blocking_on_something_unknown_is_an_error(store):
    store.add_task("a")
    with pytest.raises(ProjectError, match="nothing here matches"):
        store.block_on("a", "no such thing")


def test_adding_the_same_dependency_twice_is_harmless(store):
    a = store.add_task("a")
    store.add_task("b", depends_on=[a["id"]])
    store.block_on("b", "a")
    assert len(store.blocked()[0]["depends_on"]) == 1


# ── deadlines ───────────────────────────────────────────────────────────────
def test_due_soon_is_ordered_and_bounded(store, fake_clock):
    store.add_task("later", due=fake_clock.now + 100 * HOUR)
    store.add_task("tomorrow", due=fake_clock.now + 20 * HOUR)
    store.add_task("in an hour", due=fake_clock.now + 1 * HOUR)
    assert [t["title"] for t in store.due_soon(48)] == ["in an hour", "tomorrow"]


def test_overdue_work_is_found(store, fake_clock):
    store.add_task("was due yesterday", due=fake_clock.now - HOUR)
    store.add_task("fine", due=fake_clock.now + 100 * HOUR)
    assert [t["title"] for t in store.overdue()] == ["was due yesterday"]


def test_overdue_counts_as_due_soon(store, fake_clock):
    store.add_task("late", due=fake_clock.now - HOUR)
    assert [t["title"] for t in store.due_soon(1)] == ["late"]


def test_dated_work_sorts_before_undated(store, fake_clock):
    store.add_task("someday")
    store.add_task("friday", due=fake_clock.now + 48 * HOUR)
    assert [t["title"] for t in store.tasks()] == ["friday", "someday"]


# ── projects ────────────────────────────────────────────────────────────────
def test_tasks_can_be_filtered_by_project(store):
    store.add_project("venom")
    store.add_task("ship the kernel", project="venom")
    store.add_task("buy milk")
    assert [t["title"] for t in store.tasks("venom")] == ["ship the kernel"]
    assert len(store.tasks()) == 2


def test_adding_a_project_twice_is_idempotent(store):
    store.add_project("venom", goal="an AI OS")
    again = store.add_project("venom")
    assert again["goal"] == "an AI OS"
    assert len(store.projects()) == 1


def test_a_project_needs_a_name(store):
    with pytest.raises(ProjectError, match="needs a name"):
        store.add_project("  ")


def test_a_task_needs_a_title(store):
    with pytest.raises(ProjectError, match="needs a title"):
        store.add_task("")


# ── persistence ─────────────────────────────────────────────────────────────
def test_everything_survives_a_reboot(tmp_path, fake_clock):
    path = tmp_path / "projects.json"
    first = ProjectStore(path, clock=fake_clock)
    a = first.add_task("design")
    first.add_task("build", depends_on=[a["id"]])

    second = ProjectStore(path, clock=fake_clock)
    assert [t["title"] for t in second.blocked()] == ["build"]


def test_a_corrupt_file_reads_as_empty_not_a_crash(tmp_path, fake_clock):
    path = tmp_path / "projects.json"
    path.write_text("{ not json", encoding="utf-8")
    assert ProjectStore(path, clock=fake_clock).tasks() == []


# ── completing and dropping ─────────────────────────────────────────────────
def test_completing_by_words_works(store):
    store.add_task("write the parser")
    assert store.complete("parser")["status"] == "done"
    assert store.tasks() == []


def test_completing_something_unknown_returns_nothing(store):
    assert store.complete("no such task") is None


def test_a_dropped_task_stops_blocking(store):
    a = store.add_task("maybe")
    store.add_task("real work", depends_on=[a["id"]])
    store.drop(a["id"])
    assert [t["title"] for t in store.ready()] == ["real work"]


# ── spoken output ───────────────────────────────────────────────────────────
def test_summary_when_there_is_nothing(store):
    assert "nothing tracked" in store.summary()


def test_summary_leads_with_what_is_late(store, fake_clock):
    store.add_task("late thing", due=fake_clock.now - HOUR)
    store.add_task("ready thing")
    summary = store.summary()
    assert summary.startswith("1 overdue: late thing")
    assert "ready to start" in summary


def test_summary_counts_what_is_waiting(store):
    a = store.add_task("first")
    store.add_task("second", depends_on=[a["id"]])
    assert "1 waiting on something else" in store.summary()


def test_explain_names_the_actual_blocker(store):
    store.add_task("write the parser")
    store.add_task("write the tests", depends_on=["parser"])
    assert store.explain("tests") == (
        "write the tests is waiting on write the parser.")


def test_explain_says_when_nothing_is_blocking(store):
    store.add_task("just do it")
    assert "ready to start" in store.explain("just do it")


def test_explain_of_an_unknown_task(store):
    assert "don't have a task" in store.explain("nonsense")
