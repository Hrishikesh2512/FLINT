"""The research job type — planning, searching, and folding it all together.

The provider is a fake throughout: these tests are about whether the steps
hand their results to each other correctly, which is the part that has to work
before any of it is worth calling.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flint_core.kernel import Continue, Fail, Finish, JobStore, Scheduler
from venom.jobs import MAX_QUESTIONS, build_registry, run_research


class FakeProvider:
    """Scriptable stand-in for GeminiProvider — records what it was asked."""

    models = ("fake-model",)

    def __init__(self, plan=("what is X?", "who makes X?"), answers=None,
                 synthesis=None):
        self._plan = list(plan)
        self._answers = dict(answers or {})
        self._synthesis = synthesis or {"answer": "The full written answer.",
                                        "say": "The short spoken answer."}
        self.searched: list[str] = []
        self.completions: list[str] = []
        self.fail_search_times = 0

    def complete(self, messages, model, *, max_tokens, temperature, json_mode):
        system = messages[0].content
        user = messages[1].content
        self.completions.append(user)
        if "break a research question" in system:
            return json.dumps({"questions": self._plan})
        return json.dumps(self._synthesis)

    def grounded_search(self, query, model=None, max_tokens=2048):
        if self.fail_search_times > 0:
            self.fail_search_times -= 1
            raise ConnectionError("search is down")
        self.searched.append(query)
        for key, answer in self._answers.items():
            if key in query:
                return answer
        return f"facts about: {query[:40]}"


class Ctx:
    """A JobContext without a kernel behind it, for single-step tests."""

    def __init__(self, provider, goal="why is the sky blue", scratch=None):
        self.job = type("J", (), {"goal": goal, "type": "research"})()
        self.goal = goal
        self.params: dict = {}
        self.scratch = dict(scratch or {})
        self.services = {"provider": provider}
        self.notes: list[str] = []

    def log(self, note):
        self.notes.append(note)

    def require(self, name):
        return self.services[name]


# ── one step at a time ──────────────────────────────────────────────────────
def test_the_first_step_plans_the_searches():
    provider = FakeProvider(plan=["why is the sky blue", "what is Rayleigh scattering"])
    outcome = run_research(Ctx(provider))
    assert isinstance(outcome, Continue)
    assert outcome.scratch["questions"] == ["why is the sky blue",
                                            "what is Rayleigh scattering"]
    assert outcome.scratch["findings"] == []
    assert provider.searched == []          # planning does not search


def test_planning_falls_back_to_the_goal_itself():
    """A planner that returns nothing usable must not sink the job."""
    outcome = run_research(Ctx(FakeProvider(plan=[])))
    assert outcome.scratch["questions"] == ["why is the sky blue"]


def test_the_plan_is_capped():
    provider = FakeProvider(plan=[f"question {i}" for i in range(20)])
    outcome = run_research(Ctx(provider))
    assert len(outcome.scratch["questions"]) == MAX_QUESTIONS


def test_each_step_searches_one_question_and_keeps_the_answer():
    provider = FakeProvider(answers={"first": "the first fact"})
    ctx = Ctx(provider, scratch={"questions": ["the first one", "the second one"],
                                 "findings": []})
    outcome = run_research(ctx)
    assert isinstance(outcome, Continue)
    assert outcome.scratch["questions"] == ["the second one"]      # consumed
    assert outcome.scratch["findings"] == [
        {"question": "the first one", "answer": "the first fact"}]
    assert len(provider.searched) == 1


def test_earlier_findings_are_carried_forward_not_replaced():
    ctx = Ctx(FakeProvider(), scratch={
        "questions": ["the second one"],
        "findings": [{"question": "the first one", "answer": "already known"}]})
    outcome = run_research(ctx)
    assert [f["question"] for f in outcome.scratch["findings"]] == [
        "the first one", "the second one"]


def test_the_final_step_synthesises_every_finding():
    provider = FakeProvider(synthesis={"answer": "Because of scattering.",
                                       "say": "Short version: scattering."})
    ctx = Ctx(provider, scratch={
        "questions": [],
        "findings": [{"question": "q1", "answer": "a1"},
                     {"question": "q2", "answer": "a2"}]})
    outcome = run_research(ctx)
    assert isinstance(outcome, Finish)
    assert outcome.result == "Because of scattering."
    assert outcome.say == "Short version: scattering."
    # Every finding reached the synthesis prompt.
    assert "a1" in provider.completions[-1] and "a2" in provider.completions[-1]
    assert "why is the sky blue" in provider.completions[-1]   # the original goal


def test_the_spoken_answer_is_capped_but_the_written_one_is_not():
    from venom.jobs import MAX_SPOKEN_CHARS

    provider = FakeProvider(synthesis={"answer": "x" * 5000, "say": "y" * 5000})
    ctx = Ctx(provider, scratch={"questions": [],
                                 "findings": [{"question": "q", "answer": "a"}]})
    outcome = run_research(ctx)
    assert len(outcome.say) == MAX_SPOKEN_CHARS
    assert len(outcome.result) == 5000


def test_a_missing_spoken_version_falls_back_to_the_written_one():
    provider = FakeProvider(synthesis={"answer": "The answer.", "say": ""})
    ctx = Ctx(provider, scratch={"questions": [],
                                 "findings": [{"question": "q", "answer": "a"}]})
    assert run_research(ctx).say == "The answer."


# ── failure ─────────────────────────────────────────────────────────────────
def test_a_failed_search_is_retryable_and_loses_nothing():
    provider = FakeProvider()
    provider.fail_search_times = 1
    ctx = Ctx(provider, scratch={"questions": ["still pending"], "findings": []})
    outcome = run_research(ctx)
    assert isinstance(outcome, Fail)
    assert outcome.retry is True


def test_finishing_with_no_findings_at_all_is_a_hard_failure():
    ctx = Ctx(FakeProvider(), scratch={"questions": [], "findings": []})
    outcome = run_research(ctx)
    assert isinstance(outcome, Fail)
    assert outcome.retry is False


def test_unparseable_synthesis_is_retryable():
    class BadSynthesis(FakeProvider):
        def complete(self, messages, model, **kw):
            if "summarising research" in messages[0].content:
                return "not json at all"
            return json.dumps({"questions": ["q"]})

    ctx = Ctx(BadSynthesis(), scratch={"questions": [],
                                       "findings": [{"question": "q", "answer": "a"}]})
    outcome = run_research(ctx)
    assert isinstance(outcome, Fail)
    assert outcome.retry is True


# ── end to end, on the real kernel ──────────────────────────────────────────
@pytest.fixture()
def clock():
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    return Clock()


def test_a_whole_research_job_runs_to_a_spoken_answer(tmp_path, clock):
    provider = FakeProvider(
        plan=["what happened", "why did it happen"],
        synthesis={"answer": "Here is the long version.",
                   "say": "Here is the short version."})
    spoken: list[str] = []
    store = JobStore(tmp_path / "jobs.db", clock=clock)
    sched = Scheduler(store, build_registry(), clock=clock,
                      services={"provider": provider},
                      deliver=lambda job: spoken.append(job.say) or True)

    job = sched.submit("research", "what is going on with the rupee")
    for _ in range(4):                       # plan, 2 searches, synthesis
        asyncio.run(sched.tick())
        clock.advance(60)

    assert spoken == ["Here is the short version."]
    assert store.get(job.id).result == "Here is the long version."
    assert len(provider.searched) == 2
    notes = [event["note"] for event in store.events(job.id)]
    assert any("planned 2 search" in note for note in notes)
    assert any(note.startswith("searched:") for note in notes)


def test_a_research_job_can_be_cancelled_midway(tmp_path, clock):
    provider = FakeProvider(plan=["a", "b", "c"])
    store = JobStore(tmp_path / "jobs.db", clock=clock)
    sched = Scheduler(store, build_registry(), clock=clock,
                      services={"provider": provider}, max_passes=1)

    job = sched.submit("research", "something long")
    asyncio.run(sched.tick())                # plans
    sched.cancel(job.id)
    for _ in range(3):
        asyncio.run(sched.tick())
        clock.advance(60)

    assert provider.searched == []           # never got to searching
    assert store.get(job.id).state == "cancelled"


def test_two_research_jobs_at_once_is_the_ceiling(tmp_path, clock):
    store = JobStore(tmp_path / "jobs.db", clock=clock)
    sched = Scheduler(store, build_registry(), clock=clock,
                      services={"provider": FakeProvider()})
    sched.submit("research", "one")
    sched.submit("research", "two")
    with pytest.raises(ValueError, match="that's the limit"):
        sched.submit("research", "three")


def test_the_registry_describes_research_for_a_planner():
    docs = build_registry().documentation()
    assert "research — " in docs
    assert "steps over 2h" in docs


# ── how a finished job opens a conversation ─────────────────────────────────
def test_the_proactive_opening_leads_with_the_answer(tmp_path, clock):
    from flint_core.kernel import Job
    from venom.jobs import job_instruction

    job = Job.create("research", "what is going on with the rupee", now=clock.now)
    instruction = job_instruction(
        type(job)(**{**job.__dict__, "say": "It hit 88 against the dollar."}),
        "Tushar")

    assert instruction.startswith("[Proactive]")
    assert "what is going on with the rupee" in instruction   # what he asked
    assert "It hit 88 against the dollar." in instruction     # what she found
    assert "Tushar" in instruction
    # She is interrupting hours later — the one thing she must not do is
    # open with a greeting, the way watch_instruction also guards against.
    assert "do not greet him" in instruction.lower()


# ── agent tasks as background jobs ──────────────────────────────────────────
class FakeAgents:
    """Stands in for an AgentRegistry."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.requests = []

    def run(self, request, *, task="", agent=""):
        if self._raises:
            raise self._raises
        self.requests.append((request, task, agent))
        request.progress("agent said something")
        return self._result


