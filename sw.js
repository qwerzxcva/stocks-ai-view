// sw.js — stocks-ai-view Service Worker
// 策略:
//   - HTML (navigate): network-first, 失败回退缓存 (保证永远能打开)
//   - JSON 数据: stale-while-revalidate (先给缓存秒开, 后台更新)
//   - 其他静态资源: cache-first
const CACHE = 'stocks-ai-view-v1';
const DATA_RE = /\/data\/.*\.json$/;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll([
    './', './index.html',
    './data/latest.json'
  ])).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 跨域(腾讯行情)不走 SW

  // HTML 导航: network-first
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
        return r;
      }).catch(() => caches.match(req).then(r => r || caches.match('./index.html')))
    );
    return;
  }

  // JSON 数据: stale-while-revalidate —— 首屏零等待
  if (DATA_RE.test(url.pathname)) {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match(req);
      const network = fetch(req).then(r => {
        if (r && r.ok) cache.put(req, r.clone());
        return r;
      }).catch(() => null);
      if (cached) {
        e.waitUntil(network);      // 后台刷新, 不阻塞首屏
        return cached;
      }
      const fresh = await network;
      return fresh || new Response('{}', {
        status: 503, headers: { 'Content-Type': 'application/json' }
      });
    })());
    return;
  }

  // 其它静态资源: cache-first
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
      return r;
    }))
  );
});
