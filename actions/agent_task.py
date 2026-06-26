"""agent_task tool: run complex multi-step goals via the task queue.

Migrated to the self-registering interface (issue #8). Lives in its own file
so adding a tool is one new module, not edits across three places.
"""
from core.tools import tool


@tool(
    name='agent_task',
    description="Executes complex multi-step tasks requiring multiple different tools. Examples: 'research X and save to file', 'find and organize files'. DO NOT use for single commands. NEVER use for Steam/Epic — use game_updater.",
    parameters={'goal': {'type': 'STRING', 'description': 'Complete description of what to accomplish'}, 'priority': {'type': 'STRING', 'description': 'low | normal | high (default: normal)'}},
    required=['goal'],
)
def _tool_agent_task(args, ctx):
    from agent.task_queue import get_queue, TaskPriority
    pr = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL,
          "high": TaskPriority.HIGH}.get(
              str(args.get("priority", "normal")).lower(), TaskPriority.NORMAL)
    task_id = get_queue().submit(goal=args.get("goal", ""), priority=pr,
                                 speak=ctx.speak)
    return f"Task started (ID: {task_id})."
