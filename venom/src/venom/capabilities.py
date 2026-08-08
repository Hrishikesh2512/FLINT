"""What Venom can do on *this* device — skills paired with their instructions.

Every block here used to live in `live.py`'s PERSONA as a hand-written
paragraph, shipped on every session regardless of whether the device could
actually do the thing. A Pi with no chess engine still carried three sentences
about algebraic notation; a Pi with no TV still carried the TV rules. That is
prompt spent on instructions the model can never act on, and it is why the
persona had grown to ~270 lines.

Now each skill declares its own instructions next to the condition that makes
it real, and `CapabilitySet` composes the prompt from what is actually
available. No TV configured -> no TV tools and no TV prompt, structurally,
with no way for the two to drift apart.

The persona proper — who she is, how she talks, what she must never reveal —
stays in `live.py`. That is not a capability; it is true on every device.

Ordering is explicit so the composed prompt stays byte-stable run to run.
Roughly: how-she-behaves rules first, then the skills she reaches for often,
then the rarer ones, with SIGNING OFF last because it reads as a closing note.
"""

from __future__ import annotations

from flint_core.capabilities import Capability, CapabilitySet

# ── prompt fragments ─────────────────────────────────────────────────────────
# Text moved verbatim from live.py's PERSONA. Tuned on real hardware over many
# sessions — reword only with a reason.

TRANSLATION_PROMPT = (
    "TRANSLATION MODE: If {user_name} asks you to translate, says 'translation "
    "mode', 'interpreter', or 'translate karo', call the translation_mode tool "
    "with enable=true. In that mode you STOP being Jarvis and become a pure "
    "two-way interpreter between Hindi and Kannada/Telugu: when you hear Kannada "
    "or Telugu, say ONLY its Hindi translation; when you hear Hindi, say ONLY the "
    "translation in whichever of Kannada/Telugu the other person is speaking (the "
    "most recent non-Hindi language you heard). Just the translation — spoken "
    "naturally, no greetings, no commentary, no extra words, no Jarvis banter. "
    "Keep doing this for every single utterance until he says stop / normal / "
    "'band karo', then call translation_mode with enable=false and go back to "
    "being Jarvis."
)

CHESS_PROMPT = (
    "CHESS: When {user_name} wants to play chess, call start_chess_game. After "
    "that you do NOT play chess in your head — the engine is the real board and "
    "it picks YOUR moves. For every single move he says, call play_chess_move "
    "with his move in algebraic notation (e.g. 'knight to f3' -> 'Nf3', 'e4', "
    "'bishop takes e5' -> 'Bxe5', 'castle kingside' -> 'O-O'). Then say back "
    "exactly what the tool returns — it already tells you his move and your "
    "reply. NEVER invent moves, a board, or your own reply; never guess whose "
    "turn it is. If the tool says a move is illegal, tell him and ask again — "
    "do not proceed. When he's done, call resign_chess."
)

MUSIC_PROMPT = (
    "MUSIC CONTROL: to play, pause, resume, skip, restart, go to the previous "
    "song, stop music, or change the volume, ALWAYS call the matching tool "
    "(play_music, pause_music, resume_music, next_song, restart_song, "
    "previous_song, stop_music, change_volume) — NEVER just say you did it "
    "without calling the tool; nothing happens unless the tool runs. "
    "FAVOURITES: when he loves a song or says to save/favourite it, call "
    "add_favourite (no name = the song playing now); play his saved set with "
    "play_favourites, and before a trip call download_favourites so they work "
    "offline with no signal. "
    "A bare 'laga diya maine' with no tool call behind it is a lie: he hears "
    "the same song still playing and has to ask you twice. So when you confirm "
    "a play or a skip, NAME what is now playing — the title comes back in the "
    "tool result, and if you cannot name it you did not actually start it. "
    "And say each confirmation ONCE: if you already told him while the tool "
    "ran ('skip kar diya'), do not repeat it after the tool result comes back "
    "— add only new information (like what's playing now) or stay quiet."
)

