// sw.js — stocks-ai-view Service Worker
// 策略:
//   - HTML / JS / CSS (应用外壳): cache-first + 后台更新 (首屏快)
//   - data/*.json: network-first, 失败回退缓存 (数据新鲜优先)
//     * 例外: index_kline.json / search_index.json 等大文件走 stale-while-revalidate
//   - 跨域请求 (腾讯行情等): 不走 SW, 由 realtime.js 直连
const CACHE = 'stocks-ai-view-v2';
const SHELL = ['./', './index.html', './realtime.js', './sw.js',
               './data/latest.json'];

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
  if (url.origin !== location.origin) return;   // 跨域不走 SW

  // HTML 导航: network-first (保证拿到最新版本), 失败用缓存
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

  const path = url.pathname;
  const isJson = path.endsWith('.json');
  const isShell = /\.(js|css)$/.test(path) || path.endsWith('.html');

  // 大块/低频 JSON: stale-while-revalidate (秒开 + 后台更新)
  const swrJson = /(index_kline|search_index|multi_v4|lowpool|cycle|sector_pool|hotspot_infer|hot_impact)\.json$/.test(path);

  if (isJson && !swrJson) {
    // latest.json 等高频数据: network-first
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok) cache.put(req, fresh.clone());
        return fresh;
      } catch (_) {
        const hit = await cache.match(req);
        return hit || new Response('{}', { status: 503, headers: { 'Content-Type': 'application/json' } });
      }
    })());
    return;
  }

  if (isJson && swrJson) {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const hit = await cache.match(req);
      const net = fetch(req).then(r => {
        if (r && r.ok) cache.put(req, r.clone());
        return r;
      }).catch(() => null);
      return hit || await net || new Response('{}', { status: 503, headers: { 'Content-Type': 'application/json' } });
    })());
    return;
  }

  if (isShell) {
    // 应用外壳: cache-first + 后台刷新
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const hit = await cache.match(req);
      const net = fetch(req).then(r => {
        if (r && r.ok) cache.put(req, r.clone());
        return r;
      }).catch(() => null);
      if (hit) { e.waitUntil(net); return hit; }
      return await net || new Response('', { status: 503 });
    })());
    return;
  }

  // 其他静态资源: cache-first
  e.respondWith(caches.match(req).then(h => h || fetch(req)));
});