def agent_ctx(result=None, raises=None, goal="refactor the parser", **params):
    from venom.jobs import run_agent_task

    agents = FakeAgents(result, raises)
    ctx = Ctx(None, goal=goal)
    ctx.services = {"agents": agents}
    ctx.params = params
    return run_agent_task(ctx), agents, ctx


def test_a_finished_agent_task_reports_what_changed():
    from flint_core.agents import AgentResult

    outcome, _, ctx = agent_ctx(AgentResult(
        ok=True, summary="Renamed the parser module.", detail="full transcript",
        artifacts=("parser.py", "test_parser.py")))
    assert isinstance(outcome, Finish)
    assert outcome.result == "full transcript"
    assert "Renamed the parser module." in outcome.say
    assert "parser.py" in outcome.say
    assert "agent said something" in ctx.notes      # progress was journalled


def test_the_goal_and_parameters_reach_the_agent():
    from flint_core.agents import AgentResult

    _, agents, _ = agent_ctx(AgentResult(ok=True, summary="done"),
                             cwd="/repo", task="code", agent="claude")
    request, task, agent = agents.requests[0]
    assert request.goal == "refactor the parser"
    assert request.cwd == "/repo"
    assert (task, agent) == ("code", "claude")


def test_a_failed_agent_task_does_not_retry():
    """A coding agent that reports failure meant it — retrying burns budget."""
    from flint_core.agents import AgentResult

    outcome, _, _ = agent_ctx(AgentResult(ok=False, summary="tests failed",
                                          error="3 tests failing"))
    assert isinstance(outcome, Fail)
    assert outcome.retry is False
    assert "3 tests failing" in outcome.error


