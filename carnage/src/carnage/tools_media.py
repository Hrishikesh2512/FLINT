"""Music, volume and the camera — the Pi's mpv and camera module, on a phone.

Venom streams music itself and owns a camera module. A phone already has a
music app he likes and a better camera than the Pi will ever have, so the
tools here drive those rather than replacing them: "play Kishore Kumar" asks
his music app to search and play, and "what am I looking at?" takes one photo
and has Gemini describe it, exactly as Venom's `look_around` does.
"""

from __future__ import annotations

import logging

from carnage.platform import MEDIA_ACTIONS, Phone

log = logging.getLogger("carnage.media")

#: Same wording as Venom's camera (venom/camera.py), so she answers in the same
#: voice whichever body is looking.
DESCRIBE_SYSTEM = ("You are the eyes of a voice assistant. Answer in one or two "
                   "short spoken sentences — no markdown, no lists, no preamble.")
DESCRIBE_DEFAULT = ("Describe what you see in front of the camera, briefly and "
                    "naturally, as if telling a friend what's there.")
FIND_SYSTEM = (
    "You are the eyes of a voice assistant, looking for one specific thing.\n\n"
    "Answer in ONE short spoken sentence, and it must do two jobs:\n"
    "- say clearly whether the thing is visible, yes or no\n"
    "- if it is, say WHERE, the way a person would point: 'on the table to "
    "your left', 'behind the laptop', 'bottom right of the shelf'\n\n"
    "Rules:\n"
    "- If you cannot see it, say so plainly. Do NOT describe what is there "
    "instead, and do NOT guess at where it 'probably' is.\n"
    "- If something merely resembles it, say that it might be, and what makes "
    "you unsure.\n"
    "- No markdown, no lists, no preamble."
)


def register_music_tools(reg, phone: Phone) -> None:

    @reg.tool(
        description=("Plays music on the phone: a song, artist, album, mood or "
                     "playlist, searched in his music app. Use for 'play "
                     "Arijit Singh', 'kuch romantic gaane bajao', 'play lofi'."),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "What to play, as he said it"}},
            "required": ["query"],
        },
    )
    def play_music(query: str) -> str:
        app = phone.play_music(query)
        if not app:
            return ("I couldn't start any music — open your music app once, "
                    "then ask me again.")
        return f"Playing {query} in {app}."

    @reg.tool(
        description=("Controls whatever is playing on the phone: pause, resume, "
                     "next, previous or stop."),
        parameters={
            "type": "object",
            "properties": {"action": {"type": "string", "enum": list(MEDIA_ACTIONS)}},
            "required": ["action"],
        },
    )
    def music_control(action: str) -> str:
        if action not in MEDIA_ACTIONS:
            return f"I can only {', '.join(MEDIA_ACTIONS)}."
        if not phone.media(action):
            return "Nothing is playing that I can control."
        return {"pause": "Paused.", "resume": "Playing again.", "next": "Next one.",
                "previous": "Going back.", "stop": "Stopped."}[action]

    @reg.tool(
        description=("Sets the phone's media volume as a percentage, or says "
                     "what it is when no level is given. 'Volume 40', "
                     "'awaaz kam karo' (about 20 lower), 'louder'."),
        parameters={
            "type": "object",
            "properties": {"percent": {"type": "integer",
                                       "description": "0 to 100"}},
        },
    )
    def set_volume(percent: int | None = None) -> str:
        level = phone.volume(percent)
        if level is None:
            return "I can't reach the volume right now."
        return f"Volume is at {level}%."


def register_camera_tools(reg, phone: Phone, describe) -> None:
    """`describe(prompt, jpeg_b64, system) -> str` is the vision call."""

    def _look(prompt: str, system: str) -> str:
        jpeg = phone.photo()
        if not jpeg:
            return ("I couldn't get a picture — the camera may be busy, or I "
                    "don't have permission to use it.")
        try:
            return describe(prompt, jpeg, system)
        except Exception as exc:            # noqa: BLE001 — network / API
            log.warning("vision call failed: %s", exc)
            return "I took a look but couldn't make sense of it just now."

    @reg.tool(
        description=("Looks through the phone's back camera and answers about "
                     "what's in view. Use for 'what am I looking at?', 'ye "
                     "kya hai?', 'read this', 'what does this say?'."),
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string",
                                        "description": "What he wants to know, if specific"}},
        },
    )
    def look_around(question: str = "") -> str:
        return _look(question.strip() or DESCRIBE_DEFAULT, DESCRIBE_SYSTEM)

    @reg.tool(
        description=("Looks through the phone's camera for one specific thing "
                     "and says whether it's there and where. 'Where are my "
                     "keys?', 'is my charger on the table?'."),
        parameters={
            "type": "object",
            "properties": {"thing": {"type": "string"}},
            "required": ["thing"],
        },
    )
    def find_thing(thing: str) -> str:
        return _look(f"Is there {thing} in view? Where?", FIND_SYSTEM)
