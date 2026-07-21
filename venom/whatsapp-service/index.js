/*
 * Venom WhatsApp bridge — self-hosted, no phone automation required.
 *
 * Links the user's WhatsApp as a "linked device" (the same mechanism as
 * WhatsApp Web, so the phone keeps receiving everything normally) and exposes
 * a tiny HTTP API on localhost that Venom's Python voice loop calls:
 *
 *   GET  /health          -> {connected, loggedIn, user, contacts}
 *   GET  /qr              -> current login QR as ASCII (text) while pairing
 *   GET  /qr.png          -> the same QR as a PNG image (for the web console)
 *   GET  /contacts?q=NAME -> [{jid, name}] fuzzy matches, for disambiguation
 *   POST /send  {to,text} -> send a message; `to` = name | number | jid,
 *                            or omit `to` to reply to the most recent chat.
 *
 * Incoming 1:1 messages are forwarded to the configured ntfy topic so Venom's
 * existing NotificationHub chimes on arrival and reads them on demand — the
 * exact path the phone/MacroDroid forwarder used, now sourced here instead.
 *
 * Auth/session state (Baileys creds + the learned contact map) persists under
 * WA_STATE_DIR so a restart never needs a re-scan.
 *
 * Config via environment (set by the systemd unit):
 *   WA_STATE_DIR   directory for creds + contacts.json   (default ./state)
 *   WA_HOST        HTTP bind address                      (default 127.0.0.1)
 *   WA_PORT        HTTP port                              (default 8788)
 *   WA_TOKEN       if set, require it in X-Token header
 *   NTFY_SERVER    ntfy base for incoming forward         (default https://ntfy.sh)
 *   NTFY_TOPIC     ntfy topic for incoming forward        (empty = no forward)
 */

'use strict';

const http = require('http');
const path = require('path');
const fs = require('fs');

const {
  default: makeWASocket,
  useMultiFileAuthState,
  makeCacheableSignalKeyStore,
  fetchLatestBaileysVersion,
  DisconnectReason,
  Browsers,
  jidNormalizedUser,
  isJidUser,
} = require('@whiskeysockets/baileys');
const pino = require('pino');
const qrcodeTerminal = require('qrcode-terminal');
const QRCode = require('qrcode');

// ── config ──────────────────────────────────────────────────────────────────
const STATE_DIR = process.env.WA_STATE_DIR || path.join(__dirname, 'state');
const HOST = process.env.WA_HOST || '127.0.0.1';
const PORT = parseInt(process.env.WA_PORT || '8788', 10);
const TOKEN = (process.env.WA_TOKEN || '').trim();
const NTFY_SERVER = (process.env.NTFY_SERVER || 'https://ntfy.sh').replace(/\/+$/, '');
const NTFY_TOPIC = (process.env.NTFY_TOPIC || '').trim();

// Auto-reply mode: when on, the bridge answers incoming 1:1 messages itself
// with a short Gemini-composed reply on the user's behalf.
const GEMINI_API_KEY = (process.env.GEMINI_API_KEY || '').trim();
const GEMINI_MODEL = (process.env.GEMINI_MODEL || 'gemini-2.5-flash').trim();
const AUTO_REPLY_USER = (process.env.AUTO_REPLY_USER || '').trim() || 'the user';
const AUTO_REPLY_COOLDOWN_MS = 8000; // min gap between replies to one chat
const AUTO_REPLY_MAX_PER_HOUR = 5;   // per-chat cap — stops runaway bot loops
const AUTO_REPLY_HISTORY = 6;        // turns of context kept per chat

const CONTACTS_FILE = path.join(STATE_DIR, 'contacts.json');
const AUTH_DIR = path.join(STATE_DIR, 'auth');

fs.mkdirSync(AUTH_DIR, { recursive: true });

const logger = pino({ level: process.env.WA_LOG_LEVEL || 'warn' });