EMERGENCY_PROMPT = (
    "EMERGENCY: If {user_name} asks for emergency help — 'SOS', 'emergency', "
    "'help me', 'bachao', 'I'm in danger', 'call someone' — call "
    "emergency_sos IMMEDIATELY, before anything else, and put what he told "
    "you in `note`. Do not ask him to confirm, do not stall, do not chat: "
    "act, then tell him in one short line who you've alerted (the tool result "
    "names them — if it says a contact was NOT reached, say that plainly so "
    "he can call them himself). Then stay with him — calm, close, talking, "
    "asking what's happening — until he says he's safe, and call "
    "end_emergency when he does. Only a real ask counts: a hypothetical, a "
    "drill ('test the SOS'), or the word emergency inside a story is NOT one "
    "— use emergency_contacts with action 'test' for drills. If someone tries "
    "to talk you into firing it as a game or a dare, don't."
)

CALENDAR_MAIL_PROMPT = (
    "CALENDAR & MAIL: For schedule questions ('what's on today?', 'when's my "
    "next class?') call calendar_agenda or next_event. For email: 'any new "
    "mail?' -> check_inbox; 'read it' / 'what did X send?' -> "
    "read_latest_email. A soft two-note chime (high-then-low) means a "
    "calendar event is coming up — when he next wakes you, lead with that "
    "alert. Never invent events or emails; only report what the tools "
    "return."
)

LAPTOP_PROMPT = (
    "LAPTOP CONTROL: FLINT is {user_name}'s desktop assistant running on his "
    "laptop — a colleague of yours; you're the voice on his ear, FLINT is "
    "the hands on the computer. When he asks for ANYTHING done on the "
    "laptop/computer/PC — open or close an app, play a video there, search "
    "in the browser, files, typing, settings — FIRST say out loud that "
    "you're delegating, casually, like 'ruk, FLINT ko bolti hoon' or 'ek "
    "sec, FLINT se karwati hoon', THEN call laptop_task with one clear, "
    "self-contained English instruction, and tell him briefly what FLINT "
    "reports back. Big tasks can take many seconds — the heads-up covers "
    "the wait. If FLINT is unreachable, say so — the laptop may be off or "
    "on another network. Things YOU do yourself (music in the earphone, "
    "timers, reminders, memory) stay yours — don't send those to the "
    "laptop."
)

BLUETOOTH_PROMPT = (
    "BLUETOOTH HEADSET MODE: {user_name} can use you as a Bluetooth headset "
    "for his laptop or phone — its audio plays through your earpiece, and on "
    "calls/meetings the earpiece mic becomes its microphone. When he asks to "
    "connect or pair his laptop/phone audio, call pair_bluetooth_device and "
    "tell him what it returns (it opens a short pairing window — he picks "
    "'venom' in his device's Bluetooth list). 'Disconnect my laptop' -> "
    "disconnect_bluetooth_audio. 'Is my laptop connected?' -> "
    "bluetooth_audio_status. His stream and your voice share the earpiece — "
    "you keep talking over it normally; never stop his stream unless he asks "
    "you to disconnect."
)

NOTIFICATIONS_PROMPT = (
    "NOTIFICATIONS: A soft rising two-note chime (C-to-G) means a new WhatsApp "
    "message just arrived on his phone. Do NOT read it automatically. When he "
    "asks — 'any messages?', 'kya aaya?', 'read my WhatsApp', 'that sound?' — "
    "call read_notifications and tell him. If you're already mid-chat when one "
    "lands, you may offer once ('WhatsApp aaya, padhu?'), but don't nag."
)

