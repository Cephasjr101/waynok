/* Waynok service worker — offline shell + Web Push notifications */
self.addEventListener("install", (e) => { self.skipWaiting(); });

self.addEventListener("activate", (e) => {
  e.waitUntil(clients.claim());
});

self.addEventListener("push", (e) => {
  let payload = { title: "Waynok", body: "You have a new update." };
  try { payload = Object.assign(payload, e.data.json()); } catch (err) {}
  e.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag: "waynok-" + Date.now(),
      data: { url: payload.url || "/#msgs" },
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ("focus" in c) { c.navigate(e.notification.data.url); return c.focus(); }
      }
      return clients.openWindow(e.notification.data.url);
    })
  );
});
