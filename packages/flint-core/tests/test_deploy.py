"""Deployment: the four gates, and what happens when one of them says no."""

from __future__ import annotations

import pytest

from flint_core.deploy import (
    DeployError,
    DeployTarget,
    DeployTargets,
    detect_recipe,
    execute,
    plan_deploy,
    run_deploy,
)


def targets(*specs):
    return DeployTargets([DeployTarget(**s) for s in specs])


def project(tmp_path, *files):
    for name in files:
        (tmp_path / name).write_text("x", encoding="utf-8")
    return tmp_path


# ── gate 1: the target comes from config, never from a model ────────────────
def test_an_unknown_target_is_refused_with_the_real_options():
    known = targets({"name": "vps"}, {"name": "pi"})
    with pytest.raises(DeployError) as caught:
        known.get("wherever-the-model-fancied")
    assert "I can deploy to: pi, vps" in str(caught.value)


def test_no_targets_configured_says_so():
    with pytest.raises(DeployError, match="none configured"):
        DeployTargets().get("anywhere")


def test_target_lookup_is_case_insensitive():
    assert targets({"name": "VPS"}).get("vps").name == "VPS"


def test_a_target_name_must_be_one_word():
    with pytest.raises(DeployError, match="one word"):
        DeployTarget(name="my server")


def test_bad_targets_are_skipped_not_fatal():
    built = DeployTargets.from_config([
        {"name": ""}, {"name": "two words"}, {"name": "good", "host": "h"}])
    assert built.names() == ["good"]


# ── gate 3: the command comes from the project, never invented ──────────────
@pytest.mark.parametrize("files,kind", [
    (["docker-compose.yml"], "docker compose"),
    (["compose.yaml"], "docker compose"),
    (["Dockerfile"], "docker"),
])
def test_the_recipe_is_detected_from_the_project(files, kind):
    assert detect_recipe(files)[1] == kind


def test_compose_beats_a_bare_dockerfile():
    assert detect_recipe(["Dockerfile", "docker-compose.yml"])[1] == "docker compose"


def test_an_unrecognisable_project_is_refused_not_guessed(tmp_path):
    project(tmp_path, "main.py", "README.md")
    with pytest.raises(DeployError, match="not going to guess"):
        plan_deploy(tmp_path, DeployTarget(name="vps"))


def test_the_target_name_is_the_only_thing_interpolated(tmp_path):
    project(tmp_path, "Dockerfile")
    plan = plan_deploy(tmp_path, DeployTarget(name="wordcount"))
    assert plan.rendered() == [
        "docker build -t wordcount .",
        "docker run -d --name wordcount wordcount",
    ]


def test_a_remote_target_goes_over_ssh(tmp_path):
    project(tmp_path, "docker-compose.yml")
    plan = plan_deploy(tmp_path, DeployTarget(name="vps", host="my.host",
                                              directory="/srv/app"))
    rendered = plan.rendered()
    assert all(line.startswith("ssh my.host") for line in rendered)
    assert "cd /srv/app && docker compose build" in rendered[0]


def test_ssh_paths_are_quoted(tmp_path):
    """A directory with a space must not become two arguments."""
    project(tmp_path, "Dockerfile")
    plan = plan_deploy(tmp_path, DeployTarget(name="vps", host="h",
                                              directory="/srv/my app"))
    assert "'/srv/my app'" in plan.rendered()[0]


# ── gate 4: nothing ships without being asked twice ─────────────────────────
def test_the_default_is_a_dry_run_that_changes_nothing(tmp_path):
    project(tmp_path, "Dockerfile")
    plan = plan_deploy(tmp_path, DeployTarget(name="vps"))
    ran = []
    ok, output = execute(plan, runner=lambda c, d, t: ran.append(c) or (True, ""))
    assert ok is True
    assert ran == []                       # nothing was executed
    assert "nothing has been deployed" in output
    assert "docker build" in output        # but it said what it would do


def test_confirming_actually_runs_it(tmp_path):
    project(tmp_path, "Dockerfile")
    plan = plan_deploy(tmp_path, DeployTarget(name="vps"))
    ran = []
    ok, _ = execute(plan, confirm=True,
                    runner=lambda c, d, t: ran.append(c) or (True, "done"))
    assert ok is True
    assert len(ran) == 2


