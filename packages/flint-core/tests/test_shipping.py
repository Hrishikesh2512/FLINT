"""Build it, commit it, publish it — and stop honestly when it can't."""

from __future__ import annotations

import subprocess

from flint_core.agents import AgentRegistry, AgentResult, AgentSpec
from flint_core.kernel import Continue, Fail, Finish
from flint_core.shipping import repo_name_for, run_ship


class Ctx:
    def __init__(self, goal, cwd, scratch=None, services=None, **params):
        self.goal = goal
        self.params = {"cwd": str(cwd), **params}
        self.scratch = dict(scratch or {})
        self.services = services or {}
        self.notes: list[str] = []

    def log(self, note):
        self.notes.append(note)

    def require(self, name):
        return self.services[name]

    def service(self, name, default=None):
        return self.services.get(name, default)


def agents(result=None):
    return AgentRegistry([AgentSpec(
        name="coder", summary="Writes code.",
        run=lambda req: result or AgentResult(ok=True, summary="wrote it",
                                              artifacts=("main.py",)))])


def project(tmp_path, *files, git=False):
    for name in files:
        (tmp_path / name).write_text("print('hi')\n", encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


# ── naming ──────────────────────────────────────────────────────────────────
def test_a_name_is_derived_from_what_was_asked(tmp_path):
    name = repo_name_for("build me a CLI called wordcount that counts words",
                         tmp_path)
    assert "wordcount" in name
    assert " " not in name


def test_an_explicit_name_wins(tmp_path):
    assert repo_name_for("anything at all", tmp_path, "my-tool") == "my-tool"


def test_a_name_is_made_safe_for_github(tmp_path):
    assert repo_name_for("", tmp_path, "my project!! (v2)") == "my-project-v2"


def test_a_nameless_goal_still_gets_something(tmp_path):
    assert repo_name_for("the a an", tmp_path, "") == tmp_path.name


# ── the build half passes straight through ──────────────────────────────────
def test_the_build_half_is_delegated(tmp_path):
    project(tmp_path)
    outcome = run_ship(Ctx("build a thing", tmp_path, services={"agents": agents()}))
    assert isinstance(outcome, Continue)
    assert outcome.scratch["phase"] == "verify"          # run_build's own phase


def test_a_finished_build_carries_on_into_git_rather_than_stopping(tmp_path):
    """The whole reason this job type exists."""
    project(tmp_path, "README.md")       # nothing runnable -> build finishes
    outcome = run_ship(Ctx("build a thing", tmp_path,
                           scratch={"phase": "verify", "agent_summary": "wrote it"},
                           services={"agents": agents()}))
    assert isinstance(outcome, Continue)
    assert outcome.scratch["phase"] == "commit"


def test_a_failed_build_never_reaches_git(tmp_path):
    project(tmp_path)
    outcome = run_ship(Ctx("x", tmp_path, services={
        "agents": agents(AgentResult.failed("claude is not installed"))}))
    assert isinstance(outcome, Fail)


# ── committing ──────────────────────────────────────────────────────────────
def test_a_repo_is_started_when_there_is_none(tmp_path):
    project(tmp_path, "main.py")
    ctx = Ctx("build a thing", tmp_path, scratch={"phase": "commit"},
              services={"agents": agents()})
    outcome = run_ship(ctx)
    assert isinstance(outcome, Continue)
    assert outcome.scratch["phase"] == "publish"
    assert (tmp_path / ".git").is_dir()
    assert any("started a git repo" in n for n in ctx.notes)


def test_the_built_code_is_actually_committed(tmp_path):
    project(tmp_path, "main.py", "README.md")
    run_ship(Ctx("build a thing", tmp_path, scratch={"phase": "commit"},
                 services={"agents": agents()}))
    committed = subprocess.run(["git", "show", "--name-only", "--format="],
                               cwd=tmp_path, capture_output=True, text=True)
    assert "main.py" in committed.stdout


def test_the_goal_becomes_the_commit_message(tmp_path):
    project(tmp_path, "main.py")
    run_ship(Ctx("a CLI that counts words", tmp_path, scratch={"phase": "commit"},
                 services={"agents": agents()}))
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=tmp_path,
                         capture_output=True, text=True)
    assert "a CLI that counts words" in log.stdout


def test_an_explicit_message_wins(tmp_path):
    project(tmp_path, "main.py")
    run_ship(Ctx("x", tmp_path, scratch={"phase": "commit"}, message="my message",
                 services={"agents": agents()}))
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=tmp_path,
                         capture_output=True, text=True)
    assert log.stdout.strip() == "my message"


def test_a_secret_is_left_out_of_the_first_commit(tmp_path):
    project(tmp_path, "main.py")
    (tmp_path / ".env").write_text("GEMINI_API_KEY=sk-real", encoding="utf-8")
    run_ship(Ctx("x", tmp_path, scratch={"phase": "commit"},
                 services={"agents": agents()}))
    committed = subprocess.run(["git", "show", "--name-only", "--format="],
                               cwd=tmp_path, capture_output=True, text=True)
    assert "main.py" in committed.stdout
    assert ".env" not in committed.stdout


def test_nothing_to_commit_is_not_a_failure(tmp_path):
    """A second pass over an already-committed tree should carry on."""
    project(tmp_path, "main.py", git=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "first"], cwd=tmp_path, check=True)
    outcome = run_ship(Ctx("x", tmp_path, scratch={"phase": "commit"},
                           services={"agents": agents()}))
    assert isinstance(outcome, Continue)
    assert outcome.scratch["phase"] == "publish"


