<div align="center">

# F.L.I.N.T. — Quantum Console

**A voice-driven desktop AI assistant for Windows.**
Talk to it, and it sees your screen, controls your apps, searches the web, sets reminders, and answers back out loud. Powered by the Gemini Live API, with an optional phone remote.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6.svg)](#)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Good first issues](https://img.shields.io/github/issues/Hrishikesh2512/FLINT/good%20first%20issue)](https://github.com/Hrishikesh2512/FLINT/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)

</div>

> If FLINT is useful to you, a star helps other people find it. New contributors are very welcome, the [good first issues](https://github.com/Hrishikesh2512/FLINT/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) are a friendly place to start.

---

## What it does

- **Voice first.** Real time speech in and out through the Gemini Live API. Just talk.
- **Sees your screen.** Screen capture and vision so you can ask about what is in front of you.
- **Controls your machine.** Opens apps, drives the desktop, changes settings, manages files and the clipboard.
- **Tools built in.** Web search, fetch a URL, weather, YouTube transcripts, a flight finder, a code helper, and a multi step dev agent.
- **Reminders.** Set them by voice and get notified.
- **Phone remote.** A no build web console in `mobile_ui/` drives FLINT from your phone through a Supabase command queue.
- **Heads up display.** A PyQt6 HUD that stays out of your way and never blocks the main thread.

---

## Requirements

- **Windows 10 or 11.** The automation, audio, and notification integrations target Windows.
- **Python 3.11+**
- A **Gemini API key**, free from https://aistudio.google.com/apikey
- Optional: a **Supabase** project (free tier is fine) if you want the cloud bridge and phone remote.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/Hrishikesh2512/FLINT.git
cd FLINT

# 2. Install dependencies and the Playwright browsers
python setup.py

# 3. Add your Gemini API key (see Configuration below), then run
python main.py
```

That is it. Speak to FLINT and it answers back.

---

## Configuration

Secrets stay on your machine and are never committed (they are listed in `.gitignore`).

| File | In git? | Holds |
| --- | --- | --- |
| `config/api_keys.json` | no | Your Gemini and OpenRouter keys, language, signed in user |
| `mobile_ui/js/config.js` | no | Supabase URL and anon key for the phone remote (copy from `config.example.js`) |

Only the **publishable (anon)** Supabase key belongs in the phone remote config. Never put a service role key in client side files.

---

## Project structure

```
main.py            entry point, Gemini Live session and engine wiring
ui.py              PyQt6 heads up display
or_client.py       OpenRouter client (text fallback and helpers)
core/              capture engine, async pipeline, cloud bridge, tool registry, prompt
actions/           one module per tool (open_app, web_search, reminder, screen_processor, ...)
agent/             task queue, planner, executor, error handler for multi step goals
memory/            long term memory and config manager
mobile_ui/         phone remote web console (no build, plain HTML/CSS/JS)
config/            local secrets, gitignored
```

---

## Contributing

Contributions of every size are welcome, from typo fixes to new tools. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) and the
[good first issues](https://github.com/Hrishikesh2512/FLINT/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
Adding a new capability is usually as small as dropping a module in `actions/` and
registering it in `core/tool_registry.py`.

Please also read the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Roadmap ideas

- A cross platform path (macOS and Linux automation backends)
- More built in tools (calendar, email, notes)
- Pluggable model providers beyond Gemini
- A test suite and CI

Open an issue if you want to take any of these on.

---

## License

[MIT](LICENSE). Use it, fork it, ship it.
