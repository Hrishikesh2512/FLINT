"""Reserved built-in tools: save_memory and shutdown_flint.

Both touch app internals (long-term memory, process shutdown) and are handled
inline in main._execute_tool, which intercepts them by name before dispatch.
They register here only so their Gemini declarations live in the same
self-registering registry as every other tool (issue #8). Their handlers are
never called; they raise if they somehow are, to make a missing intercept loud.
"""
from core.tools import tool


@tool(
    name='save_memory',
    description='Save an important personal fact about the user to long-term memory. Call this silently whenever the user reveals something worth remembering: name, age, city, job, preferences, hobbies, relationships, projects, or future plans. Do NOT call for: weather, reminders, searches, or one-time commands. Do NOT announce that you are saving — just call it silently. Values must be in English regardless of the conversation language.',
    parameters={'category': {'type': 'STRING', 'description': 'identity — name, age, birthday, city, job, language, nationality | preferences — favorite food/color/music/film/game/sport, hobbies | projects — active projects, goals, things being built | relationships — friends, family, partner, colleagues | wishes — future plans, things to buy, travel dreams | notes — habits, schedule, anything else worth remembering'}, 'key': {'type': 'STRING', 'description': 'Short snake_case key (e.g. name, favorite_food, sister_name)'}, 'value': {'type': 'STRING', 'description': 'Concise value in English (e.g. Fatih, pizza, older sister)'}},
    required=['category', 'key', 'value'],
)
def _tool_save_memory(args, ctx):
    raise RuntimeError(
        "save_memory is a reserved tool handled in main._execute_tool")


@tool(
    name='shutdown_flint',
    description='Shuts down the assistant completely. Call this when the user expresses intent to end the conversation, close the assistant, say goodbye, or stop Flint. The user can say this in ANY language.',
    parameters={},
    required=[],
)
def _tool_shutdown_flint(args, ctx):
    raise RuntimeError(
        "shutdown_flint is a reserved tool handled in main._execute_tool")
