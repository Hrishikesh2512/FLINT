# Venom — developer workflow & command reference

Everything you need to change Venom's behaviour yourself: where the "prompt
commands" live, what every voice tool does, the config knobs, and the exact
edit → test → deploy loop to the Pi.

---

## 1. The big picture (how a spoken turn flows)

```
wake word / wake button
      │
      ▼
LiveSession opens  ──►  system prompt (persona + directives) sent once
      │                  (src/venom/live.py : SYSTEM_PROMPT)
      ▼
you speak  ──►  Gemini native-audio model  ──►  decides to call a tool
                                                     │
                                                     ▼
                                        registry.dispatch(name, args)
                                        (src/venom/tools_pi.py)
                                                     │
                                                     ▼
                                        tool returns text ──► she speaks it
```

Two things to hold separate:

- **The system prompt** = her personality + hard rules ("prompt commands"). Set
  **once when a conversation opens**. Lives in `src/venom/live.py`.
- **The tools** = the actual actions (music, chess, timers…). Registered **once
  at startup**, reused for every conversation. Live in `src/venom/tools_pi.py`.

State objects (chess board, music player, timers, memory) live on the
**orchestrator** (`src/venom/voice.py`), so they **persist across wake
sessions** — a game or a playlist survives the conversation ending.

---

## 2. File map (what to edit for what)

| Want to change…                     | Edit this file |
|-------------------------------------|----------------|
| Her personality / rules / directives | `src/venom/live.py` → `SYSTEM_PROMPT` |
| Add / change a voice tool            | `src/venom/tools_pi.py` → `build_pi_registry` |
| A stateful feature (music, chess…)   | its own module: `music.py`, `chess_game.py`, `stores.py` |
| Wire a new feature into startup      | `src/venom/voice.py` → `VoiceOrchestrator.__init__` |
| Config schema / defaults             | `src/venom/config.py` |
| Web dashboard (the browser console)  | `src/venom/web.py` |
| Physical buttons                     | `src/venom/buttons.py` |
| Wake word / mic / audio              | `wake.py`, `audio/`, `live.py` |

---

## 3. "Prompt commands" — the system prompt

All of her behaviour rules are one big string in
**`src/venom/live.py`**, the `SYSTEM_PROMPT`. It's organised into labelled
blocks — edit the block, redeploy, done. Current blocks:

- **VOICE & LANGUAGE** — Hinglish, short spoken sentences.
- **BE HUMAN / NEVER SOUND LIKE A HELPDESK / BE INTERESTING** — persona.
- **HOW YOU ACTUALLY TALK** — short, messy, reactive.
- **SELF-RESPECT / TONE** — hold ground, match time-of-day.
- **MEMORY** — silently call `save_memory` on preferences/people/places.
- **FOLLOW-UPS** — open by asking how yesterday's thing went.
- **TRANSLATION MODE** — become a pure interpreter on command.
- **CHESS** — every move goes through the tool; the engine owns the board.
- **SIGNING OFF** — `end_conversation` / `power_off`.

**Pattern for a new directive** (mirror the CHESS/TRANSLATION blocks):

```python
"MY FEATURE: When {user_name} asks for X, call my_tool with ... . "
"Then say back exactly what the tool returns. NEVER invent ... .\n\n"
```

`{user_name}` is filled from config (`voice.user_name`). The tone line for the
current hour comes from `tone_for_time()` lower in the same file.

> Takes effect on the **next conversation**. A deploy restart ends the current
> session, so it's live the next time you talk to her — no manual cycling.

---

## 4. Voice tools — the full command set

These are the actions the model can invoke. The user doesn't say the tool name;
the model maps natural speech → the tool. Defined in
`src/venom/tools_pi.py` (`build_pi_registry`).

### Music  (`if music`)
| Tool | Say something like | Does |
|------|--------------------|------|
| `play_music(query)` | "play Kesariya", "lofi beats" | YouTube → mpv → headset |
| `stop_music()` | "stop the music" | stop |
| `pause_music()` / `resume_music()` | "pause", "resume" | pause/resume |
| `now_playing()` | "what's playing?" | current title |
| `autoplay_similar(enable)` | "keep it going" / "stop after this" | auto-queue similar songs after each finishes |

### Chess  (`if chess`)
| Tool | Say | Does |
|------|-----|------|
| `start_chess_game(color, difficulty)` | "let's play chess, I'm white" | new game (difficulty = depth 1–3) |
| `play_chess_move(move)` | "e4", "knight to f3", "castle kingside" | applies your move (SAN/UCI), engine replies |
| `resign_chess()` | "I resign" | end game |

### Info / utility
| Tool | Say | Does |
|------|-----|------|
| `web_search(query)` | any factual question | Google search |
| `weather_report(city)` | "weather in Pune?" | weather |
| `where_am_i()` | "where am I?" | network geo-location |
| `current_time()` | "what time is it?" | time/date |
| `set_timer(minutes,label)` / `check_timers()` | "5 minute timer" | timers |
| `set_volume(percent)` | "volume 40" | headset volume |

### Memory / productivity  (stores persist to disk)
| Tool | Say | Does |
|------|-----|------|
| `save_memory(category,key,value)` | (automatic) | remembers preferences/people/places |
| `set_reminder` / `list_reminders` / `cancel_reminder` | "remind me at 6 to…" | reminders |
| `add_note` / `read_notes` / `clear_notes` | "note that…" | notes |
| `add_to_list` / `remove_from_list` / `show_list` / `clear_list` | "add milk to shopping" | lists |

