/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, get, post } from '$lib/api.js';
import Onboarding from './Onboarding.svelte';

const SECTION = '[data-testid="onboarding"]';
const ENABLE = '[data-testid="onboarding-push-enable"]';
const DISABLE = '[data-testid="onboarding-push-disable"]';
const DEVICE = '[data-testid="onboarding-device"]';
const NOTE = '[data-testid="onboarding-push-note"]';
const STEPS = '[data-testid="onboarding-ios-steps"]';
const PUSH_STATE = '[data-testid="onboarding-push-state"]';

// base64url, as the server hands the public half over.
const KEY = 'BFVpcUFyb2xs';
const RETIRED = 'BFJlc3RvcmVk';

/** The other phone: already in `push_subscription`, identified by its handle and nothing else. */
const OTHER = { id: 1, device_label: 'iPhone', device: 'aaaa11112222', last_seen_ok: null };
/** This browser's row, as the server reports it back from a subscribe. */
const MINE = { id: 2, device_label: 'This browser', device: 'bbbb33334444', last_seen_ok: null };

function subscriptionDouble(endpoint, keyText) {
  return {
    endpoint,
    options: keyText ? { applicationServerKey: toBuffer(keyText) } : {},
    toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }),
    unsubscribe: vi.fn(async () => true)
  };
}

function toBuffer(base64url) {
  const raw = atob(base64url.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0)).buffer;
}

let target;

// Defined on the real window: stubbing `window` would take Svelte's mount document with it.
function inBrowser({ subscription = null } = {}) {
  const registration = {
    pushManager: {
      getSubscription: vi.fn(async () => subscription),
      subscribe: vi.fn(async () => subscriptionDouble('https://push.example.test/fresh', null))
    }
  };
  vi.stubGlobal('PushManager', class {});
  vi.stubGlobal('Notification', { permission: 'granted', requestPermission: async () => 'granted' });
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: { ready: Promise.resolve(registration), addEventListener() {} }
  });
  return registration;
}

// `platform()` reads only the agent string; an own property, so `Reflect.deleteProperty` restores it.
function onIphone() {
  Object.defineProperty(navigator, 'userAgent', {
    configurable: true,
    value:
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 ' +
      '(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
  });
}

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  vi.mocked(api).mockReset();
});

afterEach(() => {
  target.remove();
  vi.unstubAllGlobals();
  // `Reflect.deleteProperty`, not `delete`: lib.dom declares `serviceWorker` read-only.
  Reflect.deleteProperty(navigator, 'serviceWorker');
  Reflect.deleteProperty(navigator, 'userAgent');
});

const pushState = (subscriptions) =>
  vi.mocked(get).mockResolvedValue({
    onboarding_complete: true,
    vapid_public_key: KEY,
    subscriptions
  });

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open() {
  const app = mount(Onboarding, { target });
  await settle();
  return app;
}

describe('a second device', () => {
  it('reads off, offers the enable control, and still shows the other device', async () => {
    // This browser holds no subscription while the account holds one.
    inBrowser({ subscription: null });
    pushState([OTHER]);
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('off');
      expect(target.querySelector(ENABLE)).not.toBeNull();
      expect(target.querySelector(DISABLE)).toBeNull();
      const rows = target.querySelectorAll(DEVICE);
      expect(rows).toHaveLength(1);
      expect(rows[0].getAttribute('data-device')).toBe('unknown');
      expect(target.textContent).toContain('None of these is this browser');
    } finally {
      unmount(app);
    }
  });
});

describe('a device that is registered', () => {
  it('reads on and marks its own row, not the other one', async () => {
    inBrowser({ subscription: subscriptionDouble('https://push.example.test/mine', KEY) });
    pushState([OTHER]);
    // The re-post's device handle is the only way to mark this row: no `crypto.subtle` over plain HTTP.
    vi.mocked(post).mockResolvedValue({
      ok: true,
      id: MINE.id,
      device: MINE.device,
      subscriptions: [OTHER, MINE]
    });
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('on');
      expect(target.querySelector(DISABLE)).not.toBeNull();
      const scopes = [...target.querySelectorAll(DEVICE)].map((li) => li.getAttribute('data-device'));
      expect(scopes).toEqual(['other', 'this']);
      expect(target.querySelector('[data-device="this"]').textContent).toContain('this device');
    } finally {
      unmount(app);
    }
  });
});

