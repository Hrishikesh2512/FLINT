/* ════════════════════════════════════════════════════════════════════
   FLINT Remote — Supabase bridge
   Owns the single supabase-js client, a lightweight connection
   healthcheck against the command_queue table, and the one write the
   whole app cares about: enqueue a pending command for the desktop
   CloudBridge to pick up.
   ════════════════════════════════════════════════════════════════════ */

const FlintCloud = (() => {
  const cfg = window.FLINT_CONFIG || {};
  const table = cfg.COMMAND_TABLE || "command_queue";

  const isPlaceholder = (v) =>
    !v || typeof v !== "string" || v.includes("YOUR-") || v.trim() === "";

  const configured = !isPlaceholder(cfg.SUPABASE_URL) && !isPlaceholder(cfg.SUPABASE_ANON_KEY);

  let client = null;
  if (configured) {
    if (!window.supabase || typeof window.supabase.createClient !== "function") {
      console.error("[FlintCloud] supabase-js failed to load (check the CDN <script> / network).");
    } else {
      client = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY, {
        auth: { persistSession: false },
      });
    }
  } else {
    console.warn("[FlintCloud] Supabase not configured — copy config.example.js to config.js and fill in your URL + anon key.");
  }

  /**
   * Ping the command_queue table with a tiny HEAD/count request.
   * Resolves { ok, error } so the header bar can render a real state
   * instead of guessing.
   */
  async function checkConnection() {
    if (!client) return { ok: false, error: "not-configured" };
    try {
      const { error } = await client
        .from(table)
        .select("id", { count: "exact", head: true });
      if (error) return { ok: false, error: error.message };
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e && e.message ? e.message : e) };
    }
  }

  /**
   * Drop a fresh row into command_queue for the desktop to execute.
   * Matches the CloudBridge contract: action_name + status='pending'.
   *
   * @param {string} actionName  e.g. the transcribed phrase, or a tool id
   * @param {object} [payload]   optional args bag (sent only if non-empty)
   */
  async function enqueueCommand(actionName, payload) {
    const action = String(actionName || "").trim();
    if (!action) return { ok: false, error: "empty action_name" };
    if (!client) return { ok: false, error: "not-configured" };

    const row = { action_name: action, status: "pending" };
    if (payload && typeof payload === "object" && Object.keys(payload).length) {
      row.payload = payload;
    }

    try {
      const { data, error } = await client.from(table).insert(row).select().single();
      if (error) return { ok: false, error: error.message };
      return { ok: true, row: data };
    } catch (e) {
      return { ok: false, error: String(e && e.message ? e.message : e) };
    }
  }

  return {
    configured,
    table,
    healthcheckInterval: Number(cfg.HEALTHCHECK_INTERVAL_MS) || 15000,
    checkConnection,
    enqueueCommand,
  };
})();
