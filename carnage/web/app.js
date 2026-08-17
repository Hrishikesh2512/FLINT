/* The page half of the phone body.
 *
 * Three jobs, and only three:
 *
 *   1. give her the phone's senses — GPS and battery, posted on a slow tick
 *   2. carry a conversation, in text or dictated
 *   3. open the SMS app when she has a message that needs a human tap
 *
 * The third is the honest limit of a web page and the reason it is visible in
 * the interface rather than hidden: a browser cannot send an SMS, so the page
 * shows a button and the person presses it. Anything that made that look like
 * it had already been sent would be a lie at the worst possible moment.
 *
 * The token lives in localStorage, put there by the ?k= in the link you first
 * opened. It is stripped from the URL immediately so it does not sit in
 * history or get shared when the page is.
 */
'use strict';

const LOG = document.getElementById('log');
const OUTBOX = document.getElementById('outbox');
const DOT = document.getElementById('dot');
const SUB = document.getElementById('sub');
const TEXT = document.getElementById('text');
const SEND = document.getElementById('send');
const MIC = document.getElementById('mic');

/* How often the phone's senses are posted. Slow on purpose: a GPS fix every
 * few seconds would be a measurable battery drain for a fact that changes
 * when you walk somewhere, not when you blink. */
const TICK_MS = 45000;

// ── token ───────────────────────────────────────────────────────────────────
const url = new URL(location.href);
const fromLink = url.searchParams.get('k');
if (fromLink) {
  localStorage.setItem('carnage.token', fromLink);
  url.searchParams.delete('k');
  history.replaceState({}, '', url.pathname + url.hash);
}
const TOKEN = localStorage.getItem('carnage.token') || '';

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(TOKEN ? { Authorization: 'Bearer ' + TOKEN } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) throw new Error('unauthorised');
  if (!res.ok) throw new Error('http ' + res.status);
  return res.json();
}

// ── the conversation ────────────────────────────────────────────────────────
function turn(who, text) {
  const div = document.createElement('div');
  div.className = 'turn ' + who;
  div.textContent = text;
  LOG.appendChild(div);
  LOG.scrollTop = LOG.scrollHeight;
  return div;
}

function note(text, warn) {
  const div = document.createElement('div');
  div.className = 'note' + (warn ? ' warn' : '');
  div.textContent = text;
  LOG.appendChild(div);
  LOG.scrollTop = LOG.scrollHeight;
}

let busy = false;
async function say(text) {
  text = (text || '').trim();
  if (!text || busy) return;
  busy = true;
  SEND.disabled = true;
  TEXT.value = '';
  turn('him', text);
  const waiting = turn('her', '…');
  try {
    const reply = await api('/api/ask', { text });
    waiting.textContent = reply.said || '…';
    speak(reply.said);
  } catch (err) {
    waiting.textContent = err.message === 'unauthorised'
      ? "This link has gone stale — open the one with the key in it again."
      : "I couldn't reach the rest of me just then.";
  } finally {
    busy = false;
    SEND.disabled = false;
    // Push senses straight after a turn: she may have just been asked where
    // he is, and a 45-second-old fix is the wrong answer to that.
    report();
  }
}

document.getElementById('say').addEventListener('submit', (e) => {
  e.preventDefault();
  say(TEXT.value);
});

// ── her voice, and his ──────────────────────────────────────────────────────
function speak(text) {
  if (!text || !('speechSynthesis' in window)) return;
  try {
    const utter = new SpeechSynthesisUtterance(text);
    utter.rate = 1.05;
    // Hinglish reads far better on an Indian English voice than on the
    // browser default, which sounds American and mangles the Hindi words.
    const voice = speechSynthesis.getVoices()
      .find((v) => /en[-_]IN/i.test(v.lang) || /hi[-_]IN/i.test(v.lang));
    if (voice) utter.voice = voice;
    speechSynthesis.cancel();
    speechSynthesis.speak(utter);
  } catch { /* a voice is a nicety; never let it break the reply */ }
}

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let listening = null;
MIC.addEventListener('click', () => {
  if (!Recognition) { note('This browser has no dictation.', true); return; }
  if (listening) { listening.stop(); return; }
  const rec = new Recognition();
  rec.lang = 'en-IN';
  rec.interimResults = false;
  rec.onresult = (e) => say(e.results[0][0].transcript);
  rec.onend = () => { listening = null; MIC.setAttribute('aria-pressed', 'false'); };
  rec.onerror = () => note('Didn\'t catch that.', true);
  rec.start();
  listening = rec;
  MIC.setAttribute('aria-pressed', 'true');
  if (navigator.vibrate) navigator.vibrate(15);
});

// ── the phone's senses ──────────────────────────────────────────────────────
function locate() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        accuracy: pos.coords.accuracy,
      }),
      // A refusal is not an error worth shouting about — she simply says she
      // can't see where he is, which is true.
      () => resolve(null),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
  });
}

async function power() {
  if (!navigator.getBattery) return null;
  try {
    const b = await navigator.getBattery();
    return { percent: Math.round(b.level * 100), charging: b.charging };
  } catch { return null; }
}

async function report() {
  try {
    const [location, battery] = await Promise.all([locate(), power()]);
    const reply = await api('/api/report', { location, battery });
    showOutbox(reply.outbox || []);
    live(true);
  } catch {
    live(false);
  }
}

// ── the messages only he can send ───────────────────────────────────────────
function showOutbox(messages) {
  OUTBOX.textContent = '';
  OUTBOX.hidden = messages.length === 0;
  for (const m of messages) {
    const link = document.createElement('a');
    // The sms: scheme is the only way a page may touch messaging, and it can
    // only pre-fill. That is exactly what this button says it does.
    link.href = `sms:${m.to}?body=${encodeURIComponent(m.text)}`;
    link.textContent = `Tap to send: ${m.to}`;
    OUTBOX.appendChild(link);
  }
  if (messages.length && navigator.vibrate) navigator.vibrate([60, 40, 60]);
}

// ── status ──────────────────────────────────────────────────────────────────
function live(ok) {
  DOT.classList.toggle('on', !!ok);
}

async function refresh() {
  try {
    const s = await api('/api/status');
    live(true);
    const others = (s.devices || [])
      .filter((d) => /Reachable/i.test(d.presence)).length;
    SUB.textContent = `${s.tools} tools · ${others} other bod${others === 1 ? 'y' : 'ies'} up`;
    document.title = 'Jarvis';
  } catch (err) {
    live(false);
    SUB.textContent = err.message === 'unauthorised' ? 'no key' : 'offline';
  }
}

// ── boot ────────────────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

refresh();
report();
setInterval(report, TICK_MS);
setInterval(refresh, TICK_MS);
// Coming back to a backgrounded tab should not show a stale reading.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { refresh(); report(); }
});

note('She has the same memory here as on your other devices.');