describe('a subscription minted under a key the server has replaced', () => {
  it('reads off with a reason, and is not re-posted as if it were healthy', async () => {
    // A key reset leaves every phone's subscription undeliverable; re-posting it hid that.
    inBrowser({ subscription: subscriptionDouble('https://push.example.test/stale', RETIRED) });
    pushState([OTHER]);
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('off');
      expect(post).not.toHaveBeenCalled();
      expect(target.querySelector(NOTE).getAttribute('data-note')).toBe('stale');
      expect(target.querySelector(ENABLE)).not.toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('a browser with no Web Push at all', () => {
  it('says so instead of offering a button that cannot work', async () => {
    pushState([]);
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('unsupported');
      expect(target.querySelector(ENABLE)).toBeNull();
      // Off iOS, "no Web Push support" is the true cause.
      expect(target.querySelector(PUSH_STATE).textContent).toContain(
        'This browser has no Web Push support'
      );
    } finally {
      unmount(app);
    }
  });
});

describe('an off switch that finds nothing to switch off', () => {
  it('reads off afterwards, keeps the other rows, and offers the enable control', async () => {
    // Another tab unsubscribed before the tap, so the off switch finds nothing local.
    const registration = inBrowser({
      subscription: subscriptionDouble('https://push.example.test/mine', KEY)
    });
    pushState([OTHER]);
    vi.mocked(post).mockResolvedValue({
      ok: true,
      id: MINE.id,
      device: MINE.device,
      subscriptions: [OTHER, MINE]
    });
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('on');
      // Gone from under the screen between the open and the tap.
      registration.pushManager.getSubscription.mockResolvedValue(null);
      target.querySelector(DISABLE).click();
      await settle();

      expect(api).not.toHaveBeenCalled();
      expect(target.querySelector(NOTE).getAttribute('data-note')).toBe('nothing-local');
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('off');
      expect(target.querySelector(ENABLE)).not.toBeNull();
      expect(target.querySelector(DISABLE)).toBeNull();
      // The tap deleted none of the account's rows.
      const scopes = [...target.querySelectorAll(DEVICE)].map((li) => li.getAttribute('data-device'));
      expect(scopes).toEqual(['unknown', 'unknown']);
    } finally {
      unmount(app);
    }
  });
});

describe('an iPhone in a Safari tab', () => {
  it('says the icon keeps its own sign-in, instead of promising a way back', async () => {
    onIphone();
    pushState([]);
    const app = await open();
    try {
      const steps = target.querySelector(STEPS);
      expect(steps).not.toBeNull();
      const third = steps.querySelectorAll('li')[2].textContent;
      expect(third).toContain('Open Spielplan from the new icon and sign in there once');
      expect(target.textContent).not.toContain('come back here for notifications');
      // The sentence names no direction: the host moves the Passkeys card.
      expect(target.textContent).toContain('the home-screen app keeps its own sign-in');
      expect(target.textContent).toContain('Adding a passkey on this page');
      expect(target.textContent).not.toContain('passkey below');
    } finally {
      unmount(app);
    }
  });

  it('says notifications come from the icon, rather than blaming the browser', async () => {
    // No `inBrowser()`: an iOS Safari tab has no PushManager.
    onIphone();
    pushState([]);
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-platform')).toBe('ios-safari');
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('unsupported');
      const said = target.querySelector(PUSH_STATE).textContent;
      expect(said).toContain('notifications come from the home-screen app');
      expect(said).toContain('add the icon in step 1');
      expect(said).not.toContain('This browser has no Web Push support');
    } finally {
      unmount(app);
    }
  });
});
