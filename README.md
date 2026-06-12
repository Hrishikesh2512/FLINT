# F.L.I.N.T — Quantum Console

A voice-driven desktop AI assistant (PyQt6 + Gemini Live) with screen vision,
app/automation control, reminders, web search, a phone remote, and a Supabase
cloud bridge.

- **Voice-first** — real-time speech in/out via the Gemini Live API.
- **Multilingual** — English plus major Indian languages, chosen on first launch.
- **Free accounts** — email/password sign-in via Supabase Auth (no plaintext passwords).
- **Shareable** — builds to a one-folder app you can zip and hand to anyone.

## Prerequisites

- Windows 10/11 (the automation/voice integrations target Windows)
- Python 3.11+
- A **Gemini API key** — https://aistudio.google.com/apikey
- A **Supabase project** (free tier is fine) — see [SUPABASE_SETUP.md](SUPABASE_SETUP.md)

## Quick start (from source)

```bash
python setup.py            # installs requirements + Playwright browsers

# one-time: point FLINT at your Supabase project
cp config/app_config.example.json config/app_config.json
#   then edit config/app_config.json with your Supabase URL + publishable key

python main.py
```

On the **first run** FLINT shows a one-time setup card:

1. **Pick a language** — English, Hindi, Hinglish, Tamil, Telugu, Bengali,
   Marathi, Gujarati, Punjabi, Kannada, or Malayalam. FLINT replies in it from then on.
2. **Create a free account** (or sign in) with an email + password.
3. Enter your **Gemini API key** (unless one is bundled — see below).

After that it remembers you and boots straight in.

## Accounts & privacy

Accounts use **Supabase Auth**. When someone signs up, Supabase stores only a
salted *hash* of their password — FLINT never sees, sends, or saves the
plaintext password anywhere. As the owner you get a full list of your users
(email, sign-up date, last login) in the Supabase dashboard, with no ability to
read anyone's password. Locally, only the login *tokens* and the user's
email/name are cached (`config/session.json`, never committed).

## Build a shareable app

Produces `dist\FLINT\` (a folder with `FLINT.exe` + `_internal\`) and zips it to
`dist\FLINT.zip`. Recipients unzip and run `FLINT.exe`.

```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

or manually:

```bash
pip install -r requirements-build.txt
pyinstaller flint.spec --noconfirm
```

It's a **one-folder** build on purpose: Windows Defender's heuristics flag and
auto-delete unsigned *one-file* PyInstaller exes as false positives, but the
one-folder layout (plus the bundled icon + version metadata) is flagged far
less. If a build still gets quarantined on your own machine, add an exclusion
for the project folder and rebuild (run as Administrator):

```powershell
Add-MpPreference -ExclusionPath "C:\Projects\Personal\FLINT"
```

**For distribution to strangers, code-sign `FLINT.exe`.** An unsigned binary may
still trip SmartScreen ("More info → Run anyway") or aggressive AV on some
recipients' machines; an Authenticode signature is the only thing that removes
that entirely.

Your `config/app_config.json` (Supabase URL + publishable key) is bundled into
the build, so accounts work on any machine you copy the folder to. To ship
FLINT with your own API keys baked in (so users don't need their own), fill in
`bundled_gemini_api_key` / `bundled_openrouter_api_key` there before building —
the setup card then hides those fields. Note that keys baked into a build can be
extracted, so only do this with a key you're comfortable distributing.

> Browser automation uses Playwright/Chromium, which PyInstaller cannot bundle.
> Those specific features stay dormant on a fresh machine until the user runs
> `playwright install`; everything else works out of the box.

## Phone remote (optional)

`mobile_ui/` is a no-build web console for driving FLINT from your phone through
the Supabase `command_queue` table. See [mobile_ui/README.md](mobile_ui/README.md).

## Project structure

```
main.py            # entry point — Gemini Live session + engine wiring
ui.py              # PyQt6 HUD, first-run setup card
core/              # auth, i18n, async pipeline, cloud bridge, tool registry, prompt
actions/           # one module per tool (open_app, web_search, reminder, …)
agent/             # task queue, planner, executor for multi-step goals
memory/            # long-term memory + config manager
mobile_ui/         # phone remote web console
config/            # app_config (public) + per-machine secrets (gitignored)
flint.spec         # PyInstaller build spec
build_exe.ps1      # one-command build + zip
```

## Configuration files

| File | In git? | Holds |
|------|---------|-------|
| `config/app_config.example.json` | yes (template) | placeholders to copy from |
| `config/app_config.json` | no (gitignored) | your Supabase URL + publishable key, optional bundled API keys |
| `config/api_keys.json` | no (gitignored) | per-machine: Gemini/OpenRouter keys, language, signed-in user |
| `config/session.json` | no (gitignored) | per-machine Supabase login tokens |

Only the **publishable** (anon) Supabase key ever belongs in `app_config.json` —
never a service-role/secret key.

## License

No license has been chosen yet. Until one is added, this code is
"all rights reserved" by default — add a `LICENSE` file (e.g. MIT) before
inviting outside contributions.