def test_an_empty_project_is_not_published(tmp_path):
    """Nothing committed means nothing to put anywhere."""
    project(tmp_path)                       # no files at all
    outcome = run_ship(Ctx("x", tmp_path, scratch={"phase": "commit"},
                           services={"agents": agents()}))
    assert isinstance(outcome, Fail)
    assert "nothing committed" in outcome.error


# ── publishing ──────────────────────────────────────────────────────────────
def test_without_gh_it_says_exactly_what_did_happen(tmp_path, monkeypatch):
    """Built, tested and committed is a useful answer. Pretending is not."""
    import flint_core.shipping as shipping

    monkeypatch.setattr(shipping, "gh_available", lambda: False)
    project(tmp_path, "main.py", git=True)
    outcome = run_ship(Ctx("x", tmp_path, services={"agents": agents()},
                           scratch={"phase": "publish", "built_say": "It's built."}))
    assert isinstance(outcome, Finish)
    assert "committed locally" in outcome.say
    assert "gh command isn't installed" in outcome.say


def test_publishing_can_be_declined_and_still_reports_the_work(tmp_path):
    project(tmp_path, "main.py", git=True)
    outcome = run_ship(Ctx("x", tmp_path, publish=False,
                           services={"agents": agents()},
                           scratch={"phase": "publish", "built_say": "It's built."}))
    assert isinstance(outcome, Finish)
    assert "say the word" in outcome.say


def test_it_is_private_unless_asked_otherwise(tmp_path, monkeypatch):
    """A private repo is deletable; a public one is indexed within minutes."""
    import flint_core.shipping as shipping

    seen = {}
    monkeypatch.setattr(shipping, "gh_available", lambda: True)
    monkeypatch.setattr(shipping, "_run",
                        lambda args, cwd, timeout=180.0:
                        (seen.update(args=args) or (True, "https://github.com/u/x")))
    project(tmp_path, "main.py", git=True)
    run_ship(Ctx("x", tmp_path, services={"agents": agents()},
                 scratch={"phase": "publish"}))
    assert "--private" in seen["args"] and "--public" not in seen["args"]


def test_public_is_possible_when_actually_requested(tmp_path, monkeypatch):
    import flint_core.shipping as shipping

    seen = {}
    monkeypatch.setattr(shipping, "gh_available", lambda: True)
    monkeypatch.setattr(shipping, "_run",
                        lambda args, cwd, timeout=180.0:
                        (seen.update(args=args) or (True, "https://github.com/u/x")))
    project(tmp_path, "main.py", git=True)
    run_ship(Ctx("x", tmp_path, public=True, services={"agents": agents()},
                 scratch={"phase": "publish"}))
    assert "--public" in seen["args"]


def test_the_url_comes_back_in_the_spoken_answer(tmp_path, monkeypatch):
    import flint_core.shipping as shipping

    monkeypatch.setattr(shipping, "gh_available", lambda: True)
    monkeypatch.setattr(shipping, "_run", lambda *a, **kw:
                        (True, "https://github.com/user/wordcount"))
    project(tmp_path, "main.py", git=True)
    outcome = run_ship(Ctx("wordcount CLI", tmp_path, services={"agents": agents()},
                           scratch={"phase": "publish", "built_say": "It's built."}))
    assert "github.com/user/wordcount" in outcome.say


def test_github_refusing_is_reported_not_swallowed(tmp_path, monkeypatch):
    import flint_core.shipping as shipping

    monkeypatch.setattr(shipping, "gh_available", lambda: True)
    monkeypatch.setattr(shipping, "_run", lambda *a, **kw:
                        (False, "GraphQL: Name already exists on this account"))
    project(tmp_path, "main.py", git=True)
    outcome = run_ship(Ctx("x", tmp_path, services={"agents": agents()},
                           scratch={"phase": "publish", "built_say": "It's built."}))
    assert isinstance(outcome, Finish)
    assert "already exists" in outcome.say
    assert "committed locally" in outcome.say


def test_an_unknown_phase_fails_clearly(tmp_path):
    project(tmp_path, git=True)
    outcome = run_ship(Ctx("x", tmp_path, scratch={"phase": "nonsense"},
                           services={"agents": agents()}))
    assert isinstance(outcome, Fail) and "unknown ship phase" in outcome.error


def test_a_folder_inside_another_repo_gets_its_own(tmp_path):
    """The hazard this was found by: a home directory under version control
    makes every scratch folder "inside a repo", so the init is skipped and the
    new project commits itself into that unrelated repository."""
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
    (outer / "someone-elses-file.txt").write_text("do not touch", encoding="utf-8")

    inner = outer / "nested" / "project"
    inner.mkdir(parents=True)
    (inner / "main.py").write_text("print('hi')\n", encoding="utf-8")

    ctx = Ctx("build a thing", inner, scratch={"phase": "commit"},
              services={"agents": agents()})
    outcome = run_ship(ctx)

    assert isinstance(outcome, Continue)
    assert (inner / ".git").is_dir()            # its own repo, not the outer one
    committed = subprocess.run(["git", "show", "--name-only", "--format="],
                               cwd=inner, capture_output=True, text=True)
    assert "main.py" in committed.stdout
    assert "someone-elses-file" not in committed.stdout
    # And the outer repo was left completely alone.
    outer_log = subprocess.run(["git", "log", "--oneline"], cwd=outer,
                               capture_output=True, text=True)
    assert outer_log.returncode != 0 or outer_log.stdout.strip() == ""