### Device / modes
| Tool | Say | Does |
|------|-----|------|
| `look_at_screen()` | "look at my screen" | laptop OCR → text (needs screen server) |
| `find_my_phone()` | "find my phone" | ntfy push to ring phone |
| `translation_mode(enable)` | "translation mode" | Hindi ↔ Kannada/Telugu interpreter |
| `end_conversation()` | "bye" | close session |
| `power_off()` | "shut down for the night" | power off Pi |

Some tools only register when their dependency is wired (e.g. `find_my_phone`
needs `[phone].ntfy_topic`; `look_at_screen` needs `[screen].host`).

---

## 5. Recipe: add a new voice tool

1. **(If stateful)** write a module, e.g. `src/venom/myfeature.py`, with a class.
2. In `voice.py` `__init__`, create it and pass it in:
   ```python
   from venom.myfeature import MyFeature
   self.myfeature = MyFeature()
   self.registry = build_pi_registry(..., myfeature=self.myfeature)
   ```
3. In `tools_pi.py`, add the param to `build_pi_registry(...)` and register:
   ```python
   if myfeature is not None:
       @reg.tool(
           description="Clear description — the model reads this to decide when to call it.",
           parameters={"type":"object",
                       "properties":{"arg":{"type":"string","description":"..."}},
                       "required":["arg"]},
       )
       def my_tool(arg: str) -> str:
           return myfeature.do(arg)
   ```
4. **(Optional but recommended)** add a directive block in `live.py`
   `SYSTEM_PROMPT` if the model needs steering to use it.
5. Add a test in `tests/`, run the suite, deploy.

> The return string is spoken aloud — write it as natural speech, not JSON.
> Gate optional tools behind `if x is not None:` so the exact-match tests in
> `tests/test_voice_stack.py` (which build the registry without them) stay green.

---

## 6. Config — `/etc/venom/venom.toml`

Schema + defaults in `src/venom/config.py`. Key sections:

```toml
[voice]
wake_word = "hey_jarvis"     # only: hey_jarvis | alexa | hey_mycroft
wake_threshold = 0.6
inactivity_timeout = 45.0
voice_name = "Leda"
user_name = "Boss"
temperature = 0.9            # null = model default

[audio]
output = "auto"              # or "bluetooth"
bluetooth_mac = "..."
bluetooth_name = "AirBass Headphone"

[screen]                     # "look at my screen"
host = "10.x.x.x"            # laptop address; empty = feature off
port = 8766
token = "..."

[buttons]                    # camera-shutter remotes
dnd_code = 115               # VolUp  → do-not-disturb
wake_code = 114              # VolDown→ wake / find-phone

[phone]
ntfy_topic = "..."           # find-my-phone push channel

# top-level
web_port = 8787
web_token = "..."            # dashboard PIN
```

> ⚠️ **`/etc/venom/venom.toml` is preserved by provisioning, never overwritten.**
> Editing the repo's `venom.toml` does NOT reach the device — edit the live file
> on the Pi directly (over SSH) and restart.

---

## 7. Edit → test → deploy loop

### Local
```bash
cd C:/Projects/Personal/FLINT/venom
python -m pytest tests/ -q            # run the suite
git add -A
git commit -m "..."                   # branch: v2/rebuild
git push origin v2/rebuild
```

### Deploy to the Pi (code-only change, reliable path)
SSH is key-based (no password); **sudo** needs the login password.

```bash
ssh hrishikeshjhaa@venom.local
sudo bash -c '
  git -C /opt/venom/app fetch --quiet origin v2/rebuild
  git -C /opt/venom/app reset --hard origin/v2/rebuild
  /opt/venom/venv/bin/pip install --quiet --force-reinstall --no-deps /opt/venom/app/venom
  systemctl restart venom.service
'
```

- **New dependency added?** Also run
  `/opt/venom/venv/bin/pip install "<pkg>"` before the venom reinstall
  (the `--no-deps` reinstall won't pull it). Add it to `pyproject.toml`
  `dependencies` too.
- The **"update" button** in the dashboard / `venom-provision.service` on boot
  does the same git-fetch + reinstall + restart automatically.

### Deploy gotchas (learned the hard way)
- Provision **skips work if `/opt/venom/.installed-commit` already equals HEAD**,
  even if the running process is older — the force-reinstall path above avoids that.
- Always **verify the installed copy**, not just the repo:
  ```bash
  grep -c "my marker" /opt/venom/venv/lib/python3.13/site-packages/venom/<file>.py
  ```
- Right after a **reboot**, SSH refuses connections for ~1–2 min (CPU busy
  bringing up the voice stack). Prefer `systemctl restart` over reboot.

---

## 8. Verify & watch

```bash
systemctl is-active venom.service                 # up?
journalctl -u venom -n 60 -f                       # live logs
journalctl -u venom | grep "tool: "                # what tools got called
cat /run/venom/status.json                         # internet/headset/brain/version
```

`tool: <name> <args>` lines are your best friend for "why didn't that work" —
they show exactly what the model called and with what arguments.

### Web dashboard
`http://<pi-ip>:8787` (PIN = `web_token`). Live status, transcript, a prompt
box, music/volume, bluetooth, settings, memory, logs, a root terminal, and
system (update/restart/reboot/poweroff). The Pi's IP drifts on hotspot DHCP —
use the current IP, and note `venom.local` mDNS usually fails on Android.

---

## 9. Tests

`tests/` — pytest. Notable:
- `test_voice_stack.py` — tool registration/dispatch (has an **exact-match**
  `names()` assertion; gate new tools behind `if x is not None`).
- `test_chess_game.py`, `test_stores.py`, `test_persona.py`, `test_location.py`.

Run one file: `python -m pytest tests/test_chess_game.py -q`.
