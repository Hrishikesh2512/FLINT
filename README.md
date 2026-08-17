# F.L.I.N.T — Quantum Console

> A voice-first desktop AI assistant for Windows. Talk to it, show it your screen, and let it
> drive your apps, run multi-step tasks, search the web, set reminders, and more — in English or
> any major Indian language.

<p align="left">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="UI" src="https://img.shields.io/badge/UI-PyQt6-41CD52">
  <img alt="LLM" src="https://img.shields.io/badge/voice-Gemini%20Live-8E75B2">
  <img alt="Cloud" src="https://img.shields.io/badge/cloud-Supabase-3ECF8E">
</p>

FLINT pairs the **Gemini Live API** for real-time speech with a PyQt6 heads-up display, a tool
engine that can control your machine, a multi-step task agent, and a **Supabase** cloud bridge for
accounts and an optional phone remote. It runs from source or builds into a one-folder app you can
zip and hand to anyone.

---

## ✨ Highlights

- **🎙️ Voice-first** — real-time speech in and out via the Gemini Live API.
- **🌐 Multilingual** — English plus Hindi, Hinglish, Tamil, Telugu, Bengali, Marathi, Gujarati,
  Punjabi, Kannada, and Malayalam, chosen on first launch.
- **👁️ Screen vision** — FLINT can look at your screen to answer questions and act on what it sees.
- **🤖 Task agent** — plans and executes multi-step goals (`agent/`), not just one-shot commands.
- **🔐 Free accounts** — email/password sign-in via Supabase Auth; **no plaintext passwords ever**.
- **📱 Phone remote** — drive FLINT from your phone through a no-build web console.
- **📦 Shareable** — builds to a one-folder Windows app you can zip and distribute.

## 🧰 What it can do

Each capability is a self-contained tool module in `actions/`:

| Tool | Does |
|------|------|
| `open_app` | Launch desktop apps by name |
| `desktop` / `computer_control` | Drive the desktop — clicks, windows, keystrokes |
| `computer_settings` | System settings (volume, etc.) |
| `screen_processor` | Capture and reason about what's on screen |
| `browser_control` | Browser automation via Playwright/Chromium |
| `web_search` | Search the web and summarize results |
| `send_message` | Send messages (incl. WhatsApp) |
| `reminder` | Set and fire reminders |
| `weather_report` | Current weather |
| `youtube_video` | YouTube playback + transcripts |
| `file_controller` / `file_processor` | Open, read, and manage files |
| `code_helper` / `dev_agent` | Coding assistance and a dev agent |

## 📋 Prerequisites

- **Windows 10/11** — the automation and voice integrations target Windows
- **Python 3.11+**
- A **Gemini API key** — https://aistudio.google.com/apikey
- A **Supabase project** (free tier is fine) — optional; only needed for the legacy account sign-in, which is being phased out

## 🚀 Quick start (from source)

```bash
python setup.py            # installs requirements + Playwright browsers

# one-time: point FLINT at your Supabase project
cp config/app_config.example.json config/app_config.json
#   then edit config/app_config.json with your Supabase URL + publishable key

python main.py
```

On the **first run** FLINT shows a one-time setup card:

1. **Pick a language** — replies in it from then on.
2. **Create a free account** (or sign in) with an email + password.
3. Enter your **Gemini API key** (unless one is bundled — see [Build](#-build-a-shareable-app)).

After that it remembers you and boots straight in.

## 🔒 Accounts & privacy

Accounts use **Supabase Auth**. When someone signs up, Supabase stores only a salted *hash* of
their password — FLINT never sees, sends, or saves the plaintext password anywhere. As the owner
you get a full list of your users (email, sign-up date, last login) in the Supabase dashboard, with
no ability to read anyone's password. Locally, only the login *tokens* and the user's email/name
are cached (`config/session.json`, never committed).

## 📦 Build a shareable app

Produces `dist\FLINT\` (a folder with `FLINT.exe` + `_internal\`) and zips it to `dist\FLINT.zip`.
Recipients unzip and run `FLINT.exe`.

```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

or manually:

```bash
pip install -r requirements-build.txt
pyinstaller flint.spec --noconfirm
```

It's a **one-folder** build on purpose: Windows Defender's heuristics flag and auto-delete unsigned
*one-file* PyInstaller exes as false positives, but the one-folder layout (plus the bundled icon +
version metadata) is flagged far less. If a build still gets quarantined on your own machine, add an
exclusion for the project folder and rebuild (run as Administrator):

```powershell
Add-MpPreference -ExclusionPath "C:\Projects\Personal\FLINT"
```

**For distribution to strangers, code-sign `FLINT.exe`.** An unsigned binary may still trip
SmartScreen ("More info → Run anyway") or aggressive AV on some recipients' machines; an
Authenticode signature is the only thing that removes that entirely.

Your `config/app_config.json` (Supabase URL + publishable key) is bundled into the build, so
accounts work on any machine you copy the folder to. To ship FLINT with your own API keys baked in
(so users don't need their own), fill in `bundled_gemini_api_key` / `bundled_openrouter_api_key`
there before building — the setup card then hides those fields. Note that keys baked into a build
can be extracted, so only do this with a key you're comfortable distributing.

> Browser automation uses Playwright/Chromium, which PyInstaller cannot bundle. Those specific
> features stay dormant on a fresh machine until the user runs `playwright install`; everything else
> works out of the box.

## 🗂️ Project structure

```
main.py            # entry point — Gemini Live session + engine wiring
ui.py              # PyQt6 HUD, first-run setup card
core/              # auth, i18n, async pipeline, cloud bridge, tool registry, prompt
actions/           # one module per tool (open_app, web_search, reminder, …)
agent/             # task queue, planner, executor for multi-step goals
memory/            # long-term memory + config manager
packages/flint-core/  # the shared layer all three bodies run on
venom/             # Raspberry Pi wearable runtime + pendrive provisioning kit
carnage/           # Android phone runtime — and the hub the others sync to
config/            # app_config (public) + per-machine secrets (gitignored)
flint.spec         # PyInstaller build spec
build_exe.ps1      # one-command build + zip
```

## 🧠 One assistant, three bodies

FLINT (desktop), **Venom** (a Raspberry Pi wearable) and **Carnage** (an
Android phone) are not three assistants. They share `flint-core` — the same
memory, the same searchable archive, the same projects and the same record of
what she has learned — kept in step by `flint_core.sync`.

Carnage is the hub, because it is the only device that is always powered,
always networked and always carried; the Pi hops subnets and the laptop
sleeps. The others are leaves that sync to it and never to each other.

```
venom (Pi)  ─┐
             ├─▶  carnage (phone, hub)
flint (PC)  ─┘
```

See [`carnage/README.md`](carnage/README.md) for the phone runtime and how to
point a leaf at it.

## ⚙️ Configuration files

| File | In git? | Holds |
|------|---------|-------|
| `config/app_config.example.json` | yes (template) | placeholders to copy from |
| `config/app_config.json` | no (gitignored) | your Supabase URL + publishable key, optional bundled API keys |
| `config/api_keys.json` | no (gitignored) | per-machine: Gemini/OpenRouter keys, language, signed-in user |
| `config/session.json` | no (gitignored) | per-machine Supabase login tokens |

Only the **publishable** (anon) Supabase key ever belongs in `app_config.json` — never a
service-role/secret key.

## 📄 License

No license has been chosen yet. Until one is added, this code is "all rights reserved" by default —
add a `LICENSE` file (e.g. MIT) before inviting outside contributions.
