"""Gemini Live tool declarations — generated from the single tool registry.

Definitions live in core/tools.py; this module keeps the import surface
main.py has always used. The Live API's Schema proto wants uppercase type
names, hence uppercase_types=True.
"""

from core.tools import get_registry

TOOL_DECLARATIONS = get_registry().gemini_declarations(uppercase_types=True)
