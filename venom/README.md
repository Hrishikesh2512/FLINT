# Venom — a self-hosted voice assistant that lives on a Raspberry Pi

Venom is a **standalone wearable voice assistant**. It runs entirely on a
Raspberry Pi 4 booted from a USB pendrive — no SD card, no monitor, no
keyboard, no laptop, no phone app. It needs exactly three things: power,
Wi-Fi it already knows, and the Gemini API key baked in at flash time.

Say **"Hey Jarvis"** into the USB headset and talk. LLM compute happens in
the cloud (Gemini Live streams speech both ways); the Pi orchestrates —
wake word, audio, tools, memory, self-healing. Venom shares its
architecture with FLINT through `flint-core`, but it is its own product.

## What Venom can do (standalone)

- **Voice conversation** — natural spoken dialogue, interruptions handled,
  multilingual (replies in whatever language you speak).
- **Web search** — Google-grounded answers about anything current: news,
  prices, facts, people, places.
- **Weather** — any city, no extra API key (open-meteo).
- **Timers** — "set a timer for 10 minutes for chai"; a chime plays in your
  headset when it fires, even if Venom was asleep.
- **Bluetooth headset for your laptop/phone** — "pair my laptop" makes the
  Pi discoverable as 'venom'; the device's audio plays through your earphone
  (mixed with Venom's voice, never replacing it) and its calls can use the
  earphone mic. Disconnect from the device, or say "disconnect my laptop".
- **Ambient awareness** — she also speaks *first*. Every few minutes she
  fuses calendar, weather, unread mail, reminders and her own vitals, and
  opens a conversation when the combination earns it: rain plus somewhere to
  be ("chhata le lena"), an 8 a.m. start told to you tonight, her own SD card
  filling up. Bounded by quiet hours, a per-topic cooldown and a daily cap —
  and she stays silent rather than ask a generic "sab theek?".
- **Emergency SOS** — say "SOS" (or "help me", "bachao") and she WhatsApps
  every emergency contact with what's happening, your approximate location and
  the time, then *stays in emergency mode*, resending your location every few
  minutes until you say you're safe. The SOS list is its own book — separate
  from Connections and from whoever you last messaged — so it can reach
  different people than you normally text, each with their own wording:
  "add my father to my emergency contacts". Set it up by voice or in the
  console's EMERGENCY SOS panel, and prove it works with a clearly-marked
  test alert before you ever need it.
- **Time & date**, **headset volume control** by voice.
- **Background jobs** — work that outlives the conversation that asked for
  it. "Go research what's happening with the rupee properly and tell me" —
  she plans the searches, runs them over the next few minutes, writes the
  answer up, and *opens a fresh conversation* to give it to you. Ask "what
  are you working on?" any time; "stop that" cancels it. Jobs are stored on
  disk, so one still running when the power goes is picked back up rather
  than lost, and every job carries a step budget and an expiry so a runaway
  loop can't quietly bill you all month.
- **A root terminal that keeps receipts** — the browser console's shell runs
  as real root (that's the point: `apt`, `systemctl`, `mkdir` all work), and
  every command is written to `/var/log/venom-shell.log` before it runs, so a
  command that hung or took the Pi down still left a trace. A handful of
  device-ending commands (`rm -rf /`, `mkfs`, `dd` onto a disk) are refused as
  a typo guard — do those over SSH, where you meant to.
- **Permissions & an audit log** — every tool call is checked against what
  the device is allowed to do and written down. Ask "what have you been
  doing?" for the last few actions, refusals included, or read
  `/var/lib/venom/audit.jsonl` directly. Switch a whole class of action off
  with one line: `denied = ["messaging"]`.
- **Long-term memory** — it quietly remembers who you are, what you like,
  what you're working on, and uses it naturally next time.
- **Goes back to sleep** on "goodbye" or ~45 s of silence; wake it again
  anytime. Boot/wake/error states are signalled with distinct chimes since
  there is no screen.

Everything she does inside a conversation is a tool on the shared registry,
so new skills are one `@registry.tool` away. Everything she goes away and
does is a job type on the shared kernel (`flint_core.kernel` — scheduling,
budgets, persistence, delivery), so new autonomous work is one
`@runners.runner` away.

Skills are grouped into **capabilities** (`flint_core.capabilities`), which
pair a skill's tools with the instructions for using them and the condition
that makes it real. A capability that is off contributes neither tools nor
prompt text — so a Pi with no TV is never told about a TV — and its
permissions are what the audit layer checks. Adding a skill is one
`Capability`, not an edit to the registry plus an edit to the persona plus a
note to keep them in step.

```
venom/src/venom/
├── voice.py        # wake ⇄ conversation state machine
├── live.py         # one Gemini Live session: audio duplex + tool dispatch
├── wake.py         # openWakeWord ("hey jarvis") + silence endpointing
├── tools_pi.py     # search, weather, timers, volume, memory, goodbye
├── capabilities.py # each skill's tools + its instructions + when it applies
├── jobs.py         # job types: multi-step work that outlives a conversation
├── sos.py          # emergency contacts + the alert / emergency mode
├── ambient.py      # sense → judge → gate: when she opens the conversation
├── audio/          # USB headset auto-selection, mic/speaker streams, chimes
├── supervisor.py   # health monitors + voice loop, systemd watchdog
└── monitors/       # internet probe, headset detection, brain resolver
```

## Requirements

- Raspberry Pi 4 (2 GB is plenty) with a USB pendrive (16 GB+)
- A **Bluetooth headset** with mic (auto-paired — see below) or a USB headset
- A [Gemini API key](https://aistudio.google.com/apikey)
- One-time check: Pi 4 boards ship USB-boot-ready from **2020-09** onward.
  Older boards need a one-time EEPROM update via any SD card
  (Raspberry Pi Imager → Misc utility images → Bootloader → USB Boot).

## Blank pendrive → talking assistant

**1. Flash** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
*Raspberry Pi OS Lite (64-bit)*, storage = pendrive, and in the
customisation screen set hostname `venom`, a username/password, **your
Wi-Fi**, and enable SSH.

**2. Bake in Venom** (pendrive still plugged in):

```powershell
cd venom\provisioning
.\prepare-pendrive.ps1 -GeminiApiKey "AIza..." -UserName "Tushar" `
                       -ExtraWifi "MyPhoneHotspot=hotspotpass"
```

`-ExtraWifi` takes any number of `SSID=password` networks — add your phone
hotspot so Venom follows you out of the house. The Pi hops between known
networks automatically (home Wi-Fi preferred, hotspot on the go).

The script also **auto-detects the Bluetooth headset paired with your
laptop** and bakes its identity in (override with `-BluetoothMac` /
`-BluetoothName`). Audio routes through a system-wide PipeWire instance —
speaker and microphone both work over Bluetooth, no user session needed.

**3. Plug into the Pi and power on.** First boot takes ~10 minutes with
Wi-Fi in range (filesystem expands → Venom installs itself → services
start). **Put your headset in pairing mode during this boot** — the Pi
finds it, pairs, and marks it trusted. That's a one-time step: from then
on the headset reconnects to the Pi automatically whenever it's on and in
range. You'll hear a **single chime** when Venom is up.

**4. Talk:** say **"Hey Jarvis"** → two rising chimes → speak. That's it.

## Daily behavior

| Sound | Meaning |
|---|---|
| 1 chime after power-on | Venom is up and listening for the wake word |
| 2 rising chimes | Wake word heard — conversation is live |
| 2 chimes anytime | A timer finished |
| 1 low long tone | The conversation hit an error; wake it again |

Status without a screen (optional, from any device on the network):
`ssh <user>@venom.local` then `cat /run/venom/status.json` — internet,
headset, active brain, and voice state in one JSON snapshot.
`journalctl -u venom -f` streams transcripts and state changes.

## Configuration

`/etc/venom/venom.toml` (mode 640, holds the API key): wake word
(`hey_jarvis`, `alexa`, `hey_mycroft`), sensitivity, silence timeout,
voice, user name, model. Edit and `sudo systemctl restart venom`.

## Design constraints honored

- **2 GB RAM:** no local LLMs, ever. openWakeWord (~few % of one core) is
  the only local inference. Service is capped at 600 MB and idles far below.
- **Battery:** RAM-only logging, tmpfs status, HDMI blanked, wake-word
  gating keeps the radio and CPU idle between conversations.
- **Reliability:** systemd watchdog + auto-restart, crash-backoff in the
  voice loop, provisioning retries every boot until it succeeds.
- **Additives, not mandates:** a laptop brain (`[[brain]]` in the config)
  and Bluetooth audio are optional extensions; the assistant is complete
  without them. Bluetooth stack (bluez) is preinstalled for later pairing.

## Development (any OS)

```bash
pip install -e packages/flint-core -e "venom[voice]"
python -m pytest venom/tests        # 41 tests, no hardware needed
python -m venom --once              # one health cycle, prints JSON
```

The full conversation loop (session config, tool dispatch, audio downlink)
is verified end-to-end against the real Gemini Live API in development by
driving it with text turns instead of a microphone.
