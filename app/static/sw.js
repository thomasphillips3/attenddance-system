// AttenDANCE Service Worker
// Strategy:
//   - Navigations (HTML pages): NETWORK-FIRST. Authenticated pages are never
//     cached (avoids serving stale or cross-session content). Falls back to a
//     minimal offline notice only when truly offline.
//   - API (/api/...): NETWORK-ONLY — always fresh (balances, payments, etc.).
//   - Static assets (/static/...) + CDN libs: CACHE-FIRST (safe, versioned).
const CACHE_NAME = 'attenddance-v3';
// The tap station page holds no student data (everything comes from /api), so
// it is the one HTML page cached: a station can reload with no Wi-Fi and keep
// queuing taps.
const STATION_URL = '/tap-in/station';

// Only precache things safe to serve to anyone, anytime (no authenticated HTML).
const PRECACHE_URLS = [
    '/static/manifest.json',
    '/static/icons/icon-192x192.png',
    '/static/icons/icon-512x512.png',
    'https://cdn.tailwindcss.com',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css',
    'https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js',
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(PRECACHE_URLS)).catch(() => {})
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

function offlinePage() {
    return new Response(
        '<html><body style="font-family:sans-serif;text-align:center;padding:3rem;color:#475569">' +
        '<h2>You’re offline</h2><p>Reconnect to use AttenDANCE.</p></body></html>',
        { headers: { 'Content-Type': 'text/html' } }
    );
}

function notRecordedPage() {
    return new Response(
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<title>Not recorded</title></head>' +
        '<body style="margin:0;font-family:system-ui,sans-serif;background:#c62f2f;color:#fff;' +
        'min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;' +
        'text-align:center;padding:2rem;box-sizing:border-box">' +
        '<div style="font-size:4rem;line-height:1">&#10007;</div>' +
        '<h1 style="font-size:2.4rem;margin:.5rem 0">NOT RECORDED</h1>' +
        '<p style="font-size:1.2rem;max-width:22rem">This phone has no internet. ' +
        'Tap the tag again once it is back online, or mark the dancer on the attendance card.</p>' +
        '</body></html>',
        { status: 503, headers: { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' } }
    );
}

function isStaticAsset(url) {
    return url.startsWith(self.location.origin + '/static/');
}
function isCDN(url) {
    return url.startsWith('https://cdn.tailwindcss.com')
        || url.startsWith('https://cdnjs.cloudflare.com')
        || url.startsWith('https://unpkg.com');
}

self.addEventListener('fetch', event => {
    const { request } = event;
    if (request.method !== 'GET') return;

    const url = request.url;

    // API: always network (never cache app data).
    if (url.includes('/api/')) return;

    const isNavigation = request.mode === 'navigate'
        || (request.headers.get('accept') || '').includes('text/html');

    if (isNavigation) {
        const path = new URL(url).pathname;
        if (path === STATION_URL) {
            event.respondWith(
                fetch(request).then(resp => {
                    if (resp && resp.status === 200 && !resp.redirected) {
                        const clone = resp.clone();
                        caches.open(CACHE_NAME).then(c => c.put(STATION_URL, clone));
                    }
                    return resp;
                }).catch(() => caches.match(STATION_URL).then(cached => cached || offlinePage()))
            );
            return;
        }
        if (path.startsWith('/t/')) {
            // A tag tapped with no connection: say plainly it did not count.
            event.respondWith(fetch(request).catch(() => notRecordedPage()));
            return;
        }
        event.respondWith(fetch(request).catch(() => offlinePage()));
        return;
    }

    if (isStaticAsset(url) || isCDN(url)) {
        event.respondWith(
            caches.match(request).then(cached => cached || fetch(request).then(resp => {
                if (resp && resp.status === 200) {
                    const clone = resp.clone();
                    caches.open(CACHE_NAME).then(c => c.put(request, clone));
                }
                return resp;
            }).catch(() => cached))
        );
    }
});

// --- Push notifications (kept for future use) ---
self.addEventListener('push', event => {
    if (!event.data) return;
    let data = {};
    try { data = event.data.json(); } catch (e) { data = { body: event.data.text() }; }
    event.waitUntil(self.registration.showNotification(data.title || 'AttenDANCE', {
        body: data.body || '',
        icon: '/static/icons/icon-192x192.png',
        badge: '/static/icons/icon-96x96.png',
        data: data.data || {},
    }));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    event.waitUntil(clients.openWindow((event.notification.data && event.notification.data.url) || '/'));
});

self.addEventListener('message', event => {
    if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
