"""Context: fresh when it matters, silent when it can't know."""

from __future__ import annotations

from flint_core.context import (
    STALE_MULTIPLE,
    ContextGatherer,
    ContextItem,
    Source,
    build_gatherer,
    git_project_source,
    recent_files_source,
)


def source(name="app", ttl=10.0, values=None, raises=None):
    """A probe returning each queued value in turn, then repeating the last."""
    queue = list(values if values is not None else ["Chrome"])
    calls = []

    def read():
        calls.append(1)
        if raises:
            raise raises
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        return None if value is None else ContextItem(key=name, label="On screen",
                                                      value=value)

    read.calls = calls          # type: ignore[attr-defined]
    return Source(name=name, ttl=ttl, read=read)


# ── reading ─────────────────────────────────────────────────────────────────
def test_what_is_known_reaches_the_prompt(fake_clock):
    gatherer = ContextGatherer([source(values=["VS Code — main.py"])],
                               clock=fake_clock)
    rendered = gatherer.render_for_prompt()
    assert "On screen: VS Code — main.py" in rendered


def test_nothing_known_is_an_empty_block(fake_clock):
    """A block reading "nothing known" spends tokens saying she knows nothing."""
    gatherer = ContextGatherer([source(values=[None])], clock=fake_clock)
    assert gatherer.render_for_prompt() == ""
    assert ContextGatherer([], clock=fake_clock).render_for_prompt() == ""


def test_the_block_tells_her_not_to_read_it_back(fake_clock):
    gatherer = ContextGatherer([source()], clock=fake_clock)
    assert "don't read it back at him" in gatherer.render_for_prompt()


def test_several_sources_all_appear(fake_clock):
    gatherer = ContextGatherer([source("app", values=["Chrome"]),
                                source("project", values=["venom on v2"])],
                               clock=fake_clock)
    rendered = gatherer.render_for_prompt()
    assert "Chrome" in rendered and "venom on v2" in rendered


# ── caching, per source ─────────────────────────────────────────────────────
def test_a_source_is_not_re_read_inside_its_own_ttl(fake_clock):
    probe = source(ttl=60.0)
    gatherer = ContextGatherer([probe], clock=fake_clock)
    gatherer.snapshot()
    fake_clock.advance(30)
    gatherer.snapshot()
    assert len(probe.read.calls) == 1


def test_a_source_is_re_read_once_its_ttl_expires(fake_clock):
    probe = source(ttl=60.0, values=["Chrome", "VS Code"])
    gatherer = ContextGatherer([probe], clock=fake_clock)
    assert "Chrome" in gatherer.render_for_prompt()
    fake_clock.advance(61)
    assert "VS Code" in gatherer.render_for_prompt()


def test_each_source_keeps_its_own_schedule(fake_clock):
    """One interval for everything either burns cycles or serves stale answers."""
    fast = source("app", ttl=10.0)
    slow = source("project", ttl=600.0)
    gatherer = ContextGatherer([fast, slow], clock=fake_clock)
    gatherer.snapshot()
    fake_clock.advance(20)
    gatherer.snapshot()
    assert len(fast.read.calls) == 2
    assert len(slow.read.calls) == 1


def test_refresh_forces_a_re_read(fake_clock):
    probe = source(ttl=600.0)
    gatherer = ContextGatherer([probe], clock=fake_clock)
    gatherer.snapshot()
    gatherer.snapshot(refresh=True)
    assert len(probe.read.calls) == 2


# ── failure and staleness ───────────────────────────────────────────────────
def test_a_failing_probe_does_not_break_the_rest(fake_clock):
    gatherer = ContextGatherer([source("broken", raises=OSError("no display")),
                                source("app", values=["Chrome"])],
                               clock=fake_clock)
    assert "Chrome" in gatherer.render_for_prompt()
    assert gatherer.failing() == ["broken"]


def test_a_brief_blip_keeps_serving_the_last_good_value(fake_clock):
    probe = source(ttl=10.0, values=["Chrome", None])
    gatherer = ContextGatherer([probe], clock=fake_clock)
    gatherer.snapshot()
    fake_clock.advance(11)
    assert "Chrome" in gatherer.render_for_prompt()      # a blip, not a move