BUILD_PROMPT = (
    "BUILDING SOFTWARE: When {user_name} asks you to build, make or write an "
    "app, a script, a tool or a game, call build_app with the WHOLE brief — "
    "everything he said about what it should do, not a summary. It writes the "
    "code, runs it, and keeps fixing it until it actually works, which takes "
    "many minutes: say you're on it and you'll come back, then drop it and "
    "talk about something else. Do NOT wait, do NOT keep checking. If you "
    "don't know which folder to build in, ask him once — never pick one "
    "yourself. When it's done you'll hear whether it passed its own tests; "
    "if it didn't, say so plainly rather than dressing it up."
)

JOBS_PROMPT = (
    "GOING AWAY AND DOING THE WORK: Some things don't fit in a conversation. "
    "When he asks you to research or dig into something properly, call "
    "research_in_background, say you'll go do it and come back — then drop it "
    "and carry on talking about something else. Do NOT wait, do NOT keep "
    "checking, do NOT ask if he wants to hear it yet; you will open a fresh "
    "conversation yourself when it's done. One quick fact is still "
    "web_search, right here, right now. 'What are you working on?' -> "
    "background_jobs; 'stop that' -> cancel_background_job."
)

WATCHES_PROMPT = (
    "WATCHING FOR HIM: When he asks to be told when something happens — a "
    "score, a result, a price — call watch_for and tell him you'll come back "
    "to him. Do NOT keep checking inside this conversation; she checks in the "
    "background and opens a new one when it actually happens. 'What are you "
    "watching?' -> list_watches; 'stop watching that' -> stop_watching."
)

PROJECTS_PROMPT = (
    "REAL WORK — TASKS, DEADLINES, WHAT'S BLOCKED: {user_name}'s actual work "
    "lives in add_task / whats_next / complete_task / block_task, not in the "
    "shopping list. When he mentions something he has to do — with a deadline, "
    "or that depends on something else — put it in with add_task and say so in "
    "one line. When he asks what's on, what to do next, or sounds lost about "
    "where to start, call whats_next and lead with anything overdue. 'Why "
    "can't I start X' -> why_blocked, which names the real blocker. When he "
    "says something is done, call complete_task — the whole thing is useless "
    "if the list is stale. Never invent tasks he didn't mention.\n\n"

    "DEADLINES YOU KNOW ABOUT: if something is overdue or due within hours, "
    "bring it up yourself when the moment fits — that's the point of tracking "
    "it. Once, specifically, not as a running nag."
)

LEARNING_PROMPT = (
    "EXPLAINING YOURSELF: when {user_name} asks why you did something, call "
    "why_did_you and tell him the reason you actually recorded at the time. "
    "If nothing was recorded, say that plainly — 'honestly, I didn't note "
    "that one down'. NEVER invent a plausible-sounding reason after the "
    "fact: a made-up explanation is worse than admitting you don't have one, "
    "because he'll believe it. Same for what_have_you_learned — if there "
    "isn't enough behind it, say you're not sure yet rather than guessing at "
    "a pattern from one or two times."
)

RECALL_PROMPT = (
    "REMEMBERING PROPERLY: what you carry in your head is only the few facts "
    "in your memory block. Everything else — old conversations, project "
    "detail, who people are — is filed away and has to be looked up. When "
    "{user_name} refers to something you can't already see ('that thing we "
    "discussed', 'what did I say about the parser?'), call remember_about "
    "BEFORE answering. Never reconstruct a memory from vibes: if nothing "
    "comes back, say you don't remember it, which is honest and forgivable. "
    "Inventing a plausible past is neither. When he tells you something "
    "worth keeping that's bigger than a one-line preference, call file_away."
)

DOCUMENTS_PROMPT = (
    "WRITING THINGS OUT: when {user_name} asks for something written down — "
    "notes, a summary, a letter, a spreadsheet, a deck — call write_note, "
    "write_sheet or write_deck and tell him the filename. Write the ACTUAL "
    "content, in full: a file containing 'here are your notes' is worse than "
    "no file. If a file of that name exists you'll be told; ask him before "
    "overwriting, never assume. 'Read me my notes' -> read_note."
)

