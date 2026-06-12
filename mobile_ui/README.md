# FLINT · Mobile Remote UI (Phase 3)

A self-contained, mobile-first web console for driving FLINT from your phone.
It speaks the same contract as the desktop `core/cloud_bridge.py`: tap a tool
or speak a command, and a fresh row lands in the Supabase `command_queue`
table with `status = 'pending'`. The desktop bridge polls that table and runs
the matching action.

Everything here is plain HTML/CSS/JS — **no build step, no framework, no
backend of its own.** Drop it on any static host (or open it locally) and go.

```
mobile_ui/
├── index.html              # dashboard markup
├── css/styles.css          # obsidian + cyan neon theme
├── js/
│   ├── config.example.js   # template — copy to config.js
│   ├── config.js           # YOUR keys (gitignored)
│   ├── supabase-client.js  # connection + command insert
│   └── app.js              # mic / Web Speech + tile wiring
└── README.md
```

## What's on screen

| Block               | What it does                                                        |
|---------------------|--------------------------------------------------------------------|
| **Status header**   | Live pill that pings `command_queue` and shows Online/Offline.     |
| **Tool deck**       | One-tap tiles: Browser Control, WhatsApp, System Macros.           |
| **Mic button**      | Pulsing button → browser Web Speech API (speech-to-text).         |

The mic transcribes your voice and inserts the text as the row's
`action_name`. The tiles insert their tool's `action_name` directly
(`browser_control`, `send_message`, `computer_control` — these map straight to
the modules under `actions/`).

## Setup

1. **Copy the config template and fill in your project details:**
   ```bash
   cp js/config.example.js js/config.js
   ```
   Edit `js/config.js`:
   ```js
   window.FLINT_CONFIG = {
     SUPABASE_URL:      "https://<your-ref>.supabase.co",
     SUPABASE_ANON_KEY: "<your anon public key>",   // ⚠️ anon, NOT service_role
     COMMAND_TABLE:     "command_queue",
     HEALTHCHECK_INTERVAL_MS: 15000,
   };
   ```

2. **Serve the folder over HTTPS** (the Web Speech API and mic permission
   require a secure context — `https://` or `http://localhost`):
   ```bash
   # from inside mobile_ui/
   python -m http.server 8000
   # then open http://localhost:8000 on the same machine,
   # or host it (Netlify / Vercel / GitHub Pages / Cloudflare Pages) for phone access
   ```

3. **Open it on your phone**, allow microphone access, and you're live.
   Use Chrome or Edge — Web Speech API support is best there.

## 🔒 Security — please read

The anon key in `config.js` ships to every browser that loads the page, so it
is **public by design**. That's fine *only* if the database is locked down:

- **Never** put the `service_role` / secret key in `config.js`. It bypasses
  Row Level Security and would hand anyone with the page full DB access.
  (The desktop bridge in `config/api_keys.json` may use a privileged key —
  that one stays on the desktop and must never be copied here.)
- Enable **Row Level Security** on `command_queue` and allow the `anon` role
  to do nothing but insert pending commands.

Minimal table + policy (run in the Supabase SQL editor):

```sql
-- Table the desktop bridge already expects
create table if not exists public.command_queue (
  id          bigint generated always as identity primary key,
  action_name text not null,
  status      text not null default 'pending',
  payload     jsonb,
  result      text,
  error       text,
  created_at  timestamptz not null default now(),
  executed_at timestamptz
);

alter table public.command_queue enable row level security;

-- The phone (anon) may ONLY insert, and only rows that start as 'pending'.
create policy "anon can enqueue pending commands"
  on public.command_queue
  for insert to anon
  with check (status = 'pending');
```

> The desktop bridge reads/updates rows with its own privileged key, so it
> does not need a select/update policy for `anon`. The header bar's connection
> check uses a `head` count; if your RLS blocks counts for `anon`, the pill may
> read "Offline" even though inserts work — that's expected and harmless. If
> you want an accurate pill, add a narrow `select` policy for `anon`.

## How it connects to the desktop

```
 phone (this UI) ──insert──▶ Supabase.command_queue (status='pending')
                                      │  poll every 2s
                                      ▼
 desktop FLINT ◀── core/cloud_bridge.py runs actions.<action_name>
                                      │
                                      ▼  status='executed' (+result)
```

Run the desktop side with the bridge active (see `core/cloud_bridge.py`), keep
this page open on your phone, and every tap or spoken phrase executes on your
desktop.
