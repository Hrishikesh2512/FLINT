"""Venom's job types — the work she goes away and does properly.

The kernel (`flint_core.kernel`) owns scheduling, budgets, persistence and
delivery. This module owns the other half: what each kind of job actually
does, one step at a time. Job types are a product decision the way tools are,
so they live here next to `tools_pi.py` rather than in the shared core.

    research — break a question into sub-questions, search each one, then
               synthesise the answers into something worth saying out loud

Research is the first type on purpose. It is the smallest job that cannot be
expressed as a tool call: the questions asked in step 3 depend on what step 2
found, the synthesis in the final step depends on all of them, and the whole
thing takes longer than anyone will hold a conversation open for. That is the
shape every later type shares — a coding task, a deployment, a long watch.
"""

from __future__ import annotations

import json
import logging

from flint_core.kernel import Continue, Fail, Finish, JobContext, RunnerRegistry
from flint_core.llm.base import ChatMessage

log = logging.getLogger("venom.jobs")

# One search per step. The kernel spaces steps out, so a research job costs a
# handful of calls spread over minutes rather than a burst — and it can be
# cancelled halfway without having already paid for the whole thing.
MAX_QUESTIONS = 5
PLAN_TOKENS = 500
SYNTHESIS_TOKENS = 1200

# A spoken answer is not a document. The full write-up is kept as the job's
# result (the console shows it); only this much is ever read aloud.
MAX_SPOKEN_CHARS = 700

_PLAN_SYSTEM = (
    "You break a research question into the few specific web searches that "
    "would actually answer it.\n\n"
    'Reply with JSON only: {"questions": ["...", "..."]}\n\n'
    "Rules:\n"
    f"- At most {MAX_QUESTIONS} questions, fewer when fewer will do. Each one "
    "must be independently searchable — a phrase you would type into Google, "
    "not a instruction to an assistant.\n"
    "- Cover genuinely different angles. Two rewordings of the same question "
    "waste a step and tell you nothing new.\n"
    "- If the goal is already one plain factual question, return just it."
)

_SYNTHESIS_SYSTEM = (
    "You are summarising research you just did, for someone who asked you to "
    "go and find out and has been doing something else since.\n\n"
    'Reply with JSON only: {"answer": "<the full written answer>", '
    '"say": "<the spoken version — two or three sentences, the actual '
    'finding, no preamble>"}\n\n'
    "Rules:\n"
    "- Answer the original question directly, first sentence. No 'I looked "
    "into this and found that...'.\n"
    "- Use only what the findings below actually say. Where they disagree or "
    "come up empty, say so plainly instead of smoothing it over.\n"
    "- 'say' is spoken aloud: plain sentences, no lists, no markdown, no "
    "citations, nothing that only works on a screen."
)


def _plan_questions(provider, goal: str) -> list[str]:
    """Turn the goal into a handful of searchable questions."""
    raw = provider.complete(
        (ChatMessage("system", _PLAN_SYSTEM), ChatMessage("user", f"GOAL: {goal}")),
        provider.models[0],
        max_tokens=PLAN_TOKENS, temperature=0.3, json_mode=True,
    )
    data = json.loads(raw)
    questions = data.get("questions") if isinstance(data, dict) else None
    cleaned = [str(q).strip() for q in (questions or []) if str(q).strip()]
    # A planner that returns nothing usable must not sink the job — the goal
    # itself is always a searchable question.
    return cleaned[:MAX_QUESTIONS] or [goal]


def _synthesise(provider, goal: str, findings: list[dict]) -> tuple[str, str]:
    """Fold every finding into (written answer, spoken answer)."""
    gathered = "\n\n".join(
        f"QUESTION: {f.get('question', '')}\nFOUND: {f.get('answer', '')}"
        for f in findings
    )
    raw = provider.complete(
        (ChatMessage("system", _SYNTHESIS_SYSTEM),
         ChatMessage("user", f"ORIGINAL QUESTION: {goal}\n\nFINDINGS:\n{gathered}")),
        provider.models[0],
        max_tokens=SYNTHESIS_TOKENS, temperature=0.4, json_mode=True,
    )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("synthesis did not return an object")
    answer = str(data.get("answer") or "").strip()
    spoken = str(data.get("say") or "").strip() or answer
    if not answer:
        raise ValueError("synthesis returned no answer")
    return answer, spoken[:MAX_SPOKEN_CHARS]


