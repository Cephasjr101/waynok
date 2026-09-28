const CACHE = "waynok-v1";
const SHELL = ["/", "/static/index.html", "/manifest.json", "/static/icon-192.png", "/static/icon-512.png"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/auth") || url.pathname.startsWith("/api")
      || url.pathname.startsWith("/loads") || url.pathname.startsWith("/trucks") || url.pathname.startsWith("/drivers")
      || url.pathname.startsWith("/offers") || url.pathname.startsWith("/conversations") || url.pathname.startsWith("/me")
      || url.pathname.startsWith("/payments") || url.pathname.startsWith("/agent") || url.pathname.startsWith("/maps")
      || url.pathname.startsWith("/ratings") || url.pathname.startsWith("/admin")) {
    return; // API: network only
  }
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      if (res.ok && (url.origin === location.origin)) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match("/")))
  );
});
