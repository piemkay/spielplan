/**
 * Install and Web Push, in the browser. Spec v2.1 §6 preamble, §4.2, §7.3.
 *
 * §6's preamble is the whole design brief for this file:
 *
 *   "on iPhone, Web Push works only for a PWA added to the home screen (iOS 16.4+), and the
 *    permission request must run inside a user gesture; iOS has no programmatic install
 *    prompt, so member first-run onboarding *guides* Share → Add to Home Screen, detects
 *    standalone mode, and nags until push is granted."
 *
 * So there are two mechanisms, not one with a fallback: Chrome/Edge/Android hand us a real
 * `beforeinstallprompt` event we can spend on a button, and iOS hands us nothing at all. This
 * module reports which world it is in and refuses to pretend — `platform()` returns
 * `ios-safari` where the only honest UI is a set of instructions.
 *
 * Everything here is the browser side of `backend/spielplan/api/push.py`; the endpoint and the
 * auth key go straight from the browser to that route and are never held anywhere else.
 */

import { api, get, post } from '$lib/api.js';

const ua = () => (typeof navigator === 'undefined' ? '' : navigator.userAgent);

/**
 * Which install story applies here.
 *
 *   `ios-safari`   Share → Add to Home Screen, and nothing else exists.
 *   `ios-other`    Chrome/Firefox/Edge on iOS: they cannot add to the home screen at all,
 *                  so the only useful thing to say is "open this in Safari".
 *   `installable`  a `beforeinstallprompt` was captured — a real button.
 *   `browser`      everything else: desktop Safari, Firefox, or a Chrome that has already
 *                  installed the app or does not consider it installable yet.
 *
 * iPadOS 13+ reports itself as a Mac, which is why `maxTouchPoints` is in the test: without
 * it an iPad is told to click an Install button that will never appear.
 */
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

// --- the install prompt, where a browser offers one ------------------------------------------
//
// Chrome and Edge fire `beforeinstallprompt` once per page load and only if they consider the
// app installable; the event is the *only* way to open the real install dialog, and it must be
// kept, because calling `prompt()` outside a user gesture does nothing.
//
// The listener is registered when this module loads rather than when a component mounts, so an
// event that arrives while the member is still on Home is not lost on the way to the account
// screen. It is still best-effort by nature — a browser that fired it before this module was
// ever imported has fired it for good — and the screen renders the honest "no install prompt
// here" branch in that case rather than a button that would do nothing.

/** @type {any} */
let captured = null;
/** @type {Set<(event: any) => void>} */
const watchers = new Set();

if (typeof window !== 'undefined') {
  window.addEventListener('beforeinstallprompt', (event) => {
    // Stops Chrome's own mini-infobar, so the guided act §6 asks for is the only thing on
    // screen instead of racing a browser chrome banner.
    event.preventDefault();
    captured = event;
    for (const watcher of watchers) watcher(event);
  });
}

/** The captured event, or null where this browser has not offered one. */
export const installPrompt = () => captured;

/** @param {(event: any) => void} fn @returns {() => void} unsubscribe */
export function watchInstallPrompt(fn) {
  watchers.add(fn);
  return () => watchers.delete(fn);
}

/**
 * Open the browser's install dialog. Single-use: the event cannot be prompted twice, so it is
 * dropped as it is spent and the button goes away with it.
 */
export async function showInstallPrompt() {
  const event = captured;
  if (!event) return { outcome: 'unavailable' };
  captured = null;
  await event.prompt();
  return (await Promise.resolve(event.userChoice).catch(() => null)) ?? { outcome: 'dismissed' };
}

/** Already installed: launched from the home screen or as a desktop window. */
export function isStandalone() {
  if (typeof window === 'undefined') return false;
  if (window.matchMedia?.('(display-mode: standalone)').matches === true) return true;
  // iOS Safari's own, older flag: it does not implement `display-mode: standalone`, so this is
  // the only signal on the one platform where being installed actually decides whether push
  // works at all. Cast because it is not in lib.dom's `Navigator`.
  return /** @type {any} */ (window.navigator).standalone === true;
}

export const pushSupported = () =>
  typeof window !== 'undefined' &&
  'serviceWorker' in navigator &&
  'PushManager' in window &&
  'Notification' in window;

/**
 * 'default' | 'granted' | 'denied', or 'unsupported' where there is no Notification API.
 *
 * The Permissions API first and `Notification.permission` only as the fallback, because the
 * two do not always agree: a headless Chromium whose notification permission has been granted
 * out of band still reports `Notification.permission === 'denied'`, and a screen that believed
 * that would tell the member their browser is blocking notifications it is perfectly willing
 * to deliver. Safari before 16 has no `notifications` query, which is what the fallback is for.
 */
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

/** What the account screen renders from: the step, the key, and this member's devices. */
export const readState = () => get('/push/state');

export const completeOnboarding = () => post('/setup/onboarding/complete', {});

/**
 * The VAPID application server key, base64url text → the bytes `subscribe()` wants.
 *
 * base64url, not base64: two characters of the alphabet differ and the padding is absent, the
 * same trap `passkeys.js` documents. Getting it wrong produces a subscription the push service
 * will later refuse to deliver to, which is a failure nobody sees for weeks.
 */