def run_research(ctx: JobContext):
    """One step of a research job.

    The step it takes depends entirely on what previous steps left in scratch,
    which is the whole point — a plan that cannot consume its own results is
    just a list.
    """
    provider = ctx.require("provider")
    scratch = ctx.scratch

    # ── step 0: read anything he actually pointed at ────────────────────────
    # A grounded search returns a model's summary of pages it saw, which is
    # exactly the wrong thing when he named a specific document: the paragraph
    # that answers the question is what a summary drops. So named sources are
    # fetched and read in full, one per step, before any searching.
    # Derived from the goal each time rather than stored in a setup step:
    # a whole step to run a regex is a step spent doing nothing, and a goal
    # with no links falls straight through to searching.
    from flint_core.reading import urls_in

    already_read = list(scratch.get("read") or [])
    unread = [u for u in urls_in(ctx.goal) if u not in already_read]
    if unread:
        from flint_core.reading import fetch

        url = unread[0]
        document = fetch(url)
        readings = list(scratch.get("readings") or [])
        if document.ok:
            ctx.log(f"read {document.summary_line()}")
            readings.append({"question": f"the document at {url}",
                             "answer": document.text})
        else:
            # A source that won't open is worth saying, not worth stopping
            # for — the searches can still answer the question.
            ctx.log(f"couldn't read {url}: {document.error}")
        return Continue(scratch={"read": [*already_read, url],
                                 "readings": readings}, sleep=0)

    # ── step 1: work out what to actually search for ────────────────────────
    if "questions" not in scratch:
        try:
            questions = _plan_questions(provider, ctx.goal)
        except Exception as exc:  # noqa: BLE001 — bad JSON, rate limit, anything
            return Fail(f"couldn't plan the research: {exc}", retry=True)
        ctx.log(f"planned {len(questions)} search(es): {'; '.join(questions)}")
        return Continue(scratch={"questions": questions,
                                 "findings": list(scratch.get("readings") or [])},
                        sleep=0)

    pending = list(scratch.get("questions") or [])
    findings = list(scratch.get("findings") or [])

    # ── steps 2..n: one search each, accumulating what they turn up ─────────
    if pending:
        question = pending[0]
        try:
            answer = provider.grounded_search(
                f"{question} Answer factually and briefly, with dates and "
                f"numbers where they exist. If you cannot find current "
                f"information, say exactly that."
            )
        except Exception as exc:  # noqa: BLE001 — a flaky search is not a crash
            return Fail(f"search failed: {exc}", retry=True)
        findings.append({"question": question, "answer": answer})
        ctx.log(f"searched: {question}")
        return Continue(scratch={"questions": pending[1:], "findings": findings},
                        sleep=0)

    # ── final step: fold it all into one answer ─────────────────────────────
    if not findings:
        return Fail("the research turned up nothing at all", retry=False)
    try:
        answer, spoken = _synthesise(provider, ctx.goal, findings)
    except Exception as exc:  # noqa: BLE001
        return Fail(f"couldn't write up the research: {exc}", retry=True)
    ctx.log(f"wrote up {len(findings)} finding(s)")
    return Finish(result=answer, say=spoken)