// ── learned contact map (jid -> display name), persisted ─────────────────────
/** @type {Map<string,string>} */
let contacts = new Map();
try {
  const raw = JSON.parse(fs.readFileSync(CONTACTS_FILE, 'utf8'));
  contacts = new Map(Object.entries(raw));
} catch {
  /* first run — no contacts yet */
}
let contactsDirty = false;
function rememberContact(jid, name) {
  if (!jid || !isJidUser(jid)) return;
  const norm = jidNormalizedUser(jid);
  name = (name || '').trim();
  if (!name) return;
  if (contacts.get(norm) !== name) {
    contacts.set(norm, name);
    contactsDirty = true;
  }
}
// Flush the contact map lazily so a burst of updates is one write.
setInterval(() => {
  if (!contactsDirty) return;
  contactsDirty = false;
  fs.writeFile(
    CONTACTS_FILE,
    JSON.stringify(Object.fromEntries(contacts), null, 0),
    (err) => { if (err) logger.warn({ err }, 'contacts write failed'); },
  );
}, 5000).unref();

// ── live socket state ────────────────────────────────────────────────────────
let sock = null;
let connected = false;
let loggedIn = false;
let currentQR = null; // raw QR string while pairing; null once connected
let lastChatJid = null; // most recent 1:1 chat, for "reply to the last message"
let reconnectDelay = 2000; // grows on repeated failures so we don't hammer WA

// ── auto-reply state ─────────────────────────────────────────────────────────
// The on/off flag persists across restarts/reboots (a deploy or power-cycle
// shouldn't silently stop answering), but on load we reset the "enabled at"
// clock to now so the restart's backlog is never auto-answered.
const AUTOREPLY_FILE = path.join(STATE_DIR, 'autoreply.json');
let autoReply = false;
let autoReplyEnabledAt = 0; // epoch seconds; only answer messages newer than this
try {
  if (JSON.parse(fs.readFileSync(AUTOREPLY_FILE, 'utf8')).enabled) {
    autoReply = true;
    autoReplyEnabledAt = Math.floor(Date.now() / 1000);
  }
} catch { /* no saved state — default off */ }

function persistAutoReply() {
  try {
    fs.writeFileSync(AUTOREPLY_FILE, JSON.stringify({ enabled: autoReply }));
  } catch (err) {
    logger.warn({ err }, 'could not persist auto-reply state');
  }
}
/** @type {Map<string, Array<{role:string, text:string}>>} recent turns per chat */
const chatHistory = new Map();
/** @type {Map<string, number>} last auto-reply time per chat (ms) */
const lastAutoReplyAt = new Map();
/** @type {Map<string, {windowStart:number, count:number}>} hourly cap per chat */
const autoReplyCount = new Map();

function pushHistory(jid, role, text) {
  const h = chatHistory.get(jid) || [];
  h.push({ role, text });
  while (h.length > AUTO_REPLY_HISTORY) h.shift();
  chatHistory.set(jid, h);
}

// Rolling hourly cap so a chat (or another auto-responder) can't loop forever.
function underHourlyCap(jid) {
  const now = Date.now();
  const rec = autoReplyCount.get(jid);
  if (!rec || now - rec.windowStart > 3600_000) {
    autoReplyCount.set(jid, { windowStart: now, count: 0 });
    return true;
  }
  return rec.count < AUTO_REPLY_MAX_PER_HOUR;
}
function noteAutoReply(jid) {
  lastAutoReplyAt.set(jid, Date.now());
  const rec = autoReplyCount.get(jid) || { windowStart: Date.now(), count: 0 };
  rec.count += 1;
  autoReplyCount.set(jid, rec);
}

