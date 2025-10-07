// service-worker.js
const CACHE_NAME = 'insta-downloader-v1';
const urlsToCache = [
    '/',
    '/static/style.css',
    '/static/manifest.json',
    '/static/logo.png'
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
        .then(cache => cache.addAll(urlsToCache))
    );
});

self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request)
        .then(response => response || fetch(event.request))
    );
});