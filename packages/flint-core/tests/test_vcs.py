"""Git: the safe operations offered, the destructive ones refused."""

from __future__ import annotations

from flint_core.vcs import PROTECTED_BRANCHES, GitRepo, GitResult, looks_secret


class FakeGit:
    """Records commands; answers from a scripted table."""

    def __init__(self, answers=None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, args):
        self.calls.append(list(args))
        key = " ".join(args[1:])
        for prefix, result in self.answers.items():
            if key.startswith(prefix):
                return result
        return GitResult(True, "")

    @property
    def commands(self):
        return [" ".join(c) for c in self.calls]


def repo(tmp_path, answers=None):
    fake = FakeGit(answers)
    return GitRepo(tmp_path, runner=fake), fake


# ── refusing the destructive half ───────────────────────────────────────────
def test_force_push_is_refused():
    git, fake = repo(".")
    result = git.run("push", "--force")
    assert result.ok is False
    assert "destroy work" in result.error
    assert fake.calls == []                 # never reached git


def test_reset_and_rebase_are_refused():
    git, _ = repo(".")
    assert git.run("reset", "--hard", "HEAD~3").ok is False
    assert git.run("rebase", "-i", "main").ok is False


def test_force_with_lease_is_refused_too():
    """It is gentler, not safe — and an agent cannot judge when it is fine."""
    git, _ = repo(".")
    assert git.run("push", "--force-with-lease").ok is False


def test_ordinary_commands_go_through(tmp_path):
    git, fake = repo(tmp_path)
    git.run("status", "--porcelain")
    assert fake.commands == ["git status --porcelain"]


# ── protecting the branch and the secrets ───────────────────────────────────
def test_committing_straight_to_main_is_refused(tmp_path):
    git, _ = repo(tmp_path, {"branch --show-current": GitResult(True, "main"),
                             "rev-parse --verify HEAD": GitResult(True, "abc123"),
                             "remote": GitResult(True, "origin")})
    result = git.commit("some change")
    assert result.ok is False
    assert "Make a branch first" in result.error


def test_every_protected_branch_is_covered(tmp_path):
    for branch in PROTECTED_BRANCHES:
        git, _ = repo(tmp_path, {"branch --show-current": GitResult(True, branch),
                                 "rev-parse --verify HEAD": GitResult(True, "abc"),
                                 "remote": GitResult(True, "origin")})
        assert git.commit("x").ok is False


def test_a_feature_branch_commits_fine(tmp_path):
    git, fake = repo(tmp_path, {
        "branch --show-current": GitResult(True, "feature/thing"),
        "diff --staged --quiet": GitResult(False, ""),   # exit 1 = has changes
    })
    assert git.commit("a real change").ok is True
    assert "git commit -m a real change" in fake.commands


def test_a_detached_head_is_refused(tmp_path):
    """A commit that lands nowhere still looks like it worked."""
    git, _ = repo(tmp_path, {"branch --show-current": GitResult(True, ""),
                             "rev-parse --abbrev-ref": GitResult(True, "HEAD")})
    result = git.commit("x")
    assert result.ok is False and "detached" in result.error


def test_committing_nothing_is_reported_not_faked(tmp_path):
    git, _ = repo(tmp_path, {
        "branch --show-current": GitResult(True, "feature/x"),
        "diff --staged --quiet": GitResult(True, ""),    # exit 0 = nothing staged
    })
    assert "nothing staged" in git.commit("x").error


def test_a_commit_needs_a_message(tmp_path):
    git, _ = repo(tmp_path)
    assert "needs a message" in git.commit("  ").error


def test_staging_a_secret_is_refused(tmp_path):
    git, _ = repo(tmp_path)
    result = git.stage([".env"])
    assert result.ok is False and "looks like a secret" in result.error


def test_the_secret_patterns_cover_the_usual_suspects():
    for path in (".env", ".env.local", "config/.env", "id_rsa", "certs/x.pem",
                 "app.key", "secrets.json", "credentials.yaml"):
        assert looks_secret(path), path
    for path in ("main.py", "environment.md", "keyboard.ts", "README.md"):
        assert not looks_secret(path), path


def test_staging_prefers_tracked_changes(tmp_path):
    """Never -A: an agent that stages everything commits the .env eventually.
    With tracked changes present, `add -u` is all that runs."""
    git, fake = repo(tmp_path, {
        "diff --staged --quiet": GitResult(False, ""),   # exit 1 = has changes
    })
    git.stage()
    assert fake.commands == ["git add -u", "git diff --staged --quiet"]


# ── the rest ────────────────────────────────────────────────────────────────
def test_changed_files_are_parsed_from_porcelain(tmp_path):
    git, _ = repo(tmp_path, {"status --porcelain": GitResult(
        True, " M src/main.py\n?? notes.txt\n M README.md")})
    assert git.changed_files() == ["src/main.py", "notes.txt", "README.md"]