def run_agent_task(ctx: JobContext):
    """Hand a whole task to another agent and wait for it, as a job.

    A coding agent takes minutes and a conversation does not. Running one as a
    job means the user can walk away: progress lands in the job's event log as
    it happens, the budget kills anything that wedges, and she opens a fresh
    conversation with the result the way she does for research.

    One step: the agent call blocks for as long as it takes, and the kernel
    runs runners off the event loop so that is safe. The step budget is
    therefore a retry budget — a transient failure gets another go, a
    deliberate one does not.
    """
    from flint_core.agents import AgentRequest, NoAgentAvailableError

    agents = ctx.require("agents")
    try:
        result = agents.run(
            AgentRequest(goal=ctx.goal,
                         cwd=str(ctx.params.get("cwd", "")),
                         context={"asked_by": ctx.params.get("asked_by", "")},
                         on_progress=ctx.log,
                         timeout=float(ctx.params.get("timeout", 600.0))),
            task=str(ctx.params.get("task", "")),
            agent=str(ctx.params.get("agent", "")))
    except NoAgentAvailableError as exc:
        return Fail(str(exc), retry=False)

    if result.needs_input:
        # The agent is blocked on something only the user can answer. Finish
        # and ask — a job cannot hold a conversation, but she can.
        return Finish(result=result.detail or result.question,
                      say=result.question)
    if not result.ok:
        return Fail(result.error or result.summary, retry=False)

    spoken = result.spoken()
    if result.artifacts:
        shown = ", ".join(result.artifacts[:3])
        spoken = f"{spoken} Changed {shown}."
    return Finish(result=result.detail or result.summary, say=spoken)


def job_instruction(job, user_name: str) -> str:
    """The [Proactive] opening handed to the live session when a job finishes.

    Same shape as `watch_instruction` and for the same reason: she is opening
    this conversation herself, minutes or hours after he asked, and he does
    not know she is about to speak. Leading with a greeting would waste the
    one sentence that has to carry the answer.
    """
    return (
        f"[Proactive] {user_name} asked you to go and do this: {job.goal}. "
        f"You have finished, and this is what you found — {job.say} You are "
        f"opening this conversation yourself: he asked a while ago, has been "
        f"doing something else since, and does not know you're about to "
        f"speak. Lead with the actual finding in ONE or TWO short Hinglish "
        f"sentences, and remind him in the same breath that this is the thing "
        f"he asked you to look into. Do not greet him, do not ask how he is, "
        f"do not offer generic help. If he wants more, the full write-up is "
        f"yours to explain — but start with the answer."
    )


def build_registry() -> RunnerRegistry:
    """Every job type Venom knows how to run."""
    runners = RunnerRegistry()
    runners.runner(
        "research",
        description=(
            "Go away and research a question properly — several web searches, "
            "then a written answer. For anything that needs real digging "
            "rather than one quick lookup."
        ),
        # Steps are cheap and sequential here, so they run back-to-back
        # (sleep=0) rather than on the default cadence; the ceiling is the
        # step budget: plan + MAX_QUESTIONS searches + synthesis, plus slack
        # for retries on a flaky search.
        default_interval=60.0,
        max_steps=MAX_QUESTIONS + 6,
        ttl_hours=2.0,
        max_concurrent=2,
    )(run_research)
    runners.runner(
        "agent_task",
        description=(
            "Hand a whole task to another agent — the laptop assistant, or a "
            "coding agent on a repo — and report back when it's done. For "
            "work that takes minutes rather than seconds."
        ),
        # One step is the whole task, so the budget is a retry allowance.
        # Long default interval: if a step does fail, backing off beats
        # hammering a machine that may simply be asleep.
        default_interval=300.0,
        max_steps=3,
        ttl_hours=6.0,
        max_concurrent=2,
    )(run_agent_task)
    from flint_core.building import run_build

    runners.runner(
        "build",
        description=(
            "Build a working application from a description: write it, run "
            "it, and keep fixing it until it actually works. Takes many "
            "minutes."
        ),
        # Steps run back-to-back (each phase returns sleep=0), so the budget
        # is what bounds the loop: one build, then up to MAX_FIX_ATTEMPTS
        # fix/verify pairs, plus slack.
        default_interval=60.0,
        max_steps=16,
        ttl_hours=4.0,
        max_concurrent=1,      # one build at a time; each drives a coding agent
    )(run_build)
    from flint_core.deploy import run_deploy

    runners.runner(
        "deploy",
        description=(
            "Deploy a project to a configured target. Verifies it first and "
            "does a dry run unless explicitly confirmed."
        ),
        default_interval=60.0,
        max_steps=2,           # one attempt; a failed deploy is not retried blind
        ttl_hours=1.0,
        max_concurrent=1,
    )(run_deploy)
    return runners
