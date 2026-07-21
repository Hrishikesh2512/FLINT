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

function digitsOnly(s) {
  return (s || '').replace(/[^0-9]/g, '');
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
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (!msg.message || msg.key.fromMe) continue;
      const jid = msg.key.remoteJid;
      // Learn the sender's name; forward only 1:1 chats (skip groups/status).
      if (msg.pushName) rememberContact(jid, msg.pushName);
      if (!isJidUser(jid)) continue;
      lastChatJid = jidNormalizedUser(jid);
      const text = messageText(msg);
      const sender = msg.pushName || contacts.get(lastChatJid) || jid.split('@')[0];
      if (text) await forwardIncoming(jid, sender, text);
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
    });
  }

  if (!authed(req)) return sendJSON(res, 401, { error: 'unauthorized' });

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
      await sock.sendMessage(resolved.jid, { text });
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