def test_an_agent_that_needs_input_finishes_by_asking():
    """A job cannot hold a conversation, but she can — so it comes back asking."""
    from flint_core.agents import AgentResult

    outcome, _, _ = agent_ctx(AgentResult(
        ok=False, summary="stopped", question="Which branch should I target?"))
    assert isinstance(outcome, Finish)
    assert outcome.say == "Which branch should I target?"


def test_no_agent_available_fails_without_retrying():
    from flint_core.agents import NoAgentAvailableError

    outcome, _, _ = agent_ctx(raises=NoAgentAvailableError("nothing available"))
    assert isinstance(outcome, Fail)
    assert outcome.retry is False


def test_the_agent_task_type_is_registered():
    docs = build_registry().documentation()
    assert "agent_task — " in docs


# ── reading sources he actually named ───────────────────────────────────────
def read_ctx(provider, goal, scratch=None, monkeypatch=None, document=None):
    import venom.jobs as jobs_module
    from flint_core.reading import Document

    ctx = Ctx(provider, goal=goal, scratch=scratch)
    if monkeypatch is not None:
        import flint_core.reading as reading

        monkeypatch.setattr(reading, "fetch",
                            lambda url, **kw: document or Document(
                                url=url, title="A Paper", text="THE ACTUAL TEXT"))
    return ctx, jobs_module


