/*
 * One-time import of a WhatsApp group's OLD photos into Jarvis's archive,
 * from the group's "Export chat → Include media" zip.
 *
 *   sudo -u venom WA_STATE_DIR=/var/lib/venom/whatsapp-jarvis \
 *     node /opt/venom/whatsapp-service/import-export.js <export.zip | folder> \
 *       --group <group id> --name "Family" [--dry-run] [--limit N] [--mdy]
 *
 * <group id> is what "jarvis photos" prints in that group, so imported photos
 * share its albums and "jarvis … ki photos bhej" finds them. The chat log
 * gives each photo its sender, date and caption; albums cluster by date the
 * same way live photos do. Re-running is safe: anything already archived
 * (same file content) is skipped, so an interrupted import just resumes.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');
const { PhotoArchive } = require('./gphotos');

const STATE_DIR = process.env.WA_STATE_DIR || path.join(__dirname, 'state');

function args() {
  const a = process.argv.slice(2);
  const out = { src: '', group: '', name: '', dry: false, limit: Infinity, mdy: null };
  for (let i = 0; i < a.length; i++) {
    if (a[i] === '--group') out.group = a[++i];
    else if (a[i] === '--name') out.name = a[++i];
    else if (a[i] === '--dry-run') out.dry = true;
    else if (a[i] === '--limit') out.limit = parseInt(a[++i], 10);
    else if (a[i] === '--mdy') out.mdy = true;
    else if (!out.src) out.src = a[i];
  }
  if (!out.src || !out.group) {
    console.error('usage: import-export.js <export.zip|folder> --group <group id> --name "Family" [--dry-run] [--limit N] [--mdy]');
    process.exit(2);
  }
  if (!out.name) out.name = 'Family';
  return out;
}

const MEDIA = {
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.heic': 'image/heic', '.gif': 'image/gif',
  '.mp4': 'video/mp4', '.3gp': 'video/3gpp', '.mov': 'video/quicktime', '.m4v': 'video/x-m4v',
};
// Stickers are .webp, voice notes .opus — neither belongs in a photo album.

// Android: "14/11/21, 9:03 pm - Papa: …"   iOS: "[14/11/21, 9:03:12 PM] Papa: …"
const LINE_ANDROID = /^(\d{1,2})\/(\d{1,2})\/(\d{2,4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?[\s ]*([ap]\.?m\.?)? - ([^:]+?): (.*)$/i;
const LINE_IOS = /^‎?\[(\d{1,2})\/(\d{1,2})\/(\d{2,4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?[\s ]*([ap]\.?m\.?)?\] ([^:]+?): (.*)$/i;
const ATTACH_IOS = /<attached: ([^>]+)>/i;
const ATTACH_ANDROID = /([^\s][^\n]*?\.[a-z0-9]{2,4}) \(file attached\)/i;

function parseChat(text, mdyFlag) {
  const raw = [];
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(LINE_IOS) || line.match(LINE_ANDROID);
    if (m) raw.push({ parts: m.slice(1, 8), sender: m[8].replace(/^‎/, '').trim(), text: m[9] });
    else if (raw.length) raw[raw.length - 1].text += '\n' + line;
  }
  // Day/month order: India writes DD/MM, but check — a first field over 12
  // settles it one way, a second field over 12 the other.
  let mdy = mdyFlag;
  if (mdy === null) {
    mdy = false;
    for (const r of raw) {
      if (+r.parts[0] > 12) { mdy = false; break; }
      if (+r.parts[1] > 12) { mdy = true; break; }
    }
  }
  return raw.map((r) => {
    const [a, b, y, hh, mi, ss, ap] = r.parts;
    const day = mdy ? +b : +a;
    const mon = (mdy ? +a : +b) - 1;
    let year = +y; if (year < 100) year += 2000;
    let hour = +hh;
    const pm = ap && /^p/i.test(ap);
    if (ap) { if (pm && hour < 12) hour += 12; if (!pm && hour === 12) hour = 0; }
    const ts = new Date(year, mon, day, hour, +mi, +(ss || 0)).getTime();
    const att = r.text.match(ATTACH_IOS) || r.text.match(ATTACH_ANDROID);
    const file = att ? att[1].replace(/^‎/, '').trim() : '';
    const caption = att ? r.text.replace(att[0], '').replace(/‎/g, '').trim() : '';
    return { ts, sender: r.sender, file, caption };
  }).filter((m) => m.file);
}

function sha256File(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('base64');
}

async function main() {
  const opt = args();
  let dir = opt.src;
  let tmp = '';
  if (fs.statSync(opt.src).isFile()) {
    tmp = fs.mkdtempSync(path.join(process.env.TMPDIR || '/var/tmp', 'wa-import-'));
    execFileSync('unzip', ['-q', '-o', opt.src, '-d', tmp]);
    dir = tmp;
  }
  try {
    const files = fs.readdirSync(dir);
    const chat = files.find((f) => /\.txt$/i.test(f));
    if (!chat) throw new Error('no chat .txt in the export — was it exported with the chat log?');
    const msgs = parseChat(fs.readFileSync(path.join(dir, chat), 'utf8'), opt.mdy)
      .filter((m) => MEDIA[path.extname(m.file).toLowerCase()] && fs.existsSync(path.join(dir, m.file)))
      .sort((x, y) => x.ts - y.ts);   // chronological, so events cluster like live
    console.log(`${msgs.length} photos/videos found in ${chat}` + (opt.dry ? ' (dry run)' : ''));

    const archive = new PhotoArchive({ stateDir: STATE_DIR });
    if (!opt.dry && !archive.configured()) throw new Error('Google Photos not set up — run gphotos-auth.js first');
    let done = 0, dup = 0, failed = 0;
    for (const m of msgs.slice(0, opt.limit)) {
      const file = path.join(dir, m.file);
      const mime = MEDIA[path.extname(m.file).toLowerCase()];
      const sha = sha256File(file);
      if (archive.index.has(sha)) { dup++; continue; }
      if (opt.dry) {
        console.log(`would add ${m.file}  ${new Date(m.ts).toISOString().slice(0, 16)}  ${m.sender}${m.caption ? '  "' + m.caption.slice(0, 40) + '"' : ''}`);
        done++;
        continue;
      }
      try {
        const big = fs.statSync(file).size > 25 * 1024 * 1024;
        const r = await archive.archive({
          group: opt.group, groupName: opt.name, sender: m.sender, caption: m.caption,
          ts: m.ts, sha, mime, fileName: m.file,
          // Big videos stream from disk; photos load so they can be dated.
          getData: async () => (big ? fs.createReadStream(file) : fs.readFileSync(file)),
        });
        if (r.dup) dup++; else done++;
        if ((done + dup) % 25 === 0) console.log(`… ${done} added, ${dup} already there`);
      } catch (err) {
        failed++;
        console.error(`failed ${m.file}: ${err.message}`);
        if (/token refresh failed/i.test(err.message)) throw err;
      }
    }
    console.log(`\nDone${opt.dry ? ' (dry run)' : ''}: ${done} ${opt.dry ? 'would be added' : 'added'}, ${dup} already archived, ${failed} failed.`);
  } finally {
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main().catch((err) => { console.error(err.message); process.exit(1); });