const AUTO_REPLY_SYSTEM =
  `You are Jarvis — ${AUTO_REPLY_USER}'s witty AI assistant, in the spirit of ` +
  `Tony Stark's Jarvis: dry, clever, effortlessly composed, and a little cheeky ` +
  `— auto-replying to WhatsApp messages while ${AUTO_REPLY_USER} is busy. Always ` +
  `write in the THIRD PERSON as Jarvis about ${AUTO_REPLY_USER}, NEVER as if you ` +
  `were ${AUTO_REPLY_USER} (do not use "I"/"me" to mean ${AUTO_REPLY_USER}). Add ` +
  `a light, classy touch of humour or a witty aside — playful and elegant, never ` +
  `crude, and never at the sender's expense; the joke is gently on ` +
  `${AUTO_REPLY_USER} being unreachable, not on them. Still genuinely acknowledge ` +
  `their message. Examples: "Jarvis here. ${AUTO_REPLY_USER} has wandered away ` +
  `from his phone — a rare and beautiful sighting. He shall return your message ` +
  `shortly." / "${AUTO_REPLY_USER} abhi busy hai, filhaal main Jarvis morcha ` +
  `sambhal raha hoon. Thoda sabr rakho, wapas aake reply karega." Match the ` +
  `sender's language and script (Hinglish with Hinglish). Keep it to one or two ` +
  `short sentences. Never agree to payments, share personal, financial, or ` +
  `private details, or send links or codes. Do not add any signature or ` +
  `sign-off yourself.`;

async function generateReply(jid, incomingText) {
  if (!GEMINI_API_KEY) return '';
  const history = chatHistory.get(jid) || [];
  const contents = history.map((t) => ({ role: t.role, parts: [{ text: t.text }] }));
  contents.push({ role: 'user', parts: [{ text: incomingText }] });
  const url =
    `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}` +
    `:generateContent?key=${encodeURIComponent(GEMINI_API_KEY)}`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      system_instruction: { parts: [{ text: AUTO_REPLY_SYSTEM }] },
      contents,
      generationConfig: {
        maxOutputTokens: 200,
        temperature: 0.7,
        // A one-line text needs no chain-of-thought; thinking just adds latency.
        thinkingConfig: { thinkingBudget: 0 },
      },
    }),
  });
  if (!resp.ok) {
    logger.warn({ status: resp.status }, 'gemini auto-reply failed');
    return '';
  }
  const data = await resp.json();
  const parts = data?.candidates?.[0]?.content?.parts || [];
  return parts.map((p) => p.text || '').join('').trim();
}

// Every WhatsApp message Venom sends is tagged so the recipient knows it came
// from Jarvis, not the user typing — italic via WhatsApp's _underscore_ markup.
const SIGNATURE = '\n\n_Jarvis(Auto-Generated)_';
async function sendText(jid, text) {
  return sock.sendMessage(jid, { text: (text || '').trim() + SIGNATURE });
}

async function maybeAutoReply(jid, incomingText, tsSeconds) {
  if (!autoReply || !incomingText) return;
  // Never touch the pre-activation backlog.
  if (tsSeconds && tsSeconds < autoReplyEnabledAt) return;
  const last = lastAutoReplyAt.get(jid) || 0;
  if (Date.now() - last < AUTO_REPLY_COOLDOWN_MS) return;
  if (!underHourlyCap(jid)) {
    logger.warn({ jid }, 'auto-reply hourly cap reached — skipping');
    return;
  }
  try {
    const reply = await generateReply(jid, incomingText);
    if (!reply) return;
    await sendText(jid, reply);
    pushHistory(jid, 'user', incomingText);
    pushHistory(jid, 'model', reply);
    noteAutoReply(jid);
    console.log(`[venom-whatsapp] auto-replied to ${jid.split('@')[0]}: ${reply.slice(0, 60)}`);
  } catch (err) {
    logger.warn({ err }, 'auto-reply send failed');
  }
}

function digitsOnly(s) {
  return (s || '').replace(/[^0-9]/g, '');
}