export function applicationServerKey(base64url) {
  const b64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, '='));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/** A name the member will recognise in the device list a year from now. */
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

/**
 * This browser's own endpoint, or null. §6's preamble, §4.2.
 *
 * The one question the account screen could not ask. `GET /api/push/state` answers "which devices
 * does this member have", and `api/push.py`'s own docstring is explicit that it is "this member's
 * devices. Never the household's" — so a screen that derived "notifications are on for THIS
 * device" from that list told a member's second phone that it was already registered, listed the
 * first phone as if it were itself, and hid the only control that can run
 * `Notification.requestPermission()` inside a gesture. Only the browser knows whether the browser
 * holds a subscription, so this is where that fact has to come from. [M4.11 finding 21]
 */
export async function localEndpoint() {
  if (!pushSupported()) return null;
  const { subscription } = await currentSubscription();
  return subscription?.endpoint ?? null;
}

/**
 * Was this subscription minted against the key the server is signing with now?
 *
 * A `PushSubscription` is bound for life to the application server key it was created against, so
 * a pair regenerated on this server — §3.1's first boot after a restore whose `.env` did not come
 * back, or `spielplan-secrets reset` — leaves every phone holding a subscription the push service
 * will refuse to deliver to. Nothing noticed: `enablePush` took whatever `getSubscription()`
 * returned and `syncSubscription` re-posted the same endpoint on every account-page open, so the
 * device list read healthy while no notification could arrive. `send.py` cannot repair it either,
 * deliberately — a key mismatch is a 403 and it does not prune on 403, because a transient
 * rejection must not delete a live phone.
 *
 * **Unknown is treated as a match, never as a mismatch.** `options.applicationServerKey` is absent
 * on older WebKit and on any test double that does not model it, and acting on that absence would
 * unsubscribe a working phone on the strength of a field the browser never implemented. The same
 * goes for a server with no pair at all (`vapidKey` null): there is no key to disagree with.
 */
export function keyMatches(subscription, vapidKey) {
  const held = subscription?.options?.applicationServerKey;
  if (!held || !vapidKey) return true;
  const want = applicationServerKey(vapidKey);
  const have = new Uint8Array(held);
  return have.length === want.length && want.every((byte, i) => byte === have[i]);
}

/**
 * Ask for permission and register this device. **Must be called from a click**: §6's preamble
 * notes the permission request has to run inside a user gesture, and on iOS a request outside
 * one is not deferred — it is refused, permanently, for that origin.
 *
 * Returns `{ permission, subscribed }` rather than throwing on a refusal: "no" is an answer,
 * not an error, and §3.1's fifth step completes either way. On a success it also returns this
 * device's own `endpoint` and the server's `device` handle for it, so the caller can say which row
 * in the member's list is the phone in the member's hand without hashing anything: §2 puts the app
 * behind one plain-HTTP port, where `crypto.subtle` does not exist.
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
    // A subscription bound to a retired key cannot be repaired, only replaced — `subscribe()`
    // with a different `applicationServerKey` is an InvalidStateError while the old one is live,
    // so the old one goes first. This is the act the member just asked for, so it is also the
    // only moment at which silently dropping a subscription is honest. [M4.11 finding 21]
    await held.unsubscribe();
    held = null;
  }
  const live =
    held ??
    (await registration.pushManager.subscribe({
      // Required by every browser that implements Push: a push may not be silent. It is also
      // the honest description of what this is for — §7.3's prompt is a visible question.
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
 * Re-post the subscription this browser already holds.
 *
 * A phone re-registers its service worker on every app update and the push service may hand
 * back the same endpoint or a fresh one; either way the server row must match what the browser
 * actually has. The route upserts on the endpoint (§4.2's UNIQUE), so calling this on every
 * open is cheap and cannot fan out into duplicate rows — which is exactly why it upserts.
 *
 * It no longer re-posts a subscription minted under a key this server has replaced. That re-post
 * was the thing that made the fault invisible: the row kept coming back fresh on every account-page
 * open, so the screen said "on" for a device that could not be reached. `{ stale: true }` is the
 * answer instead, and the screen reads it as off-with-a-reason — the member's own tap is what
 * replaces the subscription (`enablePush`), because only a tap is the gesture §6's preamble
 * requires. [M4.11 finding 21]
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

/**
 * Turn notifications off for this device: the server row first, then the browser's own
 * subscription. In that order — a browser subscription dropped while the row survives is a
 * target the sender will keep writing to and the push service will keep rejecting.
 *
 * `{ removed: false, subscriptions: null }` rather than a bare `null` when this browser holds
 * nothing to delete. The bare null was indistinguishable from a successful delete that returned no
 * list, and the caller resolved the ambiguity the worst way available — `state?.subscriptions ?? []`
 * blanked the device list, so the screen claimed notifications were off for the household while the
 * member's other phone stayed subscribed and kept receiving them. Two facts, said separately:
 * whether a row was deleted, and what the member's remaining devices are. [M4.11 finding 21]
 */
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
