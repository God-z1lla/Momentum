const CACHE_NAME = 'routine-tracker-v1';
const APP_SHELL = ['/static/css/style.css', '/static/js/app.js', '/static/manifest.webmanifest'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});

self.addEventListener('push', (event) => {
    const data = event.data ? event.data.json() : {title: 'Routine Tracker', body: 'You have a routine reminder.'};
    event.waitUntil(self.registration.showNotification(data.title, {body: data.body, data: data.url || '/', silent: false}));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(clients.openWindow(event.notification.data || '/'));
});