// An individual (1:1) human chat — either classic phone-number addressing or
// WhatsApp's newer LID (@lid) privacy addressing. Deliberately EXCLUDES groups
// (@g.us), channels (@newsletter) and status (@broadcast) so auto-reply and
// forwarding only ever touch person-to-person DMs.
function isIndividualChat(jid) {
  if (!jid) return false;
  return jid.endsWith('@s.whatsapp.net') || jid.endsWith('@lid');
}

// Resolve a `to` argument (name | phone number | jid) to a WhatsApp JID.
// Returns {jid} on success, {candidates:[{jid,name}]} when a name is
// ambiguous, or {error} when nothing matches.
async function resolveRecipient(to) {
  to = (to || '').trim();
  if (!to) {
    if (lastChatJid) return { jid: lastChatJid };
    return { error: 'no recipient given and no recent chat to reply to' };
  }
  // Already a JID.
  if (to.includes('@')) {
    if (isJidUser(to)) return { jid: jidNormalizedUser(to) };
    return { jid: to }; // group or other — pass through
  }
  // A phone number (has enough digits, mostly numeric).
  const digits = digitsOnly(to);
  if (digits.length >= 8 && digits.length >= to.replace(/\s/g, '').length - 2) {
    try {
      const results = sock ? await sock.onWhatsApp(digits) : [];
      const hit = (results || []).find((r) => r && r.exists);
      if (hit) return { jid: jidNormalizedUser(hit.jid) };
      return { error: `${to} does not appear to be on WhatsApp` };
    } catch (err) {
      return { error: `could not verify number: ${err.message}` };
    }
  }
  // A contact name — fuzzy match against the learned map.
  const q = to.toLowerCase();
  const scored = [];
  for (const [jid, name] of contacts) {
    const n = name.toLowerCase();
    let score = 0;
    if (n === q) score = 100;
    else if (n.startsWith(q)) score = 80;
    else if (n.includes(q)) score = 60;
    else if (q.split(/\s+/).every((w) => n.includes(w))) score = 40;
    if (score) scored.push({ jid, name, score });
  }
  scored.sort((a, b) => b.score - a.score);
  if (scored.length === 0) {
    return { error: `no contact matching "${to}"` };
  }
  // Unambiguous: a single match, or one clearly-best exact/prefix match.
  if (scored.length === 1 || scored[0].score >= 80 && scored[0].score > scored[1].score) {
    return { jid: scored[0].jid };
  }
  return { candidates: scored.slice(0, 5).map(({ jid, name }) => ({ jid, name })) };
}

function messageText(msg) {
  const m = msg.message || {};
  return (
    m.conversation ||
    (m.extendedTextMessage && m.extendedTextMessage.text) ||
    (m.imageMessage && m.imageMessage.caption) ||
    (m.videoMessage && m.videoMessage.caption) ||
    ''
  ).trim();
}

async function forwardIncoming(jid, sender, text) {
  if (!NTFY_TOPIC || !text) return;
  try {
    await fetch(`${NTFY_SERVER}/${NTFY_TOPIC}`, {
      method: 'POST',
      headers: {
        Title: (sender || 'WhatsApp').slice(0, 120),
        // Group under one ntfy tag so Venom's hub reads them as WhatsApp.
        Tags: 'speech_balloon',
      },
      body: text.slice(0, 2000),
    });
  } catch (err) {
    logger.warn({ err }, 'ntfy forward failed');
  }
}