def test_pushing_a_protected_branch_is_refused(tmp_path):
    git, _ = repo(tmp_path, {"branch --show-current": GitResult(True, "main")})
    assert git.push().ok is False


def test_pushing_a_feature_branch_sets_upstream(tmp_path):
    git, fake = repo(tmp_path, {"branch --show-current": GitResult(True, "feat")})
    git.push()
    assert "git push --set-upstream origin feat" in fake.commands


def test_a_pull_request_goes_through_gh(tmp_path):
    git, fake = repo(tmp_path)
    git.pull_request("Add the thing", "It does the thing.")
    assert fake.calls[-1][:3] == ["gh", "pr", "create"]


def test_a_pull_request_needs_a_title(tmp_path):
    git, _ = repo(tmp_path)
    assert "needs a title" in git.pull_request("").error


def test_an_invalid_branch_name_is_refused(tmp_path):
    git, _ = repo(tmp_path)
    assert git.create_branch("--upload-pack=evil").ok is False
    assert git.create_branch("").ok is False


def test_log_count_is_clamped(tmp_path):
    git, fake = repo(tmp_path)
    git.log(9999)
    assert "git log -50 --oneline" in fake.commands


def test_a_missing_directory_is_reported():
    git = GitRepo("/no/such/place/at/all")
    assert git.run("status").ok is False
    assert "no such directory" in git.run("status").error


# ── a brand-new project, which is what anything just built looks like ───────
def fresh_repo(tmp_path):
    """A real git repo with no commits — an unborn branch."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "main.py").write_text("print('hi')", encoding="utf-8")
    return GitRepo(tmp_path)


def test_an_unborn_branch_is_not_a_detached_head(tmp_path):
    """Regression: rev-parse --abbrev-ref HEAD fails on a repo with no
    commits, so a freshly built project looked detached and was refused."""
    git = fresh_repo(tmp_path)
    assert git.branch() != ""
    assert git.has_commits() is False


def test_a_first_commit_works_on_a_new_repo(tmp_path):
    """The protected-branch rule guards shared history; there isn't any yet."""
    git = fresh_repo(tmp_path)
    result = git.commit("initial commit", paths=["main.py"])
    assert result.ok is True, result.text
    assert git.has_commits() is True


def test_a_local_only_repo_is_not_protected(tmp_path):
    """Nobody else has this branch — "main" here is a default name, not a
    shared trunk. Refusing made a project Venom built unresumable."""
    git = fresh_repo(tmp_path)
    assert git.commit("initial commit", paths=["main.py"]).ok is True
    (tmp_path / "main.py").write_text("print('changed')", encoding="utf-8")
    assert git.has_remote() is False
    assert git.commit("second commit").ok is True


def test_the_protection_returns_once_the_repo_is_shared(tmp_path):
    import subprocess

    git = fresh_repo(tmp_path)
    git.commit("initial commit", paths=["main.py"])
    subprocess.run(["git", "remote", "add", "origin",
                    "https://example.com/x.git"], cwd=tmp_path, check=True)
    (tmp_path / "main.py").write_text("print('changed')", encoding="utf-8")
    result = git.commit("second commit")
    assert result.ok is False
    assert "Make a branch first" in result.error


def test_a_branch_on_a_new_repo_then_commits(tmp_path):
    git = fresh_repo(tmp_path)
    assert git.create_branch("feature/thing").ok is True
    assert git.branch() == "feature/thing"
    assert git.commit("first", paths=["main.py"]).ok is True


def test_a_new_project_stages_its_untracked_files(tmp_path):
    """Regression: `git add -u` stages tracked changes, and a brand-new
    project has none — so nothing could ever make its first commit."""
    git = fresh_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    assert git.stage().ok is True
    assert git.has_staged_changes() is True


def test_a_secret_is_left_behind_even_on_a_first_commit(tmp_path):
    """The reason this enumerates instead of using -A."""
    git = fresh_repo(tmp_path)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=sk-real", encoding="utf-8")
    result = git.commit("initial commit")
    assert result.ok is True
    committed = git.run("show", "--name-only", "--format=", "HEAD").output
    assert "main.py" in committed
    assert ".env" not in committed


def test_an_untracked_secret_is_reported_not_silently_dropped(tmp_path):
    git = fresh_repo(tmp_path)
    (tmp_path / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    result = git.stage()
    assert result.ok is True
    assert "id_rsa" in result.output and "looks secret" in result.output


def test_gitignored_files_are_not_staged(tmp_path):
    git = fresh_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "junk.o").write_text("x", encoding="utf-8")
    git.stage()
    assert "junk.o" not in git.run("diff", "--staged", "--name-only").output
