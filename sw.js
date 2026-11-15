const CACHE_NAME = 'ubuntu-monitor-v2';
const CRITICAL_LOCAL_ASSETS = [
  '/',
  '/manifest.json',
  '/icon.svg'
];

const CDN_EXTERNAL_ASSETS = [
  'https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap',
  'https://cdn.jsdelivr.net/npm/chart.js',
  'https://unpkg.com/lucide@latest'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      // 1. Mise en cache critique des fichiers locaux (obligatoire pour le succès du SW)
      await cache.addAll(CRITICAL_LOCAL_ASSETS);

      // 2. Mise en cache best-effort des ressources CDN externes (non-bloquante)
      CDN_EXTERNAL_ASSETS.forEach((url) => {
        fetch(url)
          .then((response) => {
            if (response.ok) {
              cache.put(url, response);
            }
          })
          .catch(() => {
            // Tolérance aux pannes réseau / CDN sans bloquer l'installation
          });
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Pass through WebSocket and API/metrics requests dynamically
  if (event.request.url.includes('/ws') || event.request.url.includes('/metrics')) {
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        // Fetch fresh in background
        fetch(event.request).then((networkResponse) => {
          if (networkResponse.status === 200) {
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, networkResponse));
          }
        }).catch(() => {});
        return cachedResponse;
      }
      return fetch(event.request);
    })
  );
});
