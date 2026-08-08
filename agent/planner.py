from core.llm import get_gateway
from core.tools import PLANNER_HIDDEN, get_registry
from flint_core.llm.routing import Task
from flint_core.planning import Plan

PLANNER_PROMPT_TEMPLATE = """You are the planning module of FLINT, a personal AI assistant.
Your job: break any user goal into a sequence of steps using ONLY the tools listed below.

ABSOLUTE RULES:
- ONLY use tools from the list below. Never invent tools or write Python scripts.
- Use web_search for ANY information retrieval, research, or current data.
- Use file_controller to save content to disk.
- Use open_app or computer_control to open files and drive the system.
- Max 10 steps. Use the minimum steps needed.

USING EARLIER RESULTS:
Steps run in order and a step can use what an earlier one produced. Write
{{step:N}} anywhere in a parameter value and it is replaced by step N's actual
output before the tool runs. {{goal}} is replaced by the user's original goal.

- Only refer BACKWARDS: step 3 may use {{step:1}}, never {{step:4}}.
- Do NOT paste placeholder or invented content into a parameter that should
  hold a previous result — reference it. Writing "This file will be filled
  with research results" produces a file containing exactly that sentence.
- Combine freely: "content": "{{step:1}}\\n\\n{{step:2}}".

AVAILABLE TOOLS AND THEIR PARAMETERS:

<TOOL_DOCS>

EXAMPLES:

Goal: "research mechanical engineering and save it to a notepad file"
Steps:

web_search | query: "mechanical engineering overview definition history"
web_search | query: "mechanical engineering applications and future trends"
file_controller | action: write, path: desktop, name: mechanical_engineering.txt, content: "MECHANICAL ENGINEERING RESEARCH\n\n{{step:1}}\n\n{{step:2}}"
open_app | app_name: "Notepad"

Goal: "What is the price of Bitcoin"
Steps:

web_search | query: "Bitcoin price today USD"

Goal: "List the files on the desktop and find the largest 5 files"
Steps:

file_controller | action: list, path: desktop
file_controller | action: largest, path: desktop, count: 5

Goal: "Send John a message on WhatsApp saying there is a meeting tomorrow"
Steps:

send_message | receiver: John, message_text: "There is a meeting tomorrow", platform: WhatsApp

Goal: "Open the clock and set a reminder for 30 minutes later"
Steps:

reminder | date: [today], time: [now+30min], message: "Reminder"

OUTPUT — return ONLY valid JSON, no markdown, no explanation, no code blocks:
{
  "goal": "...",
  "steps": [
    {
      "step": 1,
      "tool": "tool_name",
      "description": "what this step does",
      "parameters": {},
      "critical": true
    }
  ]
}
"""


def planner_prompt() -> str:
    """The planner system prompt with the tool list generated live from the
    registry — the docs can never drift from what's actually dispatchable."""
    docs = get_registry().planner_documentation(exclude=PLANNER_HIDDEN)
    return PLANNER_PROMPT_TEMPLATE.replace("<TOOL_DOCS>", docs)


def _known_tools() -> set[str]:
    return set(get_registry().names())


def _ask(user_input: str) -> Plan:
    """One planning call, parsed and validated."""
    # Planning is reasoning, not bulk: breaking a goal into the right steps is
    # the one call in the chain where being wrong costs the most, so it routes
    # to the strongest configured model rather than pinning the cheapest by name.
    raw = get_gateway().chat_json(
        user_input, system=planner_prompt(), temperature=0.2, task=Task.REASONING)
    return Plan.from_dict(raw)


def create_plan(goal: str, context: str = "") -> Plan:
    """A validated plan for `goal`.

    A plan that fails validation is worth one retry with the specific problems
    fed back — "you referenced step 5, there are only 3" is a far better
    prompt than asking again identically and hoping. Discovering the same
    fault mid-execution instead would mean the first half already happened.
    """
    user_input = f"Goal: {goal}"
    if context:
        user_input += f"\n\nContext: {context}"

    try:
        plan = _ask(user_input)
        problems = plan.problems(_known_tools())
        if problems:
            print(f"[Planner] ⚠️ Plan rejected: {'; '.join(problems)}")
            plan = _ask(
                f"{user_input}\n\nYour previous plan was rejected:\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n\nProduce a corrected plan.")
            plan.validate(_known_tools())

        print(f"[Planner] ✅ Plan: {len(plan)} steps")
        print(plan.describe())
        return plan

    except Exception as e:
        print(f"[Planner] ⚠️ Planning failed: {e}")
        return _fallback_plan(goal)


def _fallback_plan(goal: str) -> Plan:
    print("[Planner] 🔄 Fallback plan")
    return Plan.from_dict({
        "goal": goal,
        "steps": [{
            "tool": "web_search",
            "description": f"Search for: {goal}",
            "parameters": {"query": goal},
            "critical": True,
        }],
    })


def replan(goal: str, completed_steps: list, failed_step, error: str) -> Plan:
    completed_summary = "\n".join(
        f"  - Step {s.step} ({s.tool}): DONE" for s in completed_steps
    )

    prompt = f"""Goal: {goal}

Already completed:
{completed_summary if completed_summary else '  (none)'}

Failed step: [{getattr(failed_step, 'tool', '?')}] {getattr(failed_step, 'description', '')}
Error: {error}

Create a REVISED plan for the remaining work only. Do not repeat completed steps.
The revised plan is numbered from 1 again, so {{{{step:N}}}} references point at
steps within THIS plan — not at the ones already done."""

    try:
        plan = _ask(prompt)
        plan.validate(_known_tools())
        print(f"[Planner] 🔄 Revised plan: {len(plan)} steps")
        return plan
    except Exception as e:
        print(f"[Planner] ⚠️ Replan failed: {e}")
        return _fallback_plan(goal)