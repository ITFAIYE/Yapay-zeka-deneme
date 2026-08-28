// İtfaiye Yapay Zekâ Kılavuzu — Service Worker
// Amaç: "Ana ekrana ekle" ile uygulama gibi açılabilmesi (PWA) ve
// admin CBS sorusunu cevapladığında tarayıcı push bildirimi gösterebilmesi.
// Kapsamlı bir çevrimdışı önbellekleme yapmaz; bu kritik/canlı bir kılavuz
// aracı olduğu için eski/bayat içerik göstermek istemiyoruz.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = { title: "İtfaiye Yapay Zekâ Kılavuzu", body: "Yeni bir bildiriminiz var.", url: "/" };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (e) { /* düz metin gelirse varsayılanı kullan */ }

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if (c.url.includes(url) && "focus" in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
