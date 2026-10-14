/*
 * Google Photos for Jarvis's family photo archive — the API client and the
 * local index the bridge (index.js) and the one-time chat import
 * (import-export.js) share.
 *
 * Since March 2025 the Library API only lets an app see what it uploaded
 * itself, so the scopes are exactly that: upload, and read/edit our own
 * items and albums. Everything already in the library stays the owner's to
 * browse in the Photos app (whose face/place search covers our uploads too).
 *
 * Files (all under the bridge's state dir unless overridden):
 *   gphotos_token.json    refresh token from `node gphotos-auth.js` (0600)
 *   photos/index.jsonl    one line per archived photo/video (append-only)
 *   photos/albums.json    albums we created: id -> {title, group, ...}
 * The OAuth client (a "Desktop app" client downloaded from Google Cloud) is
 * read from GPHOTOS_CLIENT_FILE, default /etc/venom/gphotos_client.json.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const SCOPES = [
  'https://www.googleapis.com/auth/photoslibrary.appendonly',
  'https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata',
  'https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata',
];
const API = 'https://photoslibrary.googleapis.com/v1';
const TOKEN_URL = 'https://oauth2.googleapis.com/token';

function readClient(file) {
  const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  const c = raw.installed || raw.web || raw;
  if (!c.client_id || !c.client_secret) throw new Error(`${file} is not an OAuth client JSON`);
  return { clientId: c.client_id, clientSecret: c.client_secret };
}

class GooglePhotos {
  constructor({ stateDir, clientFile }) {
    this.clientFile = clientFile || process.env.GPHOTOS_CLIENT_FILE || '/etc/venom/gphotos_client.json';
    this.tokenFile = path.join(stateDir, 'gphotos_token.json');
    this._access = null;   // {token, expires}
  }

  /** True once `gphotos-auth.js` has been run and the client file exists. */
  configured() {
    return fs.existsSync(this.tokenFile) && fs.existsSync(this.clientFile);
  }

  async accessToken() {
    if (this._access && Date.now() < this._access.expires - 60_000) return this._access.token;
    const { clientId, clientSecret } = readClient(this.clientFile);
    const { refresh_token: refreshToken } = JSON.parse(fs.readFileSync(this.tokenFile, 'utf8'));
    const resp = await fetch(TOKEN_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        client_id: clientId, client_secret: clientSecret,
        refresh_token: refreshToken, grant_type: 'refresh_token',
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.access_token) {
      // invalid_grant = the refresh token died (app left in "Testing" → 7-day
      // expiry, or access revoked). Only re-running the auth fixes it.
      throw new Error(`Google token refresh failed: ${data.error || resp.status}` +
        (data.error === 'invalid_grant' ? ' — run gphotos-auth.js again' : ''));
    }
    this._access = { token: data.access_token, expires: Date.now() + (data.expires_in || 3600) * 1000 };
    return this._access.token;
  }

  async _json(method, url, body) {
    const resp = await fetch(url, {
      method,
      headers: { Authorization: `Bearer ${await this.accessToken()}`, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(`Photos API ${method} ${url.replace(API, '')}: ${resp.status} ${data.error && data.error.message || ''}`);
    return data;
  }

  async createAlbum(title) {
    const a = await this._json('POST', `${API}/albums`, { album: { title: title.slice(0, 500) } });
    return a.id;
  }

  async renameAlbum(id, title) {
    await this._json('PATCH', `${API}/albums/${encodeURIComponent(id)}?updateMask=title`, { title: title.slice(0, 500) });
  }

  /**
   * Upload raw bytes (a Buffer, or a Node stream for big videos) and get an
   * upload token. Streams go up as they arrive — a 200 MB video never sits
   * in the Pi's 2 GB of RAM.
   */
  async upload(data, fileName, mimeType) {
    const { Readable } = require('stream');
    const isStream = data && typeof data.pipe === 'function';
    const resp = await fetch(`${API}/uploads`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${await this.accessToken()}`,
        'Content-Type': 'application/octet-stream',
        'X-Goog-Upload-Content-Type': mimeType,
        'X-Goog-Upload-Protocol': 'raw',
        'X-Goog-Upload-File-Name': encodeURIComponent(fileName),
      },
      body: isStream ? Readable.toWeb(data) : data,
      ...(isStream ? { duplex: 'half' } : {}),
    });
    const token = await resp.text();
    if (!resp.ok || !token) throw new Error(`Photos upload failed: ${resp.status} ${token.slice(0, 200)}`);
    return token;
  }

  /** Turn an upload token into a library item, in an album we created. */
  async createItem(uploadToken, { albumId, description, fileName }) {
    const res = await this._json('POST', `${API}/mediaItems:batchCreate`, {
      ...(albumId ? { albumId } : {}),
      newMediaItems: [{
        description: (description || '').slice(0, 1000),
        simpleMediaItem: { uploadToken, fileName },
      }],
    });
    const r = (res.newMediaItemResults || [])[0] || {};
    const ok = r.status && (r.status.message === 'Success' || r.status.message === 'OK' || !r.status.code);
    if (!ok || !r.mediaItem) throw new Error(`Photos create failed: ${JSON.stringify(r.status || res).slice(0, 200)}`);
    return r.mediaItem;
  }

  /** Download one of OUR items back (full quality). */
  async download(id) {
    const item = await this._json('GET', `${API}/mediaItems/${encodeURIComponent(id)}`);
    const video = !!(item.mediaMetadata && item.mediaMetadata.video);
    const resp = await fetch(`${item.baseUrl}=${video ? 'dv' : 'd'}`);
    if (!resp.ok) throw new Error(`Photos download failed: ${resp.status}`);
    return { data: Buffer.from(await resp.arrayBuffer()), mime: item.mimeType, video, fileName: item.filename };
  }
}

// ── local index ──────────────────────────────────────────────────────────────
// Append-only JSONL, loaded whole at start: a family's photos are thousands
// of lines, not millions, and every query here is a simple scan.
class PhotoIndex {
  constructor(stateDir) {
    this.dir = path.join(stateDir, 'photos');
    fs.mkdirSync(this.dir, { recursive: true });
    this.file = path.join(this.dir, 'index.jsonl');
    this.albumsFile = path.join(this.dir, 'albums.json');
    this.items = [];
    this.bySha = new Map();
    try {
      for (const line of fs.readFileSync(this.file, 'utf8').split('\n')) {
        if (!line.trim()) continue;
        try { this._add(JSON.parse(line)); } catch { /* skip a torn line */ }
      }
    } catch { /* first run */ }
    try { this.albums = JSON.parse(fs.readFileSync(this.albumsFile, 'utf8')); } catch { this.albums = {}; }
  }

  _add(item) {
    this.items.push(item);
    if (item.sha) this.bySha.set(item.sha, item);
  }

  has(sha) { return !!sha && this.bySha.has(sha); }

  add(item) {
    this._add(item);
    fs.appendFileSync(this.file, JSON.stringify(item) + '\n');
    const a = this.albums[item.albumId];
    if (a) {
      const at = item.at || item.ts;
      a.count = (a.count || 0) + 1;
      a.last = Math.max(a.last || 0, at);
      a.first = Math.min(a.first || Infinity, at);
      this.saveAlbums();
    }
  }

  saveAlbums() {
    fs.writeFileSync(this.albumsFile, JSON.stringify(this.albums));
  }

  addAlbum(id, info) {
    this.albums[id] = { count: 0, ...info };
    this.saveAlbums();
  }

  albumsFor(group) {
    return Object.entries(this.albums)
      .filter(([, a]) => a.group === group)
      .map(([id, a]) => ({ id, ...a }))
      .sort((x, y) => (y.last || y.created || 0) - (x.last || x.created || 0));
  }

  itemsFor(group) {
    return this.items.filter((i) => i.group === group);
  }
}

// "14 Nov 2021" — album titles and captions, in the Pi's local time.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function dayTitle(ms) {
  const d = new Date(ms);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}
function monthTitle(ms) {
  const d = new Date(ms);
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

// Old JPEGs arrive stripped of EXIF (WhatsApp recompresses), and the Library
// API can't set a date — Google files an EXIF-less upload under TODAY. So a
// 2021 photo imported now would sit in 2026. Stamping DateTimeOriginal into
// the JPEG before upload puts it back in its year. Only ever ADDS a date to
// a JPEG that has none; never touches one that already has it.
function stampJpegDate(buf, ms) {
  try {
    const piexif = require('piexifjs');
    const bin = buf.toString('binary');
    let exif = {};
    try { exif = piexif.load(bin); } catch { exif = {}; }
    const has = exif.Exif && exif.Exif[piexif.ExifIFD.DateTimeOriginal];
    if (has) return buf;
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    const stamp = `${d.getFullYear()}:${p(d.getMonth() + 1)}:${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    exif = { '0th': exif['0th'] || {}, Exif: exif.Exif || {}, GPS: exif.GPS || {} };
    exif['0th'][piexif.ImageIFD.DateTime] = stamp;
    exif.Exif[piexif.ExifIFD.DateTimeOriginal] = stamp;
    exif.Exif[piexif.ExifIFD.DateTimeDigitized] = stamp;
    return Buffer.from(piexif.insert(piexif.dump(exif), bin), 'binary');
  } catch {
    return buf;   // a stamping failure must never lose the photo
  }
}

// When a photo was TAKEN, if its EXIF says (documents keep it; WhatsApp
// "photo" sends strip it). null when unknown.
async function takenAt(buf) {
  try {
    const exifr = require('exifr');
    const t = await exifr.parse(buf, ['DateTimeOriginal', 'CreateDate']);
    const d = t && (t.DateTimeOriginal || t.CreateDate);
    return d instanceof Date && !isNaN(d) ? d.getTime() : null;
  } catch { return null; }
}

// ── the archive: dedupe → album → date → upload, one item at a time ─────────
const crypto = require('crypto');
const EVENT_GAP_MS = 3 * 3600_000;        // photos within 3 h of each other = one event
const OLD_AFTER_MS = 2 * 24 * 3600_000;   // taken > 2 days before it was sent = an old photo

class PhotoArchive {
  constructor({ stateDir, clientFile, log = console }) {
    this.gp = new GooglePhotos({ stateDir, clientFile });
    this.index = new PhotoIndex(stateDir);
    this.log = log;
    this._chain = Promise.resolve();
  }

  configured() { return this.gp.configured(); }

  /**
   * Which album an item belongs in, creating it on first use.
   *  • an OLD photo (EXIF says taken long before it was sent — relatives
   *    dumping old pictures as documents) → "<Group> — Nov 2021";
   *  • otherwise an EVENT: items within 3 h of an existing event album of this
   *    group join it, else a new "<Group> — 26 Sep 2026" album starts.
   * Returns {albumId, title, at} — `at` is the time the item clusters on.
   */
  async albumFor(group, groupName, ts, taken) {
    const albums = this.index.albumsFor(group);
    if (taken && ts - taken > OLD_AFTER_MS) {
      const key = monthTitle(taken);
      const hit = albums.find((a) => a.type === 'month' && a.key === key);
      if (hit) return { albumId: hit.id, title: hit.title, at: taken };
      const title = `${groupName} — ${key}`;
      const id = await this.gp.createAlbum(title);
      this.index.addAlbum(id, { title, group, groupName, type: 'month', key, created: Date.now(), first: taken, last: taken });
      return { albumId: id, title, at: taken };
    }
    const hit = albums.find((a) => a.type === 'event'
      && ts >= (a.first || 0) - EVENT_GAP_MS && ts <= (a.last || 0) + EVENT_GAP_MS);
    if (hit) return { albumId: hit.id, title: hit.title, at: ts };
    const title = `${groupName} — ${dayTitle(ts)}`;
    const id = await this.gp.createAlbum(title);
    this.index.addAlbum(id, { title, group, groupName, type: 'event', created: Date.now(), first: ts, last: ts });
    return { albumId: id, title, at: ts };
  }

  /** Queue one item; resolves to {dup} | {item} | throws. Serialised. */
  archive(input) {
    const run = this._chain.then(() => this._archive(input));
    this._chain = run.catch(() => {});
    return run;
  }

  async _archive({ group, groupName, sender, caption, ts, sha, mime, fileName, getData }) {
    if (this.index.has(sha)) return { dup: true };
    let data = await getData();
    let taken = null;
    if (Buffer.isBuffer(data)) {
      sha = sha || crypto.createHash('sha256').update(data).digest('base64');
      if (this.index.has(sha)) return { dup: true };
      if (/^image\//.test(mime)) taken = await takenAt(data);
      // A dateless JPEG from before today would land under TODAY in Google
      // Photos — stamp the date it was shared.
      if (!taken && /jpe?g/i.test(mime) && Date.now() - ts > 24 * 3600_000) {
        data = stampJpegDate(data, ts);
      }
    }
    if (!sha) throw new Error('no content hash for a streamed item');
    const { albumId, title, at } = await this.albumFor(group, groupName, ts, taken);
    const uploadToken = await this.gp.upload(data, fileName, mime);
    const description = `Sent by ${sender || 'someone'} in ${groupName}` + (caption ? ` — ${caption}` : '');
    const media = await this.gp.createItem(uploadToken, { albumId, description, fileName });
    const item = {
      sha, id: media.id, albumId, album: title, group, groupName,
      sender: sender || '', caption: caption || '', ts, taken, at, mime, file: fileName,
    };
    this.index.add(item);
    return { item };
  }
}

module.exports = {
  GooglePhotos, PhotoIndex, PhotoArchive, SCOPES, readClient,
  dayTitle, monthTitle, stampJpegDate, takenAt,
};
