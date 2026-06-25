# actions/example_tool.py
# Minimal example tool for new contributors.
# Copy this file to create your own tool — see CONTRIBUTING.md for the full guide.

from datetime import datetime


def example_tool(parameters: dict, **_kwargs) -> str:
    """Returns the current date and time.

    This is the simplest possible FLINT tool: a single function that takes a
    ``parameters`` dict and returns a string.  The Live API speaks the result
    automatically — no extra wiring needed.

    To build your own tool:
        1. Copy this file to ``actions/<your_tool>.py``
        2. Write a function that accepts ``parameters`` (dict) and returns a string
        3. Add a declaration in ``core/tool_registry.py``
        4. Import and wire it in ``main.py`` (see the ``_TOOL_MAP`` dict)
    """

    now = datetime.now()
    fmt = parameters.get("format", "friendly")

    if fmt == "iso":
        return now.isoformat(timespec="seconds")
    if fmt == "time":
        return now.strftime("%H:%M:%S")
    if fmt == "date":
        return now.strftime("%A, %B %d, %Y")

    # Default: friendly format
    return now.strftime("%A, %B %d, %Y — %I:%M %p")
