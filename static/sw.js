self.addEventListener('push', function(event) {
  const data = event.data ? event.data.json() : {};
  const title = data.title || 'Propilot';
  const options = {
    body:    data.body  || '',
    icon:    '/static/icon.png',
    badge:   '/static/icon.png',
    tag:     data.tag   || 'propilot-slip',
    renotify: true,
    data:    { url: data.url || '/autopilot' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data.url));
});
