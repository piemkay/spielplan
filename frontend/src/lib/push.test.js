import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, post } from '$lib/api.js';
import { disablePush, enablePush, keyMatches, localEndpoint, syncSubscription } from './push.js';

// base64url, as `applicationServerKey` takes it — the server hands the public half over as text.
const KEY = 'BFVpcUFyb2xs';
const RETIRED = 'BFJlc3RvcmVk';

const HELD = 'https://push.example.test/device/held';
const FRESH = 'https://push.example.test/device/fresh';

function subscriptionDouble(endpoint, keyText) {
  return {
    endpoint,
    // A real subscription's `applicationServerKey` is an ArrayBuffer, not bytes or text.
    options: keyText ? { applicationServerKey: toBuffer(keyText) } : {},
    toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }),
    unsubscribe: vi.fn(async () => true)
  };
}

function toBuffer(base64url) {
  const raw = atob(base64url.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0)).buffer;
}

// `window.navigator` exists because `deviceLabel()` reads iOS Safari's `standalone`.
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
    // A new phone holds no subscription while `/api/push/state` lists the old one.
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
    // Older WebKit has no `options`, and unsubscribing a working phone over it would be worse.
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
    // Unsubscribing on every open would rotate a working phone's endpoint on each visit.
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
    // Re-posting a stale subscription made the screen say "on" for an undeliverable device.
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
    // A bare `null` here became `devices = []` in the caller.
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
    // The answer is the member's remaining devices.
    expect(result).toEqual({ removed: true, subscriptions: [{ id: 2 }] });
  });
});