def test_a_failing_command_stops_the_rest(tmp_path):
    """Carrying on after `docker build` fails would start the previous image —
    a successful-looking deploy of code that was never built."""
    project(tmp_path, "Dockerfile")
    plan = plan_deploy(tmp_path, DeployTarget(name="vps"))
    ran = []

    def runner(command, cwd, timeout):
        ran.append(command)
        return False, "build failed: no such base image"

    ok, output = execute(plan, confirm=True, runner=runner)
    assert ok is False
    assert len(ran) == 1                   # never reached docker run
    assert "no such base image" in output


# ── gate 2: never ship code nobody ran ──────────────────────────────────────
class Ctx:
    def __init__(self, cwd, services=None, **params):
        self.goal = "deploy it"
        self.params = {"cwd": str(cwd), **params}
        self.scratch: dict = {}
        self.services = services or {}
        self.notes: list[str] = []

    def log(self, note):
        self.notes.append(note)

    def require(self, name):
        return self.services[name]

    def service(self, name, default=None):
        return self.services.get(name, default)


def test_failing_tests_stop_the_deploy(tmp_path):
    from flint_core.kernel import Fail

    project(tmp_path, "Dockerfile")
    (tmp_path / "tests").mkdir()
    ctx = Ctx(tmp_path, target="vps", confirm=True, services={
        "deploy_targets": targets({"name": "vps"}),
        "verify": lambda cwd, cmd, t: (False, "2 failed"),
    })
    outcome = run_deploy(ctx)
    assert isinstance(outcome, Fail)
    assert "not deploying" in outcome.error and "2 failed" in outcome.error


def test_passing_tests_let_it_through(tmp_path):
    from flint_core.kernel import Finish

    project(tmp_path, "Dockerfile")
    (tmp_path / "tests").mkdir()
    ctx = Ctx(tmp_path, target="vps", services={
        "deploy_targets": targets({"name": "vps"}),
        "verify": lambda cwd, cmd, t: (True, "3 passed"),
    })
    assert isinstance(run_deploy(ctx), Finish)


def test_a_project_with_nothing_to_run_is_allowed_through(tmp_path):
    """Refusing would make this unusable for a static site."""
    from flint_core.kernel import Finish

    project(tmp_path, "Dockerfile", "index.html")
    ctx = Ctx(tmp_path, target="vps", services={
        "deploy_targets": targets({"name": "vps"}),
        "verify": lambda cwd, cmd, t: (False, "should not be called"),
    })
    assert isinstance(run_deploy(ctx), Finish)


def test_a_dry_run_job_says_what_it_would_do(tmp_path):
    from flint_core.kernel import Finish

    project(tmp_path, "Dockerfile")
    ctx = Ctx(tmp_path, target="vps", services={
        "deploy_targets": targets({"name": "vps"})})
    outcome = run_deploy(ctx)
    assert isinstance(outcome, Finish)
    assert "haven't deployed anything" in outcome.say
    assert "Say go ahead" in outcome.say


def test_a_production_target_says_so_out_loud(tmp_path):
    project(tmp_path, "Dockerfile")
    ctx = Ctx(tmp_path, target="live", services={
        "deploy_targets": targets({"name": "live", "host": "h",
                                   "production": True})})
    assert "production" in run_deploy(ctx).say


def test_an_unknown_target_fails_the_job_without_retrying(tmp_path):
    from flint_core.kernel import Fail

    project(tmp_path, "Dockerfile")
    ctx = Ctx(tmp_path, target="nowhere", services={
        "deploy_targets": targets({"name": "vps"})})
    outcome = run_deploy(ctx)
    assert isinstance(outcome, Fail) and outcome.retry is False


def test_a_missing_directory_fails_cleanly(tmp_path):
    from flint_core.kernel import Fail

    ctx = Ctx(tmp_path / "nope", target="vps", services={
        "deploy_targets": targets({"name": "vps"})})
    assert isinstance(run_deploy(ctx), Fail)