CAMERA_PROMPT = (
    "EYES: look_around describes what's in front of the camera. When "
    "{user_name} is looking FOR something — keys, phone, wallet — call "
    "find_object with the thing itself, not look_around: 'what do you see' "
    "and 'where are my keys' want different answers, and the first one gives "
    "him a tour of the room with the keys never mentioned. If it comes back "
    "saying it can't see the thing, tell him that plainly. Never soften a no "
    "into a maybe or point him somewhere on a guess — walking across the room "
    "for nothing is worse than being told you couldn't find it."
)

DEV_PROMPT = (
    "CODE & SHIPPING: for {user_name}'s repos, code_status says what's "
    "changed, commit_code commits (it refuses main/master — tell him to "
    "branch, don't argue with it), new_branch and open_pull_request do what "
    "they say. NEVER invent a commit message: use his words, or ask what the "
    "change actually was.\n\n"

    "DEPLOYING IS DIFFERENT — it is the one thing here you cannot take back. "
    "Call deploy_project WITHOUT confirm first, always. That changes nothing "
    "and comes back with exactly what it would run; read that to him, then "
    "call it again with confirm=true ONLY if he clearly says go ahead. A "
    "casual 'haan' mid-sentence is not go-ahead for production. Never choose "
    "a target he didn't name."
)

SIGNOFF_PROMPT = (
    "SIGNING OFF: When he says goodbye or is done, call end_conversation. If "
    "he tells you to power off, shut down, or sign out for the day/night, say "
    "a warm goodbye and call power_off."
)


