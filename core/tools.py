"""FLINT's tool definitions — the single source of truth.

Every tool is registered exactly once here. From this one registry flow:
    - core.tool_registry.TOOL_DECLARATIONS   (Gemini Live function schema)
    - agent/planner tool documentation       (generated, never hand-written)
    - dispatch for main.py and the agent executor

Action modules are imported lazily on first dispatch, so importing this
module stays cheap (no pyautogui/cv2/playwright at startup or in tests).
"""

from __future__ import annotations

import importlib
import inspect
import threading

from flint_core.tools import ToolRegistry

# Tools the live session may call but the multi-step planner must not
# schedule (session-scoped, meta, or dependent on an uploaded file).
PLANNER_HIDDEN: tuple[str, ...] = (
    "agent_task", "save_memory", "shutdown_flint", "file_processor",
)

# Spoken when an action returns nothing: never assert success an action
# didn't claim.
EMPTY_RESULT_FALLBACKS: dict[str, str] = {
    "open_app": "No status returned while opening the app.",
    "send_message": "Message status unknown — could not confirm it was sent.",
    "weather_report": "Weather delivered.",
    "reminder": "Reminder set.",
}


def _action(module: str, func: str | None = None):
    """Lazy bridge to ``actions.<module>.<func>``.

    Imports the action module on first call and forwards only the context
    kwargs (player/speak/response/session_memory) its signature accepts.
    """
    func_name = func or module

    def handler(parameters=None, player=None, speak=None,
                response=None, session_memory=None):
        fn = getattr(importlib.import_module(f"actions.{module}"), func_name)
        accepted = inspect.signature(fn).parameters
        kwargs = {"parameters": parameters or {}}
        for key, value in (("player", player), ("speak", speak),
                           ("response", response), ("session_memory", session_memory)):
            if key in accepted:
                kwargs[key] = value
        return fn(**kwargs)

    handler.__name__ = func_name
    return handler