// ── Baileys connection lifecycle ─────────────────────────────────────────────
async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  // Pull WhatsApp Web's current protocol version — a stale hardcoded version is
  // rejected at the handshake with a 405, so never rely on the bundled default.
  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
    console.log(`[venom-whatsapp] using WA Web version ${version.join('.')}`);
  } catch (err) {
    console.log('[venom-whatsapp] version fetch failed, using bundled default');
  }

  sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    logger,
    printQRInTerminal: false,
    browser: Browsers.ubuntu('Chrome'),
    syncFullHistory: false,
    // Never mark the linked device "online" — that would divert notifications
    // away from the user's phone. Venom is a silent observer + sender.
    markOnlineOnConnect: false,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      currentQR = qr;
      loggedIn = false;
      // Print to stdout so it's scannable over SSH via `journalctl`.
      console.log('\n[venom-whatsapp] scan this QR in WhatsApp → Linked Devices:\n');
      qrcodeTerminal.generate(qr, { small: true });
    }
    if (connection === 'open') {
      connected = true;
      loggedIn = true;
      currentQR = null;
      reconnectDelay = 2000; // healthy link — reset backoff
      const me = sock.user && sock.user.id;
      if (me) rememberContact(me, sock.user.name || 'me');
      console.log(`[venom-whatsapp] connected as ${me || 'unknown'}`);
    } else if (connection === 'close') {
      connected = false;
      const code =
        lastDisconnect &&
        lastDisconnect.error &&
        lastDisconnect.error.output &&
        lastDisconnect.error.output.statusCode;
      if (code === DisconnectReason.loggedOut) {
        // The user unlinked the device — wipe creds so a fresh QR is shown.
        loggedIn = false;
        console.log('[venom-whatsapp] logged out — clearing session, will re-pair');
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch {}
        fs.mkdirSync(AUTH_DIR, { recursive: true });
        reconnectDelay = 2000;
        setTimeout(start, 1500);
      } else {
        // Back off on repeated failures (esp. 405) so we don't get rate-limited.
        console.log(
          `[venom-whatsapp] connection closed (${code}) — retry in ${reconnectDelay}ms`);
        setTimeout(start, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
      }
    }
  });

  // Learn names from the contact list as WhatsApp syncs it.
  const learnFrom = (list) => {
    for (const c of list || []) {
      rememberContact(c.id, c.name || c.notify || c.verifiedName);
    }
  };
  sock.ev.on('contacts.upsert', learnFrom);
  sock.ev.on('contacts.update', learnFrom);
  sock.ev.on('messaging-history.set', ({ contacts: c }) => learnFrom(c));

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const msg of messages) {
      const dbgJid = msg.key && msg.key.remoteJid;
      console.log(
        `[venom-whatsapp] upsert type=${type} jid=${dbgJid} ` +
        `fromMe=${msg.key && msg.key.fromMe} hasMsg=${!!msg.message} ` +
        `text=${JSON.stringify((messageText(msg) || '').slice(0, 40))}`);
    }
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (!msg.message || msg.key.fromMe) continue;
      const jid = msg.key.remoteJid;
      // Learn the sender's name; forward only 1:1 chats (skip groups/status).
      if (msg.pushName) rememberContact(jid, msg.pushName);
      if (!isIndividualChat(jid)) continue;
      // @lid jids aren't phone-number jids, so don't run them through
      // jidNormalizedUser (which assumes @s.whatsapp.net) — keep them as-is.
      lastChatJid = jid.endsWith('@lid') ? jid : jidNormalizedUser(jid);
      const text = messageText(msg);
      const sender = msg.pushName || contacts.get(lastChatJid) || jid.split('@')[0];
      if (text) {
        await forwardIncoming(jid, sender, text);
        await maybeAutoReply(lastChatJid, text, Number(msg.messageTimestamp) || 0);
      }
    }
  });
}

// ── HTTP API ─────────────────────────────────────────────────────────────────
function authed(req) {
  if (!TOKEN) return true;
  return (req.headers['x-token'] || '') === TOKEN;
}

