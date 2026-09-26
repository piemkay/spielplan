// Two install mechanisms, not one with a fallback: a captured `beforeinstallprompt` where the
// browser fires one, and only instructions on iOS (§6 preamble).

import { api, get, post } from '$lib/api.js';

const ua = () => (typeof navigator === 'undefined' ? '' : navigator.userAgent);

// `ios-safari` | `ios-other` (cannot add to the home screen) | `installable` | `browser`.
// iPadOS reports itself as a Mac, hence `maxTouchPoints`.
export function platform({ installPrompt = false } = {}) {
  const agent = ua();
  const ios =
    /iPad|iPhone|iPod/.test(agent) ||
    (typeof navigator !== 'undefined' &&
      navigator.platform === 'MacIntel' &&
      navigator.maxTouchPoints > 1);
  if (ios) {
    // On iOS every browser is WebKit, but only Safari can add a page to the home screen.
    return /CriOS|FxiOS|EdgiOS|OPiOS|GSA/.test(agent) ? 'ios-other' : 'ios-safari';
  }
  return installPrompt ? 'installable' : 'browser';
}

// Registered at module load, so an event fired before the account screen mounts is kept. The
// event is the only way to open the install dialog.

/** @type {any} */
let captured = null;
/** @type {Set<(event: any) => void>} */
const watchers = new Set();

if (typeof window !== 'undefined') {
  window.addEventListener('beforeinstallprompt', (event) => {
    // Suppress Chrome's own mini-infobar.
    event.preventDefault();
    captured = event;
    for (const watcher of watchers) watcher(event);
  });
}

export const installPrompt = () => captured;

/** @param {(event: any) => void} fn @returns {() => void} unsubscribe */
export function watchInstallPrompt(fn) {
  watchers.add(fn);
  return () => watchers.delete(fn);
}

/** Single-use: the event cannot be prompted twice. */
export async function showInstallPrompt() {
  const event = captured;
  if (!event) return { outcome: 'unavailable' };
  captured = null;
  await event.prompt();
  return (await Promise.resolve(event.userChoice).catch(() => null)) ?? { outcome: 'dismissed' };
}

export function isStandalone() {
  if (typeof window === 'undefined') return false;
  if (window.matchMedia?.('(display-mode: standalone)').matches === true) return true;
  // iOS Safari's own flag: it lacks `display-mode: standalone`, and there install decides push.
  return /** @type {any} */ (window.navigator).standalone === true;
}

export const pushSupported = () =>
  typeof window !== 'undefined' &&
  'serviceWorker' in navigator &&
  'PushManager' in window &&
  'Notification' in window;

// The Permissions API first: a headless Chromium granted out of band still reports
// `Notification.permission` as 'denied'. Safari before 16 needs the fallback.
export async function permissionState() {
  if (!pushSupported()) return 'unsupported';
  try {
    const status = await navigator.permissions?.query({ name: 'notifications' });
    // The Permissions API says 'prompt' where the Notification API says 'default'.
    if (status?.state) return status.state === 'prompt' ? 'default' : status.state;
  } catch {
    // No `notifications` in this browser's permission registry — fall through.
  }
  return Notification.permission;
}

export const readState = () => get('/push/state');

export const completeOnboarding = () => post('/setup/onboarding/complete', {});

// base64url, not base64: the wrong alphabet yields a subscription the push service later refuses.
export function applicationServerKey(base64url) {
  const b64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, '='));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export function deviceLabel() {
  const agent = ua();
  if (/iPad/.test(agent)) return 'iPad';
  if (/iPhone/.test(agent)) return 'iPhone';
  if (/Android/.test(agent)) return 'Android phone';
  if (isStandalone()) return 'Installed app';
  return 'This browser';
}

async function currentSubscription() {
  const registration = await navigator.serviceWorker.ready;
  return { registration, subscription: await registration.pushManager.getSubscription() };
}

// Only the browser knows whether it holds a subscription; `/push/state` lists the member's devices.
export async function localEndpoint() {
  if (!pushSupported()) return null;
  const { subscription } = await currentSubscription();
  return subscription?.endpoint ?? null;
}

// A subscription is bound for life to its application server key. Unknown (older WebKit, or no
// server key) counts as a match, so a working phone is never unsubscribed on a guess.
export function keyMatches(subscription, vapidKey) {
  const held = subscription?.options?.applicationServerKey;
  if (!held || !vapidKey) return true;
  const want = applicationServerKey(vapidKey);
  const have = new Uint8Array(held);
  return have.length === want.length && want.every((byte, i) => byte === have[i]);
}

/**
 * Must be called from a click: iOS refuses a permission request outside a user gesture, for good.
 * A refusal is an answer, not an error.
 *
 * @param {{vapidKey?: string|null}} [options]
 */
export async function enablePush({ vapidKey = null } = {}) {
  if (!pushSupported()) return { permission: 'unsupported', subscribed: false };

  const answer = await Notification.requestPermission();
  if (answer !== 'granted') return { permission: answer, subscribed: false };

  const { registration, subscription } = await currentSubscription();
  let held = subscription;
  if (held && !keyMatches(held, vapidKey)) {
    // Subscribing under a new key is an InvalidStateError while the old one lives, so drop it first.
    await held.unsubscribe();
    held = null;
  }
  const live =
    held ??
    (await registration.pushManager.subscribe({
      // Required by every Push implementation: a push may not be silent.
      userVisibleOnly: true,
      ...(vapidKey ? { applicationServerKey: applicationServerKey(vapidKey) } : {})
    }));

  const state = await post('/push/subscribe', {
    ...live.toJSON(),
    device_label: deviceLabel()
  });
  return {
    permission: 'granted',
    subscribed: true,
    endpoint: live.endpoint,
    device: state.device,
    subscriptions: state.subscriptions
  };
}

/**
 * Re-post the held subscription; the route upserts on the endpoint. A subscription under a
 * replaced key is reported `{ stale: true }` instead, since only a tap may replace it.
 *
 * @param {{vapidKey?: string|null}} [options]
 */
export async function syncSubscription({ vapidKey = null } = {}) {
  if ((await permissionState()) !== 'granted') return null;
  const { subscription } = await currentSubscription();
  if (!subscription) return null;
  if (!keyMatches(subscription, vapidKey)) return { stale: true, subscriptions: null };
  return post('/push/subscribe', { ...subscription.toJSON(), device_label: deviceLabel() });
}

// The server row first: a dropped browser subscription with a surviving row keeps being written to.
export async function disablePush() {
  if (!pushSupported()) return { removed: false, subscriptions: null };
  const { subscription } = await currentSubscription();
  if (!subscription) return { removed: false, subscriptions: null };
  const state = await api('/push/subscription', {
    method: 'DELETE',
    body: { endpoint: subscription.endpoint }
  });
  await subscription.unsubscribe();
  return { removed: true, subscriptions: state?.subscriptions ?? null };
}