def test_a_named_url_is_read_before_any_searching(monkeypatch):
    import flint_core.reading as reading
    from flint_core.reading import Document
    monkeypatch.setattr(reading, "fetch", lambda url, **kw: Document(
        url=url, title="A Paper", text="THE ACTUAL TEXT"))

    provider = FakeProvider()
    ctx = Ctx(provider, goal="read https://example.com/rfc and tell me if it applies")
    outcome = run_research(ctx)

    assert isinstance(outcome, Continue)
    assert outcome.scratch["read"] == ["https://example.com/rfc"]
    assert outcome.scratch["readings"][0]["answer"] == "THE ACTUAL TEXT"
    assert provider.searched == []              # nothing searched yet
    assert any("read A Paper" in note for note in ctx.notes)


def test_a_goal_with_no_urls_goes_straight_to_planning():
    outcome = run_research(Ctx(FakeProvider(plan=["q1"])))
    assert isinstance(outcome, Continue)
    assert outcome.scratch["questions"] == ["q1"]


def test_a_source_that_will_not_open_is_noted_and_skipped(monkeypatch):
    import flint_core.reading as reading
    from flint_core.reading import Document
    monkeypatch.setattr(reading, "fetch", lambda url, **kw: Document(
        url=url, title="", text="", error="the site returned 404"))

    ctx = Ctx(FakeProvider(), goal="read https://example.com/gone please")
    outcome = run_research(ctx)
    assert outcome.scratch["read"] == ["https://example.com/gone"]
    assert outcome.scratch["readings"] == []     # nothing salvaged
    assert any("couldn't read" in note for note in ctx.notes)


def test_what_was_read_reaches_the_synthesis(monkeypatch):
    """The document is a finding, so the final answer is built from it."""
    provider = FakeProvider(plan=["a follow-up question"])
    ctx = Ctx(provider, goal="read https://example.com/x",
              scratch={"read": ["https://example.com/x"],
                       "readings": [{"question": "the document at x",
                                     "answer": "DOCUMENT TEXT"}]})
    outcome = run_research(ctx)
    assert outcome.scratch["findings"][0]["answer"] == "DOCUMENT TEXT"


def test_each_source_is_read_one_step_at_a_time(monkeypatch):
    import flint_core.reading as reading
    from flint_core.reading import Document
    monkeypatch.setattr(reading, "fetch", lambda url, **kw: Document(
        url=url, title="doc", text="text"))

    goal = "compare https://a.com/one and https://b.com/two"
    first = run_research(Ctx(FakeProvider(), goal=goal))
    assert first.scratch["read"] == ["https://a.com/one"]

    second = run_research(Ctx(FakeProvider(), goal=goal, scratch=first.scratch))
    assert second.scratch["read"] == ["https://a.com/one", "https://b.com/two"]
