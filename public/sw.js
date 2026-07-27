/* 随行地图 MapGo — 应用壳缓存(地图与接口数据走网络) */
const CACHE = 'mapgo-shell-v5';
const SHELL = [
  './index.html', './css/style.css', './manifest.json',
  './js/main.js', './js/state.js',
  './js/services/store.js', './js/services/api.js', './js/services/format.js',
  './js/services/algo.js', './js/services/amap.js',
  './js/ui/dom.js', './js/ui/auth.js',
  './js/modes/registry.js', './js/modes/poi.js', './js/modes/route.js',
  './js/modes/plan.js', './js/modes/social.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;          // 高德等外域直连
  if (url.pathname.startsWith('/api/')) return;        // 接口不缓存
  if (url.pathname.startsWith('/_AMapService/')) return;
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, clone));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
