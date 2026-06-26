# actions/example_tool.py
# Minimal example tool for new contributors.
# This is the canonical template: one file, one @tool decorator, nothing else.

from datetime import datetime

from core.tools import tool


@tool(
    name='example_tool',
    description='Returns the current date and time. A minimal example tool for contributors — copy actions/example_tool.py to build your own.',
    parameters={'format': {'type': 'STRING', 'description': 'friendly (default) | iso | time | date'}},
    required=[],
)
def example_tool(args, ctx):
    """Returns the current date and time.

    The simplest possible FLINT tool. A handler takes ``args`` (the parsed
    parameters dict) and ``ctx`` (shared services: ``ctx.player``/``ctx.ui``,
    ``ctx.speak``, ``ctx.pipeline``) and returns the string the assistant
    speaks. The Live API speaks the result automatically.

    To build your own tool:
        1. Copy this file to ``actions/<your_tool>.py``
        2. Give it a unique ``name`` in @tool, fill in the schema, and write
           the ``(args, ctx)`` handler
        3. That is all. It is auto-discovered into the registry and dispatched
           by main.py. No edits to core/tool_registry.py or main.py needed.
    """

    now = datetime.now()
    fmt = args.get("format", "friendly")

    if fmt == "iso":
        return now.isoformat(timespec="seconds")
    if fmt == "time":
        return now.strftime("%H:%M:%S")
    if fmt == "date":
        return now.strftime("%A, %B %d, %Y")

    # Default: friendly format
    return now.strftime("%A, %B %d, %Y — %I:%M %p")
