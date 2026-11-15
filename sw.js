/* Phoenix service worker: web push only. No caching, so the app always loads fresh from Vercel.
   A push carries {title, body, url, tag}; tapping the notification opens (or focuses) Phoenix at that url. */
self.addEventListener("install", function(){ self.skipWaiting(); });
self.addEventListener("activate", function(e){ e.waitUntil(self.clients.claim()); });

self.addEventListener("push", function(e){
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch(_) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Phoenix", {
    body: d.body || "",
    tag: d.tag || undefined,
    icon: "/icons/phoenix-192.png",
    badge: "/icons/phoenix-192.png",
    data: { url: d.url || "/#mission" }
  }));
});

self.addEventListener("notificationclick", function(e){
  e.notification.close();
  var url = new URL((e.notification.data && e.notification.data.url) || "/#mission", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function(list){
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (c.url.indexOf(self.location.origin) === 0) {
        c.postMessage({ px: "open", url: url });
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