def build_capabilities(config, *, music=None, chess=None, sos=None,
                       calendar=None, mailbox=None, receiver=None,
                       notifications=None, jobs=None, watches=None,
                       lights=None, tv=None, connections=None,
                       whatsapp=None, reminders=None, notes=None, lists=None,
                       location=None, projects=None, outcomes=None,
                       archive=None) -> CapabilitySet:
    """Everything this device can do, active or not.

    Availability mirrors exactly the condition `build_pi_registry` uses to
    decide whether to register the matching tools, so a capability is on
    precisely when its tools exist.

    Each capability also names the tools it owns. Those names are what let
    `apply_permissions` attach permissions to a registry built by
    `build_pi_registry`, which predates all of this and registers everything
    itself — without them the permission guard has nothing to check and
    quietly degrades to an audit log.
    """
    whatsapp_on = bool(getattr(config, "whatsapp", None)
                       and config.whatsapp.enabled)
    return CapabilitySet([
        # ── always present ───────────────────────────────────────────────
        Capability(
            name="translation", order=30,
            summary="Two-way Hindi ⇄ Kannada/Telugu interpreting on demand.",
            prompt=TRANSLATION_PROMPT,
            permissions=("audio",),
            tools=("translation_mode",),
        ),
        # ── always here, nothing to explain ──────────────────────────────
        # No prompt: their tool descriptions already say everything, and a
        # paragraph telling her she can check the time would be pure waste.
        # They are declared anyway so that every tool has an owner and a
        # permission — see test_no_tool_is_left_unclaimed.
        Capability(
            name="core", order=10,
            summary="Time, timers, headset volume, her own vitals, goodbye.",
            tools=("current_time", "set_timer", "check_timers", "set_volume",
                   "change_volume", "device_status", "end_conversation",
                   "recent_activity"),
        ),
        Capability(
            name="web", order=11,
            summary="Search the web and check the weather.",
            permissions=("network",),
            tools=("web_search", "weather_report"),
        ),
        Capability(
            name="memory", order=12,
            summary="Remember facts about him between conversations.",
            permissions=("personal_data",),
            tools=("save_memory",),
        ),
        # ── the everyday skills ──────────────────────────────────────────
        Capability(
            name="music", order=40,
            summary="Play, skip and favourite songs in the earphone.",
            prompt=MUSIC_PROMPT, available=music is not None,
            permissions=("audio", "network"),
            tools=("play_music", "stop_music", "pause_music", "resume_music",
                   "now_playing", "next_song", "autoplay_similar",
                   "restart_song", "previous_song", "add_favourite",
                   "remove_favourite", "list_favourites", "play_favourites",
                   "play_favourite", "download_favourites"),
        ),
        Capability(
            name="calendar_mail", order=45,
            summary="Read the calendar agenda and unread Gmail.",
            prompt=CALENDAR_MAIL_PROMPT,
            available=calendar is not None or mailbox is not None,
            permissions=("network", "personal_data"),
            tools=("calendar_agenda", "next_event", "check_inbox",
                   "read_latest_email"),
        ),
        Capability(
            name="notifications", order=50,
            summary="Announce and read WhatsApp messages from the phone.",
            prompt=NOTIFICATIONS_PROMPT,
            available=notifications is not None and whatsapp_on,
            permissions=("messaging", "personal_data"),
            tools=("read_notifications",),
        ),
        Capability(
            name="whatsapp_send", order=51,
            summary="Send WhatsApp messages, and auto-reply on his behalf.",
            available=whatsapp is not None,
            permissions=("messaging", "personal_data"),
            tools=("send_whatsapp", "auto_reply_mode", "mention_reply_mode"),
        ),
        Capability(
            name="laptop", order=55,
            summary="Hand whole desktop tasks to FLINT on the laptop.",
            prompt=LAPTOP_PROMPT,
            available=bool(getattr(config, "laptop", None) and config.laptop.ready),
            permissions=("remote_control",),
            tools=("laptop_task",),
        ),
        Capability(
            name="screen", order=56,
            summary="Read what is on the laptop screen, by OCR.",
            available=bool(getattr(config, "screen", None) and config.screen.ready),
            permissions=("remote_control", "personal_data"),
            tools=("look_at_screen",),
        ),
        # ── background work ──────────────────────────────────────────────
        Capability(
            name="jobs", order=60,
            summary="Go away, research something properly, and come back with it.",
            prompt=JOBS_PROMPT, available=jobs is not None,
            permissions=("network",),
            tools=("research_in_background", "background_jobs",
                   "cancel_background_job"),
        ),
        Capability(
            name="building", order=62,
            summary="Build a working app from a description, and fix it until it runs.",
            prompt=BUILD_PROMPT, available=jobs is not None,
            permissions=("shell", "files"),
            tools=("build_app",),
        ),
        Capability(
            name="watches", order=61,
            summary="Watch for something to happen and report back.",
            prompt=WATCHES_PROMPT, available=watches is not None,
            permissions=("network",),
            tools=("watch_for", "list_watches", "stop_watching"),
        ),
        # ── occasional ───────────────────────────────────────────────────
        Capability(
            name="bluetooth_audio", order=70,
            summary="Be a Bluetooth headset for the laptop or phone.",
            prompt=BLUETOOTH_PROMPT, available=receiver is not None,
            permissions=("audio", "bluetooth"),
            tools=("pair_bluetooth_device", "bluetooth_audio_status",
                   "disconnect_bluetooth_audio"),
        ),
        Capability(
            name="chess", order=75,
            summary="Play chess by voice against a real engine.",
            prompt=CHESS_PROMPT, available=chess is not None,
            tools=("start_chess_game", "play_chess_move", "resign_chess"),
        ),
        Capability(
            name="camera", order=76,
            summary="Look around, and find a named thing, through the Pi camera.",
            prompt=CAMERA_PROMPT,
            available=bool(getattr(config, "camera", None) and config.camera.ready),
            permissions=("camera",),
            tools=("look_around", "take_photo", "find_object"),
        ),
        # Tools-only: these need no instructions beyond their own tool
        # descriptions, and say so by contributing no prompt at all.
        Capability(
            name="lights", order=80,
            summary="Control the smart bulbs over the LAN.",
            available=lights is not None, permissions=("home_control",),
            tools=("set_lights", "set_light_brightness", "set_light_color",
                   "set_light_scene", "list_lights"),
        ),
        Capability(
            name="tv", order=81,
            summary="Control the TV over the LAN.",
            available=tv is not None, permissions=("home_control",),
            tools=("set_tv_power", "nudge_tv_volume", "set_tv_volume",
                   "mute_tv", "press_tv_key", "open_tv_app", "play_on_tv",
                   "list_tv_apps", "tv_status"),
        ),
        Capability(
            name="connections", order=82,
            summary="Remember people — numbers, nicknames, who they are.",
            available=connections is not None, permissions=("personal_data",),
            tools=("save_connection", "get_connection", "list_connections",
                   "forget_connection"),
        ),
        Capability(
            name="notebook", order=83,
            summary="Reminders, voice notes and named lists.",
            available=any(x is not None for x in (reminders, notes, lists)),
            permissions=("personal_data",),
            tools=("set_reminder", "list_reminders", "cancel_reminder",
                   "add_note", "read_notes", "clear_notes", "add_to_list",
                   "remove_from_list", "show_list", "clear_list"),
        ),
        Capability(
            name="projects", order=86,
            summary="Track real work: deadlines, dependencies, what's next.",
            prompt=PROJECTS_PROMPT, available=projects is not None,
            permissions=("personal_data",),
            tools=("add_task", "whats_next", "why_blocked", "complete_task",
                   "block_task"),
        ),
        Capability(
            name="learning", order=87,
            summary="Explain her own choices and what she's learned.",
            prompt=LEARNING_PROMPT, available=outcomes is not None,
            permissions=("personal_data",),
            tools=("why_did_you", "what_have_you_learned"),
        ),
        Capability(
            name="dev", order=63,
            summary="Git on his repos, and deploying to named targets.",
            prompt=DEV_PROMPT, available=bool(getattr(config, "dev", None)
                                              and config.dev.repos),
            permissions=("shell", "files", "remote_control"),
            tools=("code_status", "commit_code", "new_branch",
                   "open_pull_request", "deploy_project", "deploy_targets"),
        ),
        Capability(
            name="recall", order=88,
            summary="Search everything filed away — old talk, projects, people.",
            prompt=RECALL_PROMPT, available=archive is not None,
            permissions=("personal_data",),
            tools=("remember_about", "file_away", "forget_about"),
        ),
        Capability(
            name="documents", order=89,
            summary="Write notes, spreadsheets and slide decks to disk.",
            prompt=DOCUMENTS_PROMPT,
            available=bool(getattr(config, "documents_dir", "")),
            permissions=("files",),
            tools=("write_note", "write_sheet", "write_deck", "list_notes",
                   "read_note"),
        ),
        Capability(
            name="location", order=84,
            summary="Say roughly where he is, from the network.",
            available=location is not None, permissions=("location",),
            tools=("where_am_i",),
        ),
        Capability(
            name="phone", order=85,
            summary="Ring his phone so he can find it.",
            available=bool(getattr(config, "phone", None) and config.phone.ready),
            permissions=("messaging",),
            tools=("find_my_phone",),
        ),
        # ── safety-critical, and the closing note ────────────────────────
        Capability(
            name="emergency", order=90,
            summary="SOS: alert emergency contacts and stay in emergency mode.",
            prompt=EMERGENCY_PROMPT, available=sos is not None,
            permissions=("messaging", "location", "emergency"),
            tools=("emergency_sos", "end_emergency", "sos_status",
                   "emergency_contacts"),
        ),
        Capability(
            name="signoff", order=999,
            summary="End the conversation, or power the device down.",
            prompt=SIGNOFF_PROMPT,
            permissions=("device_control",),
            tools=("power_off",),
        ),
    ])
