"""Venom's standalone tool belt — everything the Pi can do with just
Wi-Fi, a headset, and cloud APIs. Registered on flint-core's ToolRegistry,
so declarations/dispatch/docs come from one definition, same as Flint.

Timers are a plain in-memory board the voice loop polls: when one fires,
Venom chimes through the headset and announces it on the next exchange.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from pathlib import Path

import requests

from flint_core.llm.providers import GeminiProvider
from flint_core.memory import MemoryStore
from flint_core.skills import (
    TimerBoard,
    fetch_weather,
    home_city,
    register_audit_tools,
    register_basic_tools,
    register_build_tools,
    register_calendar_tools,
    register_connection_tools,
    register_dev_tools,
    register_document_tools,
    register_job_tools,
    register_learning_tools,
    register_list_tools,
    register_mail_tools,
    register_memory_tools,
    register_note_tools,
    register_project_tools,
    register_recall_tools,
    register_reminder_tools,
    register_watch_tools,
)
from flint_core.tools import ToolRegistry
from venom.config import VenomConfig

log = logging.getLogger("venom.tools")

# The root control channel the console also uses: writing a keyword here makes
# the privileged venom-control unit run it as root (see provisioning/control.sh).
CONTROL_REQUEST = Path("/run/venom/control.request")


# Timers, weather, home_city and reminder-time parsing moved to
# flint_core.skills.everyday when the phone arrived — none of them was
# ever about being a Raspberry Pi. Imported above; build_briefing below
# stays here because what belongs in a spoken morning update is a
# product decision, and this device's is not the phone's.

def build_briefing(memory: MemoryStore, timers: TimerBoard,
                   now: str | None = None, location=None, reminders=None) -> str:
    """Facts for a spoken morning update — the model turns these into speech."""
    now = now or time.strftime("%A, %B %d, %Y, and it's %I:%M %p")
    parts = [f"Today is {now}."]
    city = ""
    if location is not None:
        city = (location.get() or {}).get("city") or ""
    city = city or home_city(memory)
    if city:
        try:
            parts.append("Weather — " + fetch_weather(city))
        except Exception:  # network hiccup shouldn't sink the whole briefing
            parts.append(f"(Couldn't fetch the weather for {city} right now.)")
    else:
        parts.append("(You don't know their city yet — ask where they are, "
                     "then save it with save_memory as identity/home_city.)")
    if reminders is not None:
        upcoming = reminders.pending()
        if upcoming:
            parts.append("Today's reminders: " + "; ".join(
                f"{r['text']} at "
                f"{time.strftime('%I:%M %p', time.localtime(r['due']))}"
                for r in upcoming[:5]))
    pending = timers.pending()
    if pending:
        parts.append("Running timers: " + "; ".join(
            f"{label} ({remaining:.0f} min left)" for label, remaining in pending))
    parts.append("This is the first hello of his morning. Open with it in your "
                 "usual Hinglish — warm, brief, human. Not a list, not a "
                 "weather report; just how a friend would catch him up.")
    return "\n".join(parts)


# ── volume ────────────────────────────────────────────────────────────────────
def set_alsa_volume(percent: int, card_index: int | None = None) -> str:
    percent = max(0, min(100, int(percent)))
    if platform.system() != "Linux":
        return f"Volume set to {percent}% (simulated — not on Linux)."
    cmd = ["amixer"]
    if card_index is not None:
        cmd += ["-c", str(card_index)]
    cmd += ["sset", "PCM", f"{percent}%"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        # USB headsets often expose 'Speaker' or 'Master' instead of 'PCM'
        for control in ("Speaker", "Master", "Headphone"):
            cmd[-2] = control
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                break
    if result.returncode != 0:
        return f"Could not set volume: {result.stderr.strip()[:100]}"
    return f"Volume set to {percent}%."


def set_system_volume(percent: int) -> str:
    """Absolute volume on PipeWire's default sink — the node every stream
    (voice, music, chimes) actually plays through. amixer talks to a raw ALSA
    card, which on this box is behind PipeWire and often the wrong one; wpctl
    moves the volume the user actually hears. ALSA stays as the fallback for
    a PipeWire-less dev box."""
    percent = max(0, min(100, int(percent)))
    if platform.system() != "Linux":
        return f"Volume set to {percent}% (simulated — not on Linux)."
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@",
             f"{percent / 100:.2f}"],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return f"Volume set to {percent}%."
    except (OSError, subprocess.SubprocessError):
        pass
    return set_alsa_volume(percent)


def change_system_volume(delta: int) -> str:
    """Relative volume ('thoda tez karo') on the default sink, ±percent."""
    delta = max(-100, min(100, int(delta)))
    if delta == 0:
        return "Volume unchanged."
    if platform.system() != "Linux":
        return f"Volume nudged by {delta:+d}% (simulated — not on Linux)."
    sign = "+" if delta > 0 else "-"
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@",
             f"{abs(delta)}%{sign}"],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return ("Volume up a bit." if delta > 0 else "Volume down a bit.")
    except (OSError, subprocess.SubprocessError):
        pass
    return "Could not change the volume."


# ── device health ─────────────────────────────────────────────────────────────
def _read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def device_metrics() -> dict[str, float]:
    """Raw device health numbers from /proc and /sys.

    Keys — temp_c, mem_pct, disk_pct, uptime_s — are present only when they
    could actually be read, so this returns {} on a non-Linux dev box rather
    than lying with zeros. The spoken summary below and the ambient loop's
    self-health warnings both read from here, so there is one parser."""
    out: dict[str, float] = {}

    temp = _read_file("/sys/class/thermal/thermal_zone0/temp").strip()
    if temp.isdigit():
        out["temp_c"] = int(temp) / 1000

    mem = {line.split(":")[0]: int(line.split()[1])
           for line in _read_file("/proc/meminfo").splitlines()[:5]
           if ":" in line}
    if "MemTotal" in mem and "MemAvailable" in mem and mem["MemTotal"]:
        out["mem_pct"] = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])

    try:
        st = os.statvfs("/")  # Linux-only; absent on dev boxes
        out["disk_pct"] = 100 * (1 - st.f_bavail / st.f_blocks)
    except (OSError, AttributeError, ZeroDivisionError):
        pass

    up = _read_file("/proc/uptime").split()
    if up:
        out["uptime_s"] = float(up[0])
    return out


def device_vitals() -> str:
    """A spoken-style health summary of the Pi — temperature, memory, disk,
    uptime. Anything unreadable is simply omitted, so this works (as far as
    it can) on any box."""
    metrics = device_metrics()
    parts: list[str] = []

    if "temp_c" in metrics:
        heat = " — running hot!" if metrics["temp_c"] >= 75 else ""
        parts.append(f"temperature {metrics['temp_c']:.0f}°C{heat}")
    if "mem_pct" in metrics:
        parts.append(f"memory {metrics['mem_pct']:.0f}% used")
    if "disk_pct" in metrics:
        parts.append(f"disk {metrics['disk_pct']:.0f}% full")
    if "uptime_s" in metrics:
        secs = int(metrics["uptime_s"])
        parts.append(f"up {secs // 3600}h {secs % 3600 // 60}m")

    if not parts:
        return "I couldn't read the device's health right now."
    return "Device health: " + ", ".join(parts) + "."



# ── registry ─────────────────────────────────────────────────────────────────
def build_pi_registry(config: VenomConfig, memory: MemoryStore,
                      timers: TimerBoard, music=None,
                      reminders=None, notes=None, lists=None,
                      location=None, chess=None, notifications=None,
                      receiver=None, calendar=None, mailbox=None,
                      whatsapp=None, connections=None, lights=None,
                      tv=None, watches=None, jobs=None, audit=None,
                      projects=None, outcomes=None, archive=None,
                      sos=None) -> ToolRegistry:
    reg = ToolRegistry(platform="linux")

    if calendar is not None:
        register_calendar_tools(reg, calendar)

    if mailbox is not None:
        register_mail_tools(reg, mailbox)
    if music is not None:
        @reg.tool(
            description=(
                "Plays a song, artist, album, or any music/audio from YouTube "
                "through the user's headset. Use whenever the user asks to "
                "play something. Note: playback replaces any current song."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What to play, e.g. 'Kesariya', 'lofi beats'"},
                },
                "required": ["query"],
            },
        )
        def play_music(query: str) -> str:
            return music.play(query)

        @reg.tool(
            description="Stops the currently playing music.",
        )
        def stop_music() -> str:
            return music.stop()

        @reg.tool(description="Pauses the currently playing music (resumable).")
        def pause_music() -> str:
            return music.set_paused(True)

        @reg.tool(description="Resumes paused music.")
        def resume_music() -> str:
            return music.set_paused(False)

        @reg.tool(
            description=(
                "Tells what music is currently playing, if any — including "
                "whether it's one of the user's favourites or saved offline. "
                "Use for 'what's playing?', 'kaunsa gaana hai?', 'which song is "
                "this?'."
            ),
        )
        def now_playing() -> str:
            return music.status()

        @reg.tool(
            description=(
                "Skips the current song and plays the next similar one. Use "
                "when the user says 'next', 'skip', 'agla gaana', 'change the "
                "song', or clearly dislikes what's playing."
            ),
        )
        def next_song() -> str:
            return music.skip()

        @reg.tool(
            description=(
                "Turns autoplay of similar songs on or off. When on (the "
                "default), a new similar song plays automatically after each "
                "one finishes. Use when the user asks to keep the music going, "
                "play similar songs, or to stop after the current song."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean",
                               "description": "true to keep playing similar songs, "
                                              "false to stop after this one"},
                },
                "required": ["enable"],
            },
        )
        def autoplay_similar(enable: bool) -> str:
            return music.set_autoplay(enable)

        @reg.tool(
            description=(
                "Restarts the current song from the beginning. Use when the "
                "user says 'restart', 'play it again', 'from the start', "
                "'shuru se', 'wapas se chalao'."
            ),
        )
        def restart_song() -> str:
            return music.restart()

        @reg.tool(
            description=(
                "Goes back to the previous song that played this session. Use "
                "for 'previous', 'go back', 'pichla gaana', 'last song wapas', "
                "'the one before this'."
            ),
        )
        def previous_song() -> str:
            return music.play_previous()

        @reg.tool(
            description=(
                "Saves a song to the user's favourites. With no name, favourites "
                "whatever is playing right now; with a name, finds and saves that "
                "song. Use for 'add to favourites', 'favourite this', 'ise "
                "favourite karo', 'save this song', 'I love this one'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Optional song to favourite; omit to "
                                            "favourite the current song"},
                },
            },
        )
        def add_favourite(name: str = "") -> str:
            return music.add_favourite(name)

        @reg.tool(
            description=(
                "Removes a song from the user's favourites by name. Use for "
                "'remove from favourites', 'unfavourite', 'ye favourite se hata "
                "do'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The favourite to remove (by title)"},
                },
                "required": ["name"],
            },
        )
        def remove_favourite(name: str) -> str:
            return music.remove_favourite(name)

        @reg.tool(
            description=(
                "Lists the user's favourite songs and how many are saved "
                "offline. Use for 'what are my favourites?', 'meri favourite "
                "list', 'which songs do I have saved?'."
            ),
        )
        def list_favourites() -> str:
            return music.list_favourites()

        @reg.tool(
            description=(
                "Plays through all the user's favourite songs (offline copies "
                "first, so it works with no signal), chaining to the next when "
                "each finishes. Use for 'play my favourites', 'meri favourites "
                "chalao', 'play my saved songs', 'play the songs I like'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "shuffle": {"type": "boolean",
                                "description": "Shuffle the order (default true)"},
                },
            },
        )
        def play_favourites(shuffle: bool = True) -> str:
            return music.play_favourites(shuffle)

        @reg.tool(
            description=(
                "Plays ONE specific saved favourite by name. Prefer this over "
                "play_music when the user names a song they've favourited — it "
                "needs no search, so it plays even offline with no signal (from "
                "the downloaded copy). Use for 'play my saved Kesariya', 'play "
                "that favourite', 'offline mein Kesariya chalao'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The favourite song to play, by title"},
                },
                "required": ["name"],
            },
        )
        def play_favourite(name: str) -> str:
            return music.play_favourite(name)

        @reg.tool(
            description=(
                "Downloads all of the user's favourite songs for offline play, "
                "so they keep working through dead zones with no signal — do "
                "this before a long trip. Runs in the background. Use for "
                "'download my favourites', 'save my songs offline', 'make my "
                "favourites available offline', 'offline ke liye download karo'."
            ),
        )
        def download_favourites() -> str:
            return music.download_favourites()

    if chess is not None:
        @reg.tool(
            description=(
                "Starts a new chess game the user plays by voice against you. "
                "Use when they ask to play chess. The board is tracked for you; "
                "afterwards call play_chess_move for each of their moves."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "color": {"type": "string",
                              "description": "The colour the USER plays: "
                                             "'white' or 'black'. Default white."},
                    "difficulty": {"type": "integer",
                                   "description": "Search depth 1 (easy) to 3 (hard). Default 2."},
                },
            },
        )
        def start_chess_game(color: str = "white", difficulty: int | None = None) -> str:
            return chess.new_game(color, difficulty)

        @reg.tool(
            description=(
                "Applies the user's chess move and returns your reply. Pass the "
                "move in standard algebraic notation, converting their speech: "
                "'knight to f3'->'Nf3', 'e4', 'bishop takes e5'->'Bxe5', "
                "'castle kingside'->'O-O', 'e8 promote to queen'->'e8=Q'. UCI "
                "like 'e2e4' also works. Illegal moves are rejected with a hint."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "move": {"type": "string",
                             "description": "The user's move, e.g. 'Nf3', 'e4', 'O-O', 'exd5'"},
                },
                "required": ["move"],
            },
        )
        def play_chess_move(move: str) -> str:
            return chess.human_move(move)

        @reg.tool(description="Resigns/ends the current chess game.")
        def resign_chess() -> str:
            return chess.resign()

    if notifications is not None and notifications.enabled:
        @reg.tool(
            description=(
                "Reads out the user's new phone notifications (WhatsApp). Use "
                "when they ask things like 'any messages?', 'kya notification "
                "aaya?', 'read my WhatsApp', or 'what did I miss?'. A chime "
                "already played when each one arrived; this reads them aloud and "
                "marks them seen."
            ),
        )
        def read_notifications() -> str:
            return notifications.read_unread()

    if whatsapp is not None and config.whatsapp.ready:
        @reg.tool(
            description=(
                "Sends a WhatsApp message on the user's behalf. Use when they "
                "say 'WhatsApp X to <name>', 'message <name>', 'text <name>', "
                "'reply to <name>', or after reading a message 'reply: ...'. "
                "Pass the message body as `text`, converting their spoken words "
                "into a natural written message. Set `to` to the contact's name "
                "(or a phone number); LEAVE `to` EMPTY to reply to whoever last "
                "messaged. If several contacts match a name, this asks which one "
                "— relay that and call again with a clearer name. Always confirm "
                "the message and recipient with the user before sending anything "
                "they didn't dictate word-for-word."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "The message to send"},
                    "to": {"type": "string",
                           "description": "Contact name or phone number; omit to "
                                          "reply to the most recent chat"},
                },
                "required": ["text"],
            },
        )
        def send_whatsapp(text: str, to: str = "") -> str:
            # Resolve a name against the user's own Connections first, so
            # 'message Rahul' works from saved numbers even before WhatsApp
            # syncs its contact list. A number/blank is passed through as-is.
            target = to
            if to and connections is not None:
                bare = to.strip().lstrip("+").replace(" ", "").replace("-", "")
                if not bare.isdigit():
                    num = connections.phone_for(to)
                    if num:
                        target = num
            return whatsapp.send(text, target)

        @reg.tool(
            description=(
                "Turns AUTO-REPLY mode for WhatsApp on or off. When ON, you "
                "answer NEW incoming WhatsApp messages yourself, on the user's "
                "behalf, without them dictating each reply — until they turn it "
                "off. Existing unread chats are left alone; only messages that "
                "arrive after it's switched on are answered. Use when the user "
                "says 'auto message mode on/off', 'auto reply on', 'reply to "
                "messages automatically', 'answer my WhatsApp for me', or 'stop "
                "auto replying'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean",
                               "description": "true to start auto-replying, "
                                              "false to stop"},
                },
                "required": ["enable"],
            },
        )
        def auto_reply_mode(enable: bool) -> str:
            return whatsapp.set_auto_reply(enable)

        @reg.tool(
            description=(
                "Turns on/off answering when someone writes '@jarvis' in the "
                "user's WhatsApp chats — groups included. This is ON by "
                "default and is NOT the same as auto_reply_mode: this one "
                "answers only when summoned by name, and never speaks for the "
                "user otherwise. Use for 'stop replying in my groups', "
                "'@jarvis wala reply band kar do', 'start answering when "
                "people tag you'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean",
                               "description": "true to answer mentions, false to stop"},
                },
                "required": ["enable"],
            },
        )
        def mention_reply_mode(enable: bool) -> str:
            return whatsapp.set_mention_reply(enable)

    if sos is not None:
        @reg.tool(
            description=(
                "EMERGENCY SOS. WhatsApps every saved emergency contact that "
                "the user needs help, with their approximate location and the "
                "time, then stays in emergency mode and resends the location "
                "every few minutes until it's called off. Call this the moment "
                "the user asks for emergency help — 'SOS', 'emergency', 'help "
                "me', 'bachao', 'I'm in danger', 'call for help', 'send my "
                "location to my family' — in ANY language, and act first: alert "
                "now, reassure while it sends. Do NOT call it for a drill or a "
                "hypothetical ('what would happen if…', 'test the SOS') — use "
                "emergency_contacts with action 'test' for that. If you are "
                "genuinely unsure they mean it, ask one short question first. "
                "Put anything they said about what's happening in `note` — it "
                "goes to the contacts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {"type": "string",
                             "description": "What's happening, in the user's "
                                            "own words — sent to the contacts"},
                },
            },
        )
        def emergency_sos(note: str = "") -> str:
            return sos.start(note)

        @reg.tool(
            description=(
                "Ends emergency mode and sends an 'all clear — I'm safe' "
                "WhatsApp to everyone who was alerted. Use for 'I'm safe', "
                "'cancel the SOS', 'emergency over', 'call it off', 'main "
                "theek hoon'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string",
                                "description": "Optional extra line for the "
                                               "all-clear message"},
                },
            },
        )
        def end_emergency(message: str = "") -> str:
            return sos.stop(message)

        @reg.tool(
            description=(
                "Tells whether emergency mode is on and who the SOS would "
                "alert. Use for 'is the SOS on?', 'who do you alert in an "
                "emergency?'."
            ),
        )
        def sos_status() -> str:
            return sos.status()

        @reg.tool(
            description=(
                "Manages WHO gets the emergency SOS — a contact list separate "
                "from normal messaging, so the SOS can go to different people "
                "than the user usually WhatsApps. Actions: 'add' (name, plus "
                "`to` = their phone number with country code, or the exact "
                "WhatsApp contact name; `label` = who they are, e.g. father), "
                "'remove', 'list', 'enable'/'disable' (keep someone saved but "
                "skip them), 'set_message' (custom SOS wording for that one "
                "contact — leave `name` empty to change the wording everyone "
                "gets), and 'test' (sends a clearly-marked drill to everyone so "
                "the user can confirm it works). Use for 'add my father to my "
                "emergency contacts', 'SOS mein Priya ko daal do', 'who's on my "
                "SOS list', 'test the emergency alert'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action":  {"type": "string",
                                "description": "add | remove | list | enable | "
                                               "disable | set_message | test"},
                    "name":    {"type": "string",
                                "description": "The contact's name"},
                    "to":      {"type": "string",
                                "description": "Phone number with country code, "
                                               "or exact WhatsApp contact name"},
                    "label":   {"type": "string",
                                "description": "Who they are: father, sister, "
                                               "flatmate, doctor…"},
                    "message": {"type": "string",
                                "description": "Custom SOS wording (set_message)"},
                },
                "required": ["action"],
            },
        )
        def emergency_contacts(action: str, name: str = "", to: str = "",
                               label: str = "", message: str = "") -> str:
            from venom.sos import manage_contacts

            return manage_contacts(sos, action, name=name, to=to,
                                   label=label, message=message)

    if connections is not None:
        register_connection_tools(reg, connections)
    if receiver is not None:
        @reg.tool(
            description=(
                "Makes this wearable discoverable as a Bluetooth headset named "
                "'venom' for about two minutes, so the user's laptop or phone "
                "can connect, play its audio through the earpiece, and use the "
                "earpiece mic for its calls. Use when they say 'pair my "
                "laptop', 'connect my laptop audio', 'play my phone through "
                "you', or 'bluetooth pairing on'. Tell the user exactly what "
                "this returns — it has the connection steps."
            ),
        )
        def pair_bluetooth_device() -> str:
            return receiver.open_pairing()

        @reg.tool(
            description=(
                "Reports whether a laptop or phone is currently streaming "
                "audio through the earpiece, and whether pairing is open. Use "
                "for 'is my laptop connected?', 'kya connected hai?'."
            ),
        )
        def bluetooth_audio_status() -> str:
            return receiver.status()

        @reg.tool(
            description=(
                "Disconnects the laptop/phone that is streaming audio through "
                "the earpiece. Use when the user says 'disconnect my laptop', "
                "'stop the laptop audio', 'bluetooth disconnect karo'. Does "
                "NOT touch the user's headset or stop your own music."
            ),
        )
        def disconnect_bluetooth_audio() -> str:
            return receiver.disconnect_all()

    if lights is not None:
        @reg.tool(
            description=(
                "Turns the user's smart lights ON or OFF. `where` names a room "
                "or a single light ('bedroom', 'kitchen lamp'); leave it EMPTY "
                "for every light. Use for 'lights on/off', 'turn off the "
                "bedroom', 'lights band karo', 'sab lights on kar do'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "on": {"type": "boolean",
                           "description": "true to switch on, false to switch off"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["on"],
            },
        )
        def set_lights(on: bool, where: str = "") -> str:
            return lights.power(on, where)

        @reg.tool(
            description=(
                "Sets smart-light BRIGHTNESS to an absolute level (also turns "
                "the light on). Use for 'dim the lights', 'bedroom 20%', 'full "
                "brightness', 'thoda tez/kam kar do'. For relative nudges pick a "
                "sensible new percent. `where` omitted = all lights."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "percent": {"type": "integer",
                                "description": "Brightness 1-100"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["percent"],
            },
        )
        def set_light_brightness(percent: int, where: str = "") -> str:
            return lights.brightness(percent, where)

        @reg.tool(
            description=(
                "Sets smart-light COLOUR or white tone (also turns it on). Pass "
                "a colour name — red, orange, yellow, green, teal, cyan, blue, "
                "indigo, violet, purple, magenta, pink — or a white: 'warm', "
                "'neutral', 'cool'/'daylight'. Map the user's spoken colour onto "
                "the closest of these. Use for 'make it blue', 'warm white kar "
                "do', 'lights red kar do'. `where` omitted = all lights."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "color": {"type": "string",
                              "description": "A colour name, or warm/neutral/cool white"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["color"],
            },
        )
        def set_light_color(color: str, where: str = "") -> str:
            return lights.colour(color, where)

        @reg.tool(
            description=(
                "Applies a preset lighting SCENE (colour + brightness together). "
                "Known scenes: relax, reading, movie, focus, night, party, "
                "romantic, sunset. Use when the user names a mood/scene like "
                "'movie mode', 'reading light', 'party lights', 'night mode'. "
                "`where` omitted = all lights."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene": {"type": "string",
                              "description": "relax|reading|movie|focus|"
                                             "night|party|romantic|sunset"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["scene"],
            },
        )
        def set_light_scene(scene: str, where: str = "") -> str:
            return lights.scene(scene, where)

        @reg.tool(
            description=("Lists the smart lights the user has set up, by name and "
                         "room. Use for 'what lights do you control', 'list my "
                         "lights', 'kaun kaun si lights hain'."),
        )
        def list_lights() -> str:
            return lights.list_lights()

    if tv is not None:
        @reg.tool(
            description=(
                "Turns the user's TV ON or OFF. Use for 'TV on kar do', 'turn "
                "off the TV', 'TV band karo'. Switching ON uses Wake-on-LAN and "
                "takes a few seconds, so don't immediately follow it with "
                "another TV command — give it a moment."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "on": {"type": "boolean",
                           "description": "true to switch on, false to switch off"},
                },
                "required": ["on"],
            },
        )
        def set_tv_power(on: bool) -> str:
            return tv.power(on)

        @reg.tool(
            description=(
                "Makes the TV LOUDER or QUIETER by a few steps. This is the "
                "normal way to change TV volume — use it for 'volume up', "
                "'thoda tez karo', 'turn it down', 'awaaz kam karo'. Only use "
                "set_tv_volume when the user names an exact number."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "direction": {"type": "string",
                                  "description": "'up' or 'down'"},
                    "steps": {"type": "integer",
                              "description": "How many notches, 1-30 (default 3)"},
                },
                "required": ["direction"],
            },
        )
        def nudge_tv_volume(direction: str, steps: int = 3) -> str:
            return tv.nudge_volume(direction, steps)

        @reg.tool(
            description=(
                "Sets the TV to an EXACT volume level, 0-100. Only use when the "
                "user names a number ('TV volume 20'). Not all TVs support "
                "this; if it fails, fall back to nudge_tv_volume."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "percent": {"type": "integer", "description": "Volume 0-100"},
                },
                "required": ["percent"],
            },
        )
        def set_tv_volume(percent: int) -> str:
            return tv.set_volume(percent)

        @reg.tool(
            description=(
                "Mutes or unmutes the TV. Omit `on` to simply toggle mute, "
                "which is the most reliable. Use for 'mute the TV', 'chup "
                "karao', 'unmute'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "on": {"type": "boolean",
                           "description": "true to mute, false to unmute; omit to toggle"},
                },
            },
        )
        def mute_tv(on: bool | None = None) -> str:
            return tv.mute(on)

        @reg.tool(
            description=(
                "Presses a button on the TV remote. Use for playback and "
                "navigation: play, pause, stop, forward, rewind, up, down, "
                "left, right, ok, back, home, menu, exit, source, channel up, "
                "channel down. Handles 'pause karo', 'aage badhao', 'go back', "
                "'thoda aage'. For skipping ahead, send forward a few times."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "Button name, e.g. pause|forward|ok|back|home"},
                    "times": {"type": "integer",
                              "description": "How many presses, 1-20 (default 1)"},
                },
                "required": ["key"],
            },
        )
        def press_tv_key(key: str, times: int = 1) -> str:
            return tv.press(key, times)

        @reg.tool(
            description=(
                "Opens an app on the TV: Netflix, YouTube, Prime Video, "
                "Hotstar, Disney+, Spotify, Apple TV, JioCinema, SonyLIV, Zee5 "
                "or the browser. Use for 'Netflix kholo', 'put on YouTube', "
                "'open Prime on the TV'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string",
                            "description": "App name, e.g. netflix|youtube|prime video"},
                },
                "required": ["app"],
            },
        )
        def open_tv_app(app: str) -> str:
            return tv.launch_app(app)

        @reg.tool(
            description=(
                "Tries to play a named title on a TV streaming app — 'play Dune "
                "on Netflix', 'YouTube pe lo-fi lagao'. Jumping straight to a "
                "title often isn't possible, in which case this opens the app "
                "and says so; relay that honestly instead of claiming it's "
                "playing. Default app is Netflix if the user didn't name one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string",
                              "description": "What to play, as the user said it"},
                    "app": {"type": "string",
                            "description": "App to play it on (default netflix)"},
                },
                "required": ["title"],
            },
        )
        def play_on_tv(title: str, app: str = "netflix") -> str:
            return tv.play_title(title, app)

        @reg.tool(
            description=("Lists the apps installed on the TV. Use for 'what "
                         "apps are on the TV', 'TV pe kya kya hai'."),
        )
        def list_tv_apps() -> str:
            return tv.list_apps()

        @reg.tool(
            description=("Checks whether the TV is on, in standby, or "
                         "unreachable. Use for 'is the TV on?', 'TV chalu hai?'."),
        )
        def tv_status() -> str:
            return tv.status()

    if watches is not None:
        register_watch_tools(reg, watches)
    # ── background jobs (work that outlives this conversation) ───────────────
    if jobs is not None:
        register_job_tools(reg, jobs)
    if jobs is not None:
        register_build_tools(reg, jobs, default_dir=config.build_dir)

    if config.dev.repos:
        register_dev_tools(reg, config.dev, jobs=jobs)

    if archive is not None:
        register_recall_tools(reg, archive)

    if config.documents_dir:
        register_document_tools(reg, config.documents_dir)

    if projects is not None:
        register_project_tools(reg, projects)

    if outcomes is not None:
        register_learning_tools(reg, outcomes)

    if audit is not None:
        register_audit_tools(reg, audit)

    register_basic_tools(
        reg,
        search=lambda q: GeminiProvider(
            config.gemini_api_key).grounded_search(q),
        memory=memory, timers=timers, weather=fetch_weather,
        # This body's answer to "where is he": a network lookup, city-level.
        # The phone answers the same question from GPS — same tool, different
        # source, which is the whole point of passing it in.
        current_city=(lambda: (location.get() or {}).get("city") or "")
        if location is not None else None)
    if location is not None:
        @reg.tool(
            description=("Gets the user's current approximate location (city, "
                         "region, country) from network geolocation. Use for "
                         "'where am I' and to ground local questions."),
        )
        def where_am_i() -> str:
            loc = location.get()
            if not loc:
                return "I can't determine your location right now."
            desc = ", ".join(p for p in (loc.get("city"), loc.get("region"),
                                         loc.get("country")) if p)
            return f"You appear to be in {desc}."

    @reg.tool(
        description="Sets the headset volume to an absolute level.",
        parameters={
            "type": "object",
            "properties": {
                "percent": {"type": "integer", "description": "Volume 0-100"},
            },
            "required": ["percent"],
        },
    )
    def set_volume(percent: int) -> str:
        return set_system_volume(percent)

    @reg.tool(
        description=(
            "Turns the volume up or down by a relative step. Use for 'louder', "
            "'volume badhao', 'thoda kam karo', 'turn it down a bit'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "delta": {"type": "integer",
                          "description": "Signed percent change, e.g. 10 or -10"},
            },
            "required": ["delta"],
        },
    )
    def change_volume(delta: int) -> str:
        return change_system_volume(delta)

    @reg.tool(
        description=(
            "Reports the wearable device's own health — temperature, memory, "
            "disk, uptime. Use when the user asks how the device is doing, if "
            "it's hot, or why it feels slow."
        ),
    )
    def device_status() -> str:
        return device_vitals()


    register_memory_tools(reg, memory)
    if reminders is not None:
        register_reminder_tools(reg, reminders)
    if notes is not None:
        register_note_tools(reg, notes)
    if lists is not None:
        register_list_tools(reg, lists)
    if config.screen.ready:
        @reg.tool(
            description=(
                "Reads the text currently on the user's laptop screen and returns "
                "it. Use whenever the user asks you to look at, read, check, or "
                "help debug what's on their screen — an error, code, a log, a "
                "message, anything. The laptop does local OCR and sends back the "
                "on-screen text; read it, reason over it, and answer by voice. "
                "It captures the active window, so ask the user to focus what they "
                "want you to see. Text only — you won't get colours or layout."
            ),
        )
        def look_at_screen() -> str:
            sc = config.screen
            try:
                resp = requests.get(
                    f"http://{sc.host}:{sc.port}/screen_text",
                    params={"token": sc.token}, timeout=sc.timeout)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                log.warning("look_at_screen fetch failed: %s", exc)
                return ("I couldn't reach your laptop screen — is the screen "
                        "server running and on the same network?")
            text = (data.get("text") or "").strip()
            if not text:
                return ("I looked, but there's no readable text on the active "
                        "window right now.")
            # Keep the spoken turn snappy — she doesn't need the whole essay.
            if len(text) > 4000:
                text = text[:4000] + " …(truncated)"
            return "This is the text on the screen right now:\n" + text

    if config.laptop.ready:
        @reg.tool(
            description=(
                "Runs a task ON THE USER'S LAPTOP through FLINT, the desktop "
                "assistant running there: open/close apps, play a video or "
                "song on the laptop, browser searches, files, typing, system "
                "settings — anything done at the computer. Use whenever the "
                "user asks for something 'on the laptop / computer / PC'. "
                "Pass ONE clear, self-contained instruction in English. The "
                "reply is what FLINT reports back after doing it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string",
                                "description": "The task, e.g. 'open spotify', "
                                               "'search cheap flights to Goa'"},
                },
                "required": ["command"],
            },
        )
        def laptop_task(command: str) -> str:
            from venom.laptop import run_laptop_task
            return run_laptop_task(config.laptop, command)

    if config.camera.ready:
        @reg.tool(
            description=(
                "Looks through the Raspberry Pi camera and tells the user what "
                "is in front of it. Use whenever they ask what you see, what's in "
                "front of them, to identify or read an object, count things, or "
                "any question about the physical scene — 'what do you see?', "
                "'kya dikh raha hai?', 'what is this?', 'read this label'. Pass "
                "their specific question so you look for the right thing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string",
                                 "description": "What to look for; omit for a general description"},
                },
            },
        )
        def look_around(question: str = "") -> str:
            from venom import camera
            return camera.describe_scene(config, question)

        @reg.tool(
            description=(
                "Looks for one specific thing through the camera and says "
                "whether it's there and exactly where. Use for 'where are my "
                "keys?', 'can you see my phone?', 'mera wallet dikh raha "
                "hai?'. Different from look_around, which just describes the "
                "scene — use this whenever he's looking FOR something. Report "
                "exactly what comes back, including a plain no."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "thing": {"type": "string",
                              "description": "What to look for, e.g. 'my keys'"},
                },
                "required": ["thing"],
            },
        )
        def find_object(thing: str) -> str:
            from venom import camera
            return camera.find_object(config, thing)

        @reg.tool(
            description=(
                "Takes a photo with the Raspberry Pi camera, sends it to the "
                "user's phone, and describes it aloud. Use when they say 'take a "
                "photo', 'take a shot', 'click a picture', 'photo le lo', or "
                "similar."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caption": {"type": "string",
                                "description": "Optional caption to send with the photo"},
                },
            },
        )
        def take_photo(caption: str = "") -> str:
            from venom import camera
            return camera.take_photo(config, caption)

    if config.phone.ready:
        @reg.tool(
            description=(
                "Rings the user's phone loudly to help them find it, even if "
                "it's on silent. Use for 'find my phone', 'where's my phone', "
                "'ring my phone', 'make my phone ring' — in any language."
            ),
        )
        def find_my_phone() -> str:
            from venom.phone import find_phone
            return find_phone(config.phone.ntfy_server, config.phone.ntfy_topic)

    @reg.tool(
        description=(
            "Turns live TRANSLATION (interpreter) mode on or off. Call with "
            "enable=true when the user asks to translate, says 'translation "
            "mode', 'interpreter', or 'translate karo'; enable=false when they "
            "say stop / normal / 'band karo'. In this mode you are a two-way "
            "interpreter between Hindi and Kannada/Telugu, nothing else."
        ),
        parameters={
            "type": "object",
            "properties": {
                "enable": {"type": "boolean",
                           "description": "true to start translating, false to stop"},
            },
            "required": ["enable"],
        },
    )
    def translation_mode(enable: bool) -> str:
        if enable:
            return (
                "TRANSLATION MODE ON. You are now a live two-way interpreter, not "
                "Jarvis. For every utterance from here on: if you hear Kannada or "
                "Telugu, say ONLY its Hindi translation; if you hear Hindi, say "
                "ONLY its translation in whichever of Kannada/Telugu the other "
                "person is speaking (the most recent non-Hindi language you "
                "heard). Just the translation — spoken naturally, no greetings, "
                "no commentary, no extra words. Keep going until translation mode "
                "is turned off."
            )
        return ("TRANSLATION MODE OFF. Resume being Jarvis — warm, normal "
                "conversation in your usual Hinglish.")

    @reg.tool(
        description=(
            "Ends the current conversation and returns to wake-word listening. "
            "Call when the user says goodbye, thanks you and is done, or asks "
            "you to stop listening — in any language."
        ),
    )
    def end_conversation() -> str:
        return "Ending conversation."

    @reg.tool(
        description=(
            "Cleanly powers OFF the whole device (not just the conversation). "
            "Call ONLY when the user explicitly says to power off, shut down, "
            "switch you off, or sign out for the day/night. Say a warm goodbye "
            "in your reply before this — the device shuts down right after."
        ),
    )
    def power_off() -> str:
        try:
            CONTROL_REQUEST.parent.mkdir(parents=True, exist_ok=True)
            CONTROL_REQUEST.write_text("poweroff")
        except OSError as exc:
            return f"I couldn't shut down: {exc}"
        return "Powering off. Good night, take care."

    return reg
