/// <reference types="@sveltejs/kit" />
/**
 * The service worker. Spec v2.1 §6 preamble, §7.3.
 *
 * Two jobs, and a deliberate non-job.
 *
 *   1. **Web Push.** There is no such thing as a web push notification without a service
 *      worker: the push event is delivered here and nowhere else. §7.3's finish prompt is the
 *      first thing that will use it (M4 ships the sender; M2 ships the subscription and this).
 *   2. **The shell cache** §6's preamble asks for — the built JS/CSS and the static files, so
 *      the app opens on a phone that has just lost the WiFi at the end of the garden.
 *
 * And the non-job: **nothing under `/api` is ever cached or served from cache.** A cached
 * `/api/rate` card would hand back a card the server has already collected an answer for, and
 * the person would rate the same film twice into a Ledger that has no idea. Everything the
 * spec calls state — verdicts, seen flags, prompts, sessions — is a network fact only.
 *
 * SvelteKit registers this file automatically because of where it sits (`src/service-worker.js`).
 */

import { build, files, version } from '$service-worker';

// One cache per build. `version` changes on every build, so activate can delete every other
// cache and there is no such thing as a stale asset surviving a deploy.
const CACHE = `spielplan-shell-${version}`;

// The app shell: hashed build assets, the static files (manifest, icons, fonts — the manifest
// and icons are what make the thing installable at all), and `/`, which the backend answers
// with index.html. Nothing else. `addAll` is all-or-nothing on purpose: a half-cached shell
// that boots into a missing chunk is worse than no cache.
const SHELL = [...build, ...files, '/'];

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
  // The non-job, enforced here rather than trusted to the rest of the file.
  if (url.pathname.startsWith('/api/')) return;

  // Build assets are content-hashed: cache-first is safe by construction, because a changed
  // file is a changed URL.
  if (build.includes(url.pathname) || files.includes(url.pathname)) {
    event.respondWith(caches.match(request).then((hit) => hit ?? fetch(request)));
    return;
  }

  // Navigations: network first, shell second. Network-first because index.html is *not*
  // content-hashed — serving it from cache first would pin a phone to the previous release
  // until the cache happened to be evicted.
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/').then((hit) => hit ?? Response.error())));
  }
});

/**
 * §7.3: "Push notification if the user isn't in the app — best-effort".
 *
 * The payload is whatever the sender (M4) puts in it; this stays a dumb renderer, because a
 * service worker that decides what a notification says is a second copy of the copy rules
 * (§6.2's conflict phrasing, §7.3's prompt wording) living where nobody will look for it.
 */
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
      // A tag means a second prompt for the same title replaces the first instead of stacking
      // — the same "one at a time" rule the in-app finish prompt follows.
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
      // Focus the tab the household already has open rather than opening a fourth one; a
      // phone that has the app on the home screen has exactly one, and it is the right one.
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
