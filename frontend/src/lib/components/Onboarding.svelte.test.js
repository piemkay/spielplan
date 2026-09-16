/**
 * @vitest-environment jsdom
 *
 * Whose device is "this device". Spec v2.1 §6 preamble, §3.1, §4.2; M4.11 finding 21.
 *
 * The screen asked the server a question only the browser can answer. `GET /api/push/state` returns
 * the member's devices — `api/push.py`: "this member's devices. Never the household's" — and this
 * section derived "notifications are on for this device" from `devices.length > 0`. So the second
 * phone any member picked up read the FIRST phone's row as itself: the heading said This device, the
 * copy said notifications were on, the list showed a device the member was not holding, and the
 * enable button — the only thing that runs `Notification.requestPermission()` inside the gesture §6
 * requires — sat in an unreachable `else`. That member's second device could never be registered at
 * all, which is half of M1's "both users" promise.
 *
 * MOUNTED, AND NOT ONLY IN PLAYWRIGHT. The browser state that matters is "this context holds no
 * PushSubscription while the account already has a row", and a second one, "the subscription this
 * browser holds was minted under a VAPID key the server has since replaced" — which no test browser
 * can be put into, because there is no push service behind one to mint either. The end-to-end story
 * stays in `e2e/specs/12-onboarding.spec.js`, where two real contexts make the claim a human would:
 * that is what the coverage row `push-a-second-device-registers-independently` names.
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

/**
 * jsdom implements neither Push nor Notification, so `pushSupported()` is false until these exist.
 * Defined ON the real window rather than over it: `vi.stubGlobal('window', …)` would take the
 * document Svelte mounts into with it.
 */
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

/**
 * An iPhone in a Safari tab. `platform()` reads the agent string and nothing else, and jsdom's
 * default one is a desktop Chrome, so both iOS branches are unreachable without this. Defined as
 * an own property for the same reason `serviceWorker` is: `Reflect.deleteProperty` in `afterEach`
 * hands the prototype's getter back, where `vi.stubGlobal('navigator', …)` would replace the
 * object Svelte mounts against.
 */
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
  // `Reflect.deleteProperty` and not `delete`: jsdom's `navigator.serviceWorker` is declared
  // read-only in lib.dom, and `npm run check` counts that as an error on a baseline this milestone
  // measures against.
  Reflect.deleteProperty(navigator, 'serviceWorker');
  Reflect.deleteProperty(navigator, 'userAgent');
});

/** The account screen's one read, with whatever devices this member's account holds. */
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
    // The whole of finding 21: this browser holds no subscription, the account holds one.
    inBrowser({ subscription: null });
    pushState([OTHER]);
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('off');
      expect(target.querySelector(ENABLE)).not.toBeNull();
      // The off switch was the only control on screen, and pressing it deleted nothing while
      // blanking the list — so the screen claimed off for a household that was still subscribed.
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
    // The re-post on open answers with this device's handle and the member's full list, which is
    // the only way this screen can know which row it is: §2's plain-HTTP origin has no
    // `crypto.subtle`, so the browser cannot hash its own endpoint.
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
    // A restore whose `.env` did not come back, or `spielplan-secrets reset`: the pair is new and
    // every phone holds a subscription the push service will refuse. The re-post is what hid it.
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
    // The negative control: `pushSupported()` is false here, and the section must not fall into
    // the off branch and offer an enable control that throws.
    pushState([]);
    const app = await open();
    try {
      expect(target.querySelector(SECTION).getAttribute('data-push-state')).toBe('unsupported');
      expect(target.querySelector(ENABLE)).toBeNull();
      // The negative control for the iOS branch below: off iOS, "no Web Push support" IS the
      // cause, and the sentence that names it has to survive the branch added beside it.
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
    // The one state `disablePush` reports and this screen used only to annotate: the member
    // cleared site data in another tab, or a second tab unsubscribed, so `getSubscription()` is
    // null by the time the tap lands. `localSub` is the single fact that answer falsifies — "only
    // the browser knows what the browser holds" (M4.11 finding 21) — and leaving it set kept
    // `pushState` on 'on', so the card said "Notifications are on for this device" directly above
    // "there was nothing to turn off here", still offered the off switch, and still hid the enable
    // control in the unreachable arm. That last clause is finding 21's own defect, reintroduced by
    // the branch written to repair it: §6's preamble makes the tap the only gesture that can
    // re-register the device, and it was not on screen.
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
      // The account's rows are the server's answer and this tap deleted none of them: §4.2 keys
      // the table on the member, and the DELETE that would have shortened the list never ran.
      const scopes = [...target.querySelectorAll(DEVICE)].map((li) => li.getAttribute('data-device'));
      expect(scopes).toEqual(['unknown', 'unknown']);
    } finally {
      unmount(app);
    }
  });
});

/**
 * The iOS install journey, in the two sentences that were wrong about it. Spec v2.1 §6 preamble,
 * §3.2; M4.15 finding 22 [fe-14-ios-install-journey-second-login-and-copy].
 *
 * Both sentences were true of a world this app is not in. "Open Spielplan from the new icon, then
 * come back here for notifications" describes a journey the session forbids: §3.2's session is an
 * HttpOnly SameSite=Lax cookie and the home-screen app holds its own jar, so the icon opens on a
 * 401 and the shell lands the member on /login. And "This browser has no Web Push support" is
 * true in a Safari tab while naming the wrong cause — §6's preamble states the real rule, "on
 * iPhone, Web Push works only for a PWA added to the home screen (iOS 16.4+)", which is a place
 * and not a capability.
 *
 * MOUNTED, because the branch is chosen by the agent string and by nothing a stack can be put
 * into: `playwright.config.js`'s phone project is WebKit on an iPhone 13 viewport, so it reaches
 * `ios-safari` too, but it cannot be made to hold the one fact the copy now turns on — a second
 * cookie jar behind a home-screen icon, which is decision 281's owed device check. What is
 * assertable here is what the screen SAYS, and that is the whole of this repair.
 */
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
      // The claim the old copy made, and the one thing the cookie jar makes impossible.
      expect(target.textContent).not.toContain('come back here for notifications');
      // The reason, and the card `/account` puts ABOVE this one on the `?welcome=1` visit and
      // below it on every other. Which is why the sentence names no direction: this component
      // does not know where its host places it, and "below" was false on precisely the visit
      // finding 22 inverted the order for. Both facts under this copy — that the icon opens
      // with its own cookie jar, and whether a passkey registered in this tab answers inside
      // the home-screen app — are owed in `docs/TESTING.md` as unsigned device checks
      // (decision 281); this asserts what renders, and claims nothing about what an iPhone does.
      expect(target.textContent).toContain('the home-screen app keeps its own sign-in');
      expect(target.textContent).toContain('Adding a passkey on this page');
      expect(target.textContent).not.toContain('passkey below');
    } finally {
      unmount(app);
    }
  });

  it('says notifications come from the icon, rather than blaming the browser', async () => {
    // No `inBrowser()`: an iOS Safari tab has no PushManager, which is exactly the state that
    // used to render "This browser has no Web Push support" to somebody whose browser has it.
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
