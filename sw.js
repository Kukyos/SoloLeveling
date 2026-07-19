self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith("/api")) return; // live data, never cached
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open("polymath-v1").then(c => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request))
  );
});
