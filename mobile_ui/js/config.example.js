/* ════════════════════════════════════════════════════════════════════
   FLINT Remote — Supabase connection config (TEMPLATE)

   1. Copy this file to `config.js` in the same folder.
   2. Fill in your project URL and the **anon (public)** key.

   ⚠️  SECURITY — read this:
   This file is loaded by the browser, so its contents are PUBLIC.
   • Use the *anon* key only.  NEVER paste the service_role / secret key
     here — it bypasses Row Level Security and would hand full DB access
     to anyone who opens the page.
   • Lock the `command_queue` table down with RLS so the anon role can
     ONLY insert pending commands (see mobile_ui/README.md for the SQL).

   `config.js` is gitignored so your keys never get committed.
   ════════════════════════════════════════════════════════════════════ */

window.FLINT_CONFIG = {
  // Your project URL, e.g. "https://abcdefgh.supabase.co"
  SUPABASE_URL: "https://YOUR-PROJECT-ref.supabase.co",

  // The ANON / public key (Project Settings → API → anon public)
  SUPABASE_ANON_KEY: "YOUR-ANON-PUBLIC-KEY",

  // Must match supabase_command_table in config/api_keys.json
  COMMAND_TABLE: "command_queue",

  // How often the header bar re-checks the connection (ms)
  HEALTHCHECK_INTERVAL_MS: 15000,
};