function sendJSON(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (c) => {
      data += c;
      if (data.length > 1_000_000) req.destroy(); // 1 MB guard
    });
    req.on('end', () => resolve(data));
    req.on('error', () => resolve(''));
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const route = url.pathname;

  if (route === '/health') {
    return sendJSON(res, 200, {
      connected,
      loggedIn,
      user: (sock && sock.user && sock.user.id) || null,
      contacts: contacts.size,
      hasQR: !!currentQR,
      autoReply,
    });
  }

  if (!authed(req)) return sendJSON(res, 401, { error: 'unauthorized' });

  if (route === '/auto-reply' && req.method === 'POST') {
    let payload;
    try {
      payload = JSON.parse((await readBody(req)) || '{}');
    } catch {
      return sendJSON(res, 400, { error: 'invalid JSON body' });
    }
    autoReply = !!payload.enabled;
    if (autoReply) {
      // Answer only messages from this point on; wipe stale per-chat context.
      autoReplyEnabledAt = Math.floor(Date.now() / 1000);
      chatHistory.clear();
      lastAutoReplyAt.clear();
      autoReplyCount.clear();
    }
    persistAutoReply();
    const hasKey = !!GEMINI_API_KEY;
    console.log(`[venom-whatsapp] auto-reply ${autoReply ? 'ON' : 'OFF'}`);
    return sendJSON(res, 200, { autoReply, hasKey });
  }

  if (route === '/qr') {
    if (!currentQR) {
      res.writeHead(loggedIn ? 200 : 204, { 'Content-Type': 'text/plain' });
      return res.end(loggedIn ? 'already linked\n' : '');
    }
    const ascii = await QRCode.toString(currentQR, { type: 'terminal', small: true });
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    return res.end(ascii);
  }

  if (route === '/qr.png') {
    if (!currentQR) {
      return sendJSON(res, loggedIn ? 200 : 404, { loggedIn });
    }
    const png = await QRCode.toBuffer(currentQR, { width: 320, margin: 2 });
    res.writeHead(200, { 'Content-Type': 'image/png' });
    return res.end(png);
  }

  if (route === '/contacts') {
    const q = (url.searchParams.get('q') || '').toLowerCase().trim();
    const out = [];
    for (const [jid, name] of contacts) {
      if (!q || name.toLowerCase().includes(q)) out.push({ jid, name });
      if (out.length >= 50) break;
    }
    return sendJSON(res, 200, { contacts: out });
  }

  if (route === '/send' && req.method === 'POST') {
    if (!loggedIn) {
      return sendJSON(res, 503, { error: 'WhatsApp is not linked yet' });
    }
    let payload;
    try {
      payload = JSON.parse((await readBody(req)) || '{}');
    } catch {
      return sendJSON(res, 400, { error: 'invalid JSON body' });
    }
    const text = (payload.text || '').toString().trim();
    if (!text) return sendJSON(res, 400, { error: 'text is required' });

    const resolved = await resolveRecipient(payload.to);
    if (resolved.error) return sendJSON(res, 404, { error: resolved.error });
    if (resolved.candidates) {
      return sendJSON(res, 300, {
        error: 'ambiguous recipient',
        candidates: resolved.candidates,
      });
    }
    try {
      await sendText(resolved.jid, text);
      lastChatJid = isJidUser(resolved.jid) ? resolved.jid : lastChatJid;
      const name = contacts.get(resolved.jid) || resolved.jid.split('@')[0];
      return sendJSON(res, 200, { ok: true, to: resolved.jid, name });
    } catch (err) {
      logger.warn({ err }, 'send failed');
      return sendJSON(res, 500, { error: `send failed: ${err.message}` });
    }
  }

  return sendJSON(res, 404, { error: 'not found' });
});

server.listen(PORT, HOST, () => {
  console.log(`[venom-whatsapp] HTTP API on http://${HOST}:${PORT}`);
  console.log(`[venom-whatsapp] state dir: ${STATE_DIR}`);
  if (NTFY_TOPIC) {
    console.log(`[venom-whatsapp] forwarding incoming to ${NTFY_SERVER}/${NTFY_TOPIC}`);
  }
});

start().catch((err) => {
  console.error('[venom-whatsapp] fatal start error:', err);
  process.exit(1);
});
