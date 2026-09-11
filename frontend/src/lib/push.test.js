/**
 * What this browser knows about its own subscription. Spec v2.1 §6 preamble, §4.2, §3.1;
 * M4.11 finding 21.
 *
 * Four faults in one screen, and all four came from asking the wrong party a browser-local
 * question. `GET /api/push/state` returns the member's devices — `api/push.py`'s own docstring
 * says "this member's devices. Never the household's" — and the account screen read that list as
 * "this device", so the second phone any member picked up was told it was already registered,
 * shown the first phone's row, and offered only the off switch. Underneath, nothing ever compared
 * the subscription the browser held against the key the server signs with, and the off switch
 * returned a bare `null` when there was nothing local to delete, which the caller turned into an
 * empty device list.
 *
 * ASSERTED HERE AND NOT ONLY IN PLAYWRIGHT, deliberately, and the split is a layer choice rather
 * than a limit of the map (`test_spec_coverage.py::_vitest_ids` registers vitest ids since M4.9).
 * These four are pure functions of what `pushManager` returns, and a test double can hand them a
 * subscription minted under a retired VAPID key — which is a state no test browser can be put into,
 * because there is no push service behind one. The story this file cannot tell is the one the
 * coverage row `push-a-second-device-registers-independently` names: only a real second browser
 * context, holding no subscription of its own while the account already has a device row, proves
 * the screen reads "off" and offers the enable control. That is `e2e/specs/12-onboarding.spec.js`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, post } from '$lib/api.js';
import { disablePush, enablePush, keyMatches, localEndpoint, syncSubscription } from './push.js';

// base64url, as `applicationServerKey` takes it — the server hands the public half over as text.
const KEY = 'BFVpcUFyb2xs';
const RETIRED = 'BFJlc3RvcmVk';

const HELD = 'https://push.example.test/device/held';
const FRESH = 'https://push.example.test/device/fresh';

/** A `PushSubscription` as far as this module reads one. */
function subscriptionDouble(endpoint, keyText) {
  return {
    endpoint,
    // `options.applicationServerKey` is an ArrayBuffer on a real subscription, which is the
    // shape the mismatch check has to survive — not a Uint8Array, and not base64 text.
    options: keyText ? { applicationServerKey: toBuffer(keyText) } : {},
    toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }),
    unsubscribe: vi.fn(async () => true)
  };
}

function toBuffer(base64url) {
  const raw = atob(base64url.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0)).buffer;
}

/**
 * The browser around the module: just enough of it that `pushSupported()` is true.
 *
 * `window.navigator` is present because `deviceLabel()` reaches for iOS Safari's `standalone`
 * flag, and a bare `{}` window makes that a TypeError rather than a label.
 */
function inBrowser({ subscription = null, permission = 'granted' } = {}) {
  const minted = [];
  const subscribe = vi.fn(async (options) => {
    minted.push(options);
    return subscriptionDouble(FRESH, null);
  });
  const registration = {
    pushManager: { getSubscription: vi.fn(async () => subscription), subscribe }
  };
  vi.stubGlobal('window', {
    PushManager: class {},
    Notification: class {},
    navigator: {},
    matchMedia: () => ({ matches: false })
  });
  vi.stubGlobal('navigator', {
    userAgent: 'vitest',
    serviceWorker: { ready: Promise.resolve(registration) }
  });
  vi.stubGlobal('Notification', {
    permission,
    requestPermission: vi.fn(async () => permission)
  });
  return { minted, subscribe };
}

