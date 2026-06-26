"""Gemini Live tool declarations for FLINT.

Declarations are derived from the self-registering tool registry rather than
hand maintained (issue #8). Each tool defines its own schema with the @tool
decorator in its ``actions/`` module; importing those modules populates the
registry, and ``TOOL_DECLARATIONS`` is the list of their Gemini declarations.

Adding a tool is now one new file in ``actions/`` with an @tool decorator; it
is discovered automatically here and dispatched from main.py, with no edits in
either place.
"""
from core.tools import declarations, discover

# Import every actions/ module so its @tool decorator registers the tool.
discover("actions")

TOOL_DECLARATIONS = declarations()
