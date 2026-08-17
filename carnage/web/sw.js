/* A deliberately small service worker.
 *
 * Its only job is to make the page open instantly and survive a dead network
 * long enough to say so. It caches the shell — markup, script, icon — and
 * never the API, because a cached answer from her would be a remembered
 * answer presented as a current one, which is the failure this whole codebase
 * keeps refusing to make.
 */
const SHELL = 'carnage-shell-v1';
const FILES = ['/', '/index.html', '/app.js', '/icon.svg', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return;      // never cached, see above
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request).then((hit) => hit
      || caches.match('/index.html'))));
});
