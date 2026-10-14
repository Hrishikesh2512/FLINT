/*
 * One-time Google sign-in for Jarvis's photo archive.
 *
 *   sudo -u venom WA_STATE_DIR=/var/lib/venom/whatsapp-jarvis \
 *     node /opt/venom/whatsapp-service/gphotos-auth.js
 *
 * Google's loopback flow: this listens on 127.0.0.1:8791 on the Pi, so from
 * the laptop open an SSH tunnel first —
 *   ssh -L 8791:127.0.0.1:8791 tushar@<pi>
 * then open the printed link in the laptop's browser and sign in with the
 * photos account. Google redirects back through the tunnel, and the refresh
 * token is saved to <state>/gphotos_token.json (0600).
 */

'use strict';

const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { SCOPES, readClient } = require('./gphotos');

const STATE_DIR = process.env.WA_STATE_DIR || path.join(__dirname, 'state');
const CLIENT_FILE = process.env.GPHOTOS_CLIENT_FILE || '/etc/venom/gphotos_client.json';
const PORT = parseInt(process.env.GPHOTOS_AUTH_PORT || '8791', 10);
const REDIRECT = `http://127.0.0.1:${PORT}/`;

const { clientId, clientSecret } = readClient(CLIENT_FILE);
const verifier = crypto.randomBytes(48).toString('base64url');
const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
const state = crypto.randomBytes(16).toString('hex');

const authUrl = 'https://accounts.google.com/o/oauth2/v2/auth?' + new URLSearchParams({
  client_id: clientId,
  redirect_uri: REDIRECT,
  response_type: 'code',
  scope: SCOPES.join(' '),
  access_type: 'offline',
  prompt: 'consent',          // always hand back a refresh token
  code_challenge: challenge,
  code_challenge_method: 'S256',
  state,
});

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, REDIRECT);
  if (url.pathname !== '/') { res.writeHead(404); return res.end(); }
  const code = url.searchParams.get('code');
  if (url.searchParams.get('state') !== state || !code) {
    res.writeHead(400, { 'Content-Type': 'text/plain' });
    return res.end(`Sign-in failed: ${url.searchParams.get('error') || 'bad state'}`);
  }
  try {
    const resp = await fetch('https://oauth2.googleapis.com/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        code, client_id: clientId, client_secret: clientSecret,
        redirect_uri: REDIRECT, grant_type: 'authorization_code', code_verifier: verifier,
      }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.refresh_token) throw new Error(data.error_description || data.error || 'no refresh token');
    fs.mkdirSync(STATE_DIR, { recursive: true });
    const file = path.join(STATE_DIR, 'gphotos_token.json');
    fs.writeFileSync(file, JSON.stringify({ refresh_token: data.refresh_token, scope: data.scope, created: Date.now() }),
      { mode: 0o600 });
    res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('Done — Jarvis can use Google Photos now. You can close this tab.');
    console.log(`\nSaved ${file}. Restart the bridge: sudo systemctl restart venom-whatsapp-jarvis`);
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'text/plain' });
    res.end(`Token exchange failed: ${err.message}`);
    console.error('Token exchange failed:', err.message);
    process.exitCode = 1;
  }
  setTimeout(() => process.exit(), 500);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`\n1) On the laptop, keep a tunnel open:  ssh -L ${PORT}:127.0.0.1:${PORT} tushar@<pi>`);
  console.log('2) Open this link in the laptop browser and sign in with the photos account:\n');
  console.log(authUrl + '\n');
  console.log('Waiting for Google to redirect back…');
});