beforeEach(() => {
  vi.mocked(post).mockReset();
  vi.mocked(api).mockReset();
  vi.mocked(post).mockResolvedValue({ ok: true, device: 'abc123', subscriptions: [{ id: 1 }] });
  vi.mocked(api).mockResolvedValue({ ok: true, subscriptions: [{ id: 2 }] });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('localEndpoint', () => {
  it('is null on a browser holding no subscription of its own', async () => {
    // The whole of the second-device fault: this is the state the member's new phone is in while
    // `/api/push/state` lists the old one, and it has to be distinguishable from "on".
    inBrowser();
    expect(await localEndpoint()).toBe(null);
  });

  it('is the endpoint this browser holds', async () => {
    inBrowser({ subscription: subscriptionDouble(HELD, KEY) });
    expect(await localEndpoint()).toBe(HELD);
  });

  it('is null where Web Push does not exist at all', async () => {
    // No stubs: `pushSupported()` is false, and the read must not throw on `navigator`.
    expect(await localEndpoint()).toBe(null);
  });
});

describe('keyMatches', () => {
  it('treats a key the browser does not report as a match, not a mismatch', () => {
    // Older WebKit has no `options`, and unsubscribing a working phone over a field the browser
    // never implemented would be a fault worse than the one this check exists for.
    expect(keyMatches(subscriptionDouble(HELD, null), KEY)).toBe(true);
    expect(keyMatches(subscriptionDouble(HELD, KEY), null)).toBe(true);
  });

  it('compares the bytes, not the objects', () => {
    expect(keyMatches(subscriptionDouble(HELD, KEY), KEY)).toBe(true);
    expect(keyMatches(subscriptionDouble(HELD, RETIRED), KEY)).toBe(false);
  });
});

describe('enablePush', () => {
  it('replaces a subscription minted under a key the server has since replaced', async () => {
    const stale = subscriptionDouble(HELD, RETIRED);
    const { minted } = inBrowser({ subscription: stale });

    const result = await enablePush({ vapidKey: KEY });

    expect(stale.unsubscribe).toHaveBeenCalledTimes(1);
    expect(minted).toHaveLength(1);
    expect(result.endpoint).toBe(FRESH);
    // The row the server stores is the new endpoint, so the dead one is not re-posted on top of it.
    expect(vi.mocked(post).mock.calls[0][1].endpoint).toBe(FRESH);
  });

  it('keeps a subscription that already matches, and re-posts it', async () => {
    // The negative control. A check that unsubscribed on every open would rotate a working
    // phone's endpoint on every account-page visit.
    const live = subscriptionDouble(HELD, KEY);
    const { minted } = inBrowser({ subscription: live });

    const result = await enablePush({ vapidKey: KEY });

    expect(live.unsubscribe).not.toHaveBeenCalled();
    expect(minted).toHaveLength(0);
    expect(result.endpoint).toBe(HELD);
    expect(result.device).toBe('abc123');
  });

  it('reports a refusal as an answer and registers nothing', async () => {
    const { minted } = inBrowser({ permission: 'denied' });
    expect(await enablePush({ vapidKey: KEY })).toEqual({
      permission: 'denied',
      subscribed: false
    });
    expect(minted).toHaveLength(0);
    expect(post).not.toHaveBeenCalled();
  });
});

describe('syncSubscription', () => {
  it('does not re-post a subscription bound to a retired key', async () => {
    // The re-post is what hid the fault: the row came back fresh on every open, so the screen
    // said "on" for a device the push service would refuse to deliver to.
    inBrowser({ subscription: subscriptionDouble(HELD, RETIRED) });
    expect(await syncSubscription({ vapidKey: KEY })).toEqual({
      stale: true,
      subscriptions: null
    });
    expect(post).not.toHaveBeenCalled();
  });

  it('re-posts the one the browser still holds', async () => {
    inBrowser({ subscription: subscriptionDouble(HELD, KEY) });
    const state = await syncSubscription({ vapidKey: KEY });
    expect(state.subscriptions).toEqual([{ id: 1 }]);
    expect(vi.mocked(post).mock.calls[0][1].endpoint).toBe(HELD);
  });

  it('posts nothing when there is nothing to post', async () => {
    inBrowser();
    expect(await syncSubscription({ vapidKey: KEY })).toBe(null);
    expect(post).not.toHaveBeenCalled();
  });
});

describe('disablePush', () => {
  it('says plainly that there was nothing local to delete', async () => {
    // A bare `null` here became `devices = []` in the caller: the screen claimed notifications
    // were off while the member's other phone stayed subscribed.
    inBrowser();
    expect(await disablePush()).toEqual({ removed: false, subscriptions: null });
    expect(api).not.toHaveBeenCalled();
  });

  it('deletes the row first, then drops the local subscription', async () => {
    const live = subscriptionDouble(HELD, KEY);
    inBrowser({ subscription: live });

    const result = await disablePush();

    expect(vi.mocked(api).mock.calls[0][1]).toMatchObject({
      method: 'DELETE',
      body: { endpoint: HELD }
    });
    expect(live.unsubscribe).toHaveBeenCalledTimes(1);
    // The server's answer is the member's REMAINING devices, which is what keeps another
    // device's row on screen after this one goes off.
    expect(result).toEqual({ removed: true, subscriptions: [{ id: 2 }] });
  });
});
