/// <reference types="@sveltejs/kit" />

import { build, files, version } from '$service-worker';

const CACHE = `spielplan-shell-${version}`;

// addAll is all-or-nothing: a half-cached shell that boots into a missing chunk is worse than none.
// '/' bypasses the HTTP cache, which may still hold the previous release's index.html.
const SHELL = [...build, ...files, new Request('/', { cache: 'reload' })];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Never cache /api: a cached /api/rate card would be answered twice.
  if (url.pathname.startsWith('/api/')) return;

  // Build assets are content-hashed, so cache-first is safe.
  if (build.includes(url.pathname) || files.includes(url.pathname)) {
    event.respondWith(caches.match(request).then((hit) => hit ?? fetch(request)));
    return;
  }

  // Network first: index.html is not content-hashed, so cache-first would pin the old release.
  // The backend answers every navigation 200, so a 404 or 5xx is the proxy's page during a restart.
  if (request.mode === 'navigate') {
    const shell = (fallback) => caches.match('/').then((hit) => hit ?? fallback);
    event.respondWith(
      fetch(request)
        .then((res) => (res.status === 404 || res.status >= 500 ? shell(res) : res))
        .catch(() => shell(Response.error()))
    );
  }
});

// A dumb renderer: the notification copy is the sender's.
self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { body: event.data ? event.data.text() : '' };
  }

  const title = payload.title || 'Spielplan';
  event.waitUntil(
    self.registration.showNotification(title, {
      body: payload.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      // A tag makes a second prompt for the same title replace the first.
      tag: payload.tag || 'spielplan',
      data: { url: payload.url || '/' }
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/', self.location.origin);

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (new URL(client.url).origin === target.origin && 'focus' in client) {
          if ('navigate' in client && client.url !== target.href) client.navigate(target.href);
          return client.focus();
        }
      }
      return self.clients.openWindow(target.href);
    })
  );
});
