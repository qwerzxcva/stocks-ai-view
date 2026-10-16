// sw.js — Service worker for the cloud-deployed dashboard.
//
// The local data/ directory no longer exists, so there is nothing to cache
// for data: everything is fetched from the cloud warehouse at request time
// (see data_origin_patch.js). This worker only caches the app shell so the
// page still opens offline.
const CACHE = 'stocks-ai-view-v15';   // v15: shell cache bust after stale-data incident
const SHELL = ['./', './index.html', './db-client.js', './realtime.js',
               './data_origin_patch.js', './stock-detail.js'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => c.addAll(SHELL.filter(u => u !== './sw.js')))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch (_) { return; }
  if (url.origin !== location.origin) return;   // cross-origin: not ours

  // App shell + same-origin assets: network-first, fall back to cache.
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.ok) cache.put(req, fresh.clone());
      return fresh;
    } catch (_) {
      const hit = await cache.match(req);
      return hit || new Response('', { status: 503 });
    }
  })());
});