def test_a_value_that_has_gone_properly_stale_is_dropped(fake_clock):
    """Acting on where he was an hour ago is a confident, specific mistake."""
    probe = source(ttl=10.0, values=["Chrome", None])
    gatherer = ContextGatherer([probe], clock=fake_clock)
    gatherer.snapshot()
    fake_clock.advance(10 * STALE_MULTIPLE + 5)
    assert gatherer.render_for_prompt() == ""


def test_a_source_that_recovers_is_used_again(fake_clock):
    probe = source(ttl=10.0, values=[None, "VS Code"])
    gatherer = ContextGatherer([probe], clock=fake_clock)
    assert gatherer.render_for_prompt() == ""
    fake_clock.advance(11)
    assert "VS Code" in gatherer.render_for_prompt()
    assert gatherer.failing() == []


# ── the real sources ────────────────────────────────────────────────────────
def test_the_project_source_names_the_repo_and_branch(tmp_path):
    from flint_core.vcs import GitResult

    def fake_git(args):
        key = " ".join(args[1:])
        if key.startswith("rev-parse --git-dir"):
            return GitResult(True, ".git")
        if key.startswith("rev-parse --abbrev-ref"):
            return GitResult(True, "v2/rebuild")
        if key.startswith("status --porcelain"):
            return GitResult(True, " M a.py\n M b.py")
        return GitResult(True, "")

    item = git_project_source(tmp_path, runner=fake_git).read()
    assert item.value == f"{tmp_path.name} on v2/rebuild, 2 file(s) changed"


def test_a_relative_project_dir_still_gets_a_name(monkeypatch, tmp_path):
    """Regression: Path(".").name is "", so a gatherer pointed at the working
    directory reported " on v2/rebuild" with the repo name missing."""
    from flint_core.vcs import GitResult

    monkeypatch.chdir(tmp_path)
    item = git_project_source(".", runner=lambda a: GitResult(
        True, "main" if "abbrev-ref" in " ".join(a) else "")).read()
    assert item.value.startswith(tmp_path.resolve().name + " on main")


def test_the_project_source_says_nothing_outside_a_repo(tmp_path):
    from flint_core.vcs import GitResult

    item = git_project_source(tmp_path, runner=lambda a: GitResult(False, "")).read()
    assert item is None


def test_recent_files_lists_what_was_just_touched(tmp_path, fake_clock):
    import os

    (tmp_path / "edited.py").write_text("x", encoding="utf-8")
    (tmp_path / "old.py").write_text("x", encoding="utf-8")
    os.utime(tmp_path / "edited.py", (fake_clock.now, fake_clock.now))
    os.utime(tmp_path / "old.py", (fake_clock.now - 99999, fake_clock.now - 99999))

    item = recent_files_source([tmp_path], within_minutes=60,
                               clock=fake_clock).read()
    assert item.value == "edited.py"


def test_recent_files_says_nothing_when_nothing_is_recent(tmp_path, fake_clock):
    (tmp_path / "old.py").write_text("x", encoding="utf-8")
    import os

    os.utime(tmp_path / "old.py", (fake_clock.now - 99999, fake_clock.now - 99999))
    assert recent_files_source([tmp_path], within_minutes=1,
                               clock=fake_clock).read() is None


def test_recent_files_survives_a_missing_folder(fake_clock):
    assert recent_files_source(["/no/such/place"], clock=fake_clock).read() is None


def test_hidden_files_are_ignored(tmp_path, fake_clock):
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    assert recent_files_source([tmp_path], clock=fake_clock).read() is None


def test_the_window_probe_never_guesses_on_an_unknown_platform(monkeypatch):
    """"You're in Chrome" on a machine it cannot see is worse than silence."""
    import sys

    from flint_core.context import _foreground_window_title

    monkeypatch.setattr(sys, "platform", "linux")
    assert _foreground_window_title() == ""


# ── assembly ────────────────────────────────────────────────────────────────
def test_the_gatherer_skips_what_this_device_cannot_answer():
    assert len(build_gatherer(include_window=False)) == 0
    assert len(build_gatherer(project_dir=".", include_window=False)) == 1


def test_time_is_off_by_default():
    """The voice prompt already carries the clock; two is one too many."""
    built = build_gatherer(include_window=False)
    assert "time" not in [s.name for s in built._sources]
    assert "time" in [s.name for s in build_gatherer(
        include_time=True, include_window=False)._sources]