def _build_registry() -> ToolRegistry:
    reg = ToolRegistry(platform="windows")

    def tool(name, description, parameters=None, handler=None, platforms=("windows",)):
        reg.tool(
            name, description=description, parameters=parameters,
            platforms=platforms, arg_style="parameters",
        )(handler or _action(name))

    tool(
        "open_app",
        "Opens any application on the Windows computer. "
        "Use this whenever the user asks to open, launch, or start any app, "
        "website, or program. Always call this tool — never just say you opened it.",
        {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')",
                }
            },
            "required": ["app_name"],
        },
    )

    tool(
        "web_search",
        "Searches the web for any information.",
        {
            "type": "object",
            "properties": {
                "query":  {"type": "string", "description": "Search query"},
                "mode":   {"type": "string", "description": "search (default) or compare"},
                "items":  {"type": "array", "items": {"type": "string"},
                           "description": "Items to compare"},
                "aspect": {"type": "string", "description": "price | specs | reviews"},
            },
            "required": ["query"],
        },
        platforms=("any",),
    )

    tool(
        "weather_report",
        "Gives the weather report to user",
        {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        },
        handler=_action("weather_report", "weather_action"),
        platforms=("any",),
    )

    tool(
        "send_message",
        "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        {
            "type": "object",
            "properties": {
                "receiver":     {"type": "string", "description": "Recipient contact name"},
                "message_text": {"type": "string", "description": "The message to send"},
                "platform":     {"type": "string", "description": "Platform: WhatsApp, Telegram, etc."},
            },
            "required": ["receiver", "message_text", "platform"],
        },
    )

    tool(
        "reminder",
        "Sets a timed reminder using Windows Task Scheduler.",
        {
            "type": "object",
            "properties": {
                "date":    {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "string", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "string", "description": "Reminder message text"},
            },
            "required": ["date", "time", "message"],
        },
    )

    tool(
        "youtube_video",
        "Controls YouTube. Use for: playing videos, summarizing a video's content, "
        "getting video info, or showing trending videos.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "string", "description": "Search query for play action"},
                "save":   {"type": "boolean", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "string", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "string", "description": "Video URL for get_info action"},
            },
        },
    )

    def _screen_process(parameters=None, player=None, speak=None,
                        response=None, session_memory=None):
        fn = importlib.import_module("actions.screen_processor").screen_process
        fn(parameters=parameters or {}, response=None, player=player, session_memory=None)
        return ("Vision module activated. Stay completely silent — "
                "vision module will speak directly.")

    tool(
        "screen_process",
        "Captures and analyzes the screen or webcam image. "
        "MUST be called when user asks what is on screen, what you see, "
        "analyze my screen, look at camera, etc. "
        "You have NO visual ability without this tool. "
        "After calling this tool, stay SILENT — the vision module speaks directly.",
        {
            "type": "object",
            "properties": {
                "angle": {"type": "string", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "string", "description": "The question or instruction about the captured image"},
            },
            "required": ["text"],
        },
        handler=_screen_process,
    )

    tool(
        "computer_settings",
        "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
        "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
        "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
        "Use for ANY single computer control command. NEVER route to agent_task.",
        {
            "type": "object",
            "properties": {
                "action":      {"type": "string", "description": "The action to perform"},
                "description": {"type": "string", "description": "Natural language description of what to do"},
                "value":       {"type": "string", "description": "Optional value: volume level, text to type, etc."},
            },
        },
    )

    tool(
        "browser_control",
        "Controls the web browser. Use for: opening websites, searching the web, "
        "clicking elements, filling forms, scrolling, any web-based task.",
        {
            "type": "object",
            "properties": {
                "action":      {"type": "string", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | press | close"},
                "url":         {"type": "string", "description": "URL for go_to action"},
                "query":       {"type": "string", "description": "Search query for search action"},
                "selector":    {"type": "string", "description": "CSS selector for click/type"},
                "text":        {"type": "string", "description": "Text to click or type"},
                "description": {"type": "string", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "string", "description": "up or down for scroll"},
                "key":         {"type": "string", "description": "Key name for press action"},
                "incognito":   {"type": "boolean", "description": "Open in private/incognito mode"},
            },
            "required": ["action"],
        },
    )

    tool(
        "file_controller",
        "Manages files and folders: list, create, delete, move, copy, rename, read, write, "
        "find, disk usage.",
        {
            "type": "object",
            "properties": {
                "action":      {"type": "string", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "string", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "string", "description": "Destination path for move/copy"},
                "new_name":    {"type": "string", "description": "New name for rename"},
                "content":     {"type": "string", "description": "Content for create_file/write"},
                "name":        {"type": "string", "description": "File name to search for"},
                "extension":   {"type": "string", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "integer", "description": "Number of results for largest"},
            },
            "required": ["action"],
        },
    )

    tool(
        "desktop_control",
        "Controls the desktop: wallpaper, organize, clean, list, stats.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "wallpaper | wallpaper_url | current_wallpaper | organize | clean | list | stats"},
                "path":   {"type": "string", "description": "Image path for wallpaper"},
                "url":    {"type": "string", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "string", "description": "by_type or by_date for organize"},
            },
            "required": ["action"],
        },
        handler=_action("desktop", "desktop_control"),
    )

    tool(
        "code_helper",
        "Writes, edits, explains, runs, or builds code files.",
        {
            "type": "object",
            "properties": {
                "action":      {"type": "string", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "string", "description": "What the code should do or what change to make"},
                "language":    {"type": "string", "description": "Programming language (default: python)"},
                "output_path": {"type": "string", "description": "Where to save the file"},
                "file_path":   {"type": "string", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "string", "description": "Raw code string for explain"},
                "args":        {"type": "string", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "integer", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"],
        },
    )

    tool(
        "dev_agent",
        "Builds complete multi-file projects from scratch: plans, writes files, installs deps, "
        "opens VSCode, runs and fixes errors.",
        {
            "type": "object",
            "properties": {
                "description":  {"type": "string", "description": "What the project should do"},
                "language":     {"type": "string", "description": "Programming language (default: python)"},
                "project_name": {"type": "string", "description": "Optional project folder name"},
                "timeout":      {"type": "integer", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"],
        },
    )

    def _agent_task(parameters=None, player=None, speak=None,
                    response=None, session_memory=None):
        from agent.task_queue import TaskPriority, get_queue

        params = parameters or {}
        priority = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL,
                    "high": TaskPriority.HIGH}.get(
                        str(params.get("priority", "normal")).lower(),
                        TaskPriority.NORMAL)
        task_id = get_queue().submit(goal=params.get("goal", ""),
                                     priority=priority, speak=speak)
        return f"Task started (ID: {task_id})."

    tool(
        "agent_task",
        "Executes complex multi-step tasks requiring multiple different tools. "
        "Examples: 'research X and save to file', 'find and organize files'. "
        "DO NOT use for single commands.",
        {
            "type": "object",
            "properties": {
                "goal":     {"type": "string", "description": "Complete description of what to accomplish"},
                "priority": {"type": "string", "description": "low | normal | high (default: normal)"},
            },
            "required": ["goal"],
        },
        handler=_agent_task,
        platforms=("any",),
    )

    tool(
        "computer_control",
        "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, "
        "find elements on screen.",
        {
            "type": "object",
            "properties": {
                "action":      {"type": "string", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "string", "description": "Text to type or paste"},
                "x":           {"type": "integer", "description": "X coordinate"},
                "y":           {"type": "integer", "description": "Y coordinate"},
                "keys":        {"type": "string", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "string", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "string", "description": "up | down | left | right"},
                "amount":      {"type": "integer", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "number",  "description": "Seconds to wait"},
                "title":       {"type": "string",  "description": "Window title for focus_window"},
                "description": {"type": "string",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "string",  "description": "Data type for random_data"},
                "field":       {"type": "string",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "boolean", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "string",  "description": "Save path for screenshot"},
            },
            "required": ["action"],
        },
    )

    def _file_processor(parameters=None, player=None, speak=None,
                        response=None, session_memory=None):
        params = dict(parameters or {})
        if not params.get("file_path") and player is not None and player.current_file:
            params["file_path"] = player.current_file
        fn = importlib.import_module("actions.file_processor").file_processor
        return fn(parameters=params, player=player, speak=speak)

    tool(
        "file_processor",
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a "
        "command about it. If the user's command is ambiguous, pick the most logical "
        "action for that file type.",
        {
            "type": "object",
            "properties": {
                "file_path":   {"type": "string", "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."},
                "action":      {"type": "string", "description": "What to do with the file (per type: describe, ocr, summarize, extract_text, analyze, convert, trim, run, ...)"},
                "instruction": {"type": "string", "description": "Free-form instruction if action doesn't cover it"},
                "format":      {"type": "string", "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"},
                "width":       {"type": "integer", "description": "Target width for image resize"},
                "height":      {"type": "integer", "description": "Target height for image resize"},
                "scale":       {"type": "number",  "description": "Scale factor for image resize (e.g. 0.5)"},
                "quality":     {"type": "integer", "description": "Quality 1-100 for image/video compress"},
                "start":       {"type": "string",  "description": "Start time for trim: seconds or HH:MM:SS"},
                "end":         {"type": "string",  "description": "End time for trim: seconds or HH:MM:SS"},
                "timestamp":   {"type": "string",  "description": "Timestamp for video frame extraction HH:MM:SS"},
                "column":      {"type": "string",  "description": "Column name for CSV filter/sort"},
                "value":       {"type": "string",  "description": "Filter value for CSV filter"},
                "condition":   {"type": "string",  "description": "Filter condition: equals|contains|gt|lt"},
                "ascending":   {"type": "boolean", "description": "Sort order for CSV sort (default: true)"},
                "save":        {"type": "boolean", "description": "Save result to file (default: true)"},
                "destination": {"type": "string",  "description": "Output folder for archive extract"},
            },
        },
        handler=_file_processor,
    )

    def _shutdown_flint(parameters=None, player=None, speak=None,
                        response=None, session_memory=None):
        # The live session intercepts this before dispatch; this handler
        # exists so the tool is a complete registry citizen.
        return "Shutting down."

    tool(
        "shutdown_flint",
        "Shuts down the assistant completely. "
        "Call this when the user expresses intent to end the conversation, "
        "close the assistant, say goodbye, or stop Flint. "
        "The user can say this in ANY language.",
        handler=_shutdown_flint,
        platforms=("any",),
    )

    def _save_memory(parameters=None, player=None, speak=None,
                     response=None, session_memory=None):
        from memory.memory_manager import update_memory

        params = parameters or {}
        category = params.get("category", "notes")
        key, value = params.get("key", ""), params.get("value", "")
        if key and value:
            update_memory({category: {key: {"value": value}}})
            return "ok"
        return "nothing to save"

    tool(
        "save_memory",
        "Save an important personal fact about the user to long-term memory. "
        "Call this silently whenever the user reveals something worth remembering: "
        "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
        "Do NOT call for: weather, reminders, searches, or one-time commands. "
        "Do NOT announce that you are saving — just call it silently. "
        "Values must be in English regardless of the conversation language.",
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    ),
                },
                "key":   {"type": "string", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "string", "description": "Concise value in English (e.g. Tushar, pizza, older sister)"},
            },
            "required": ["category", "key", "value"],
        },
        handler=_save_memory,
        platforms=("any",),
    )

    return reg


_registry: ToolRegistry | None = None
_lock = threading.Lock()


def get_registry() -> ToolRegistry:
    global _registry
    with _lock:
        if _registry is None:
            _registry = _build_registry()
        return _registry
