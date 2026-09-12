/**
 * @vitest-environment jsdom
 *
 * Which card the welcome visit shows first. Spec v2.1 §3.1, §3.2, §6 preamble; M4.15 finding 22
 * [fe-14-ios-install-journey-second-login-and-copy].
 *
 * §3.1 hands a new member from the forced password change to this page as `/account?welcome=1`,
 * "and passkey registration is prompted afterwards". The page did prompt — with a banner at the
 * top — and then put the control that answers the prompt BELOW the onboarding card, whose first
 * instruction is to leave for the home-screen icon. §3.2's session is an HttpOnly cookie and the
 * home-screen app keeps its own jar, so following that instruction costs a second sign-in; the
 * passkey is the one thing that shortens it, and it was being offered after the exit.
 *
 * So the order inverts on that one visit and on no other: `welcome` already means exactly "the
 * `?welcome=1` hand-off, no credential registered yet, WebAuthn present", and every other visit
 * keeps §3.1's own order with the still-owed fifth setup step first.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT, and named `account-page.test.js` and not
 * `+page.svelte.test.js`: SvelteKit reserves the `+` prefix inside `src/routes` and `vite build`
 * fails outright on any other `+`-named file, which is the note `rate-page.test.js` carries. The
 * order is a render fact and nothing else — `09-passkeys.spec.js` already signs in and registers
 * one, and reaching the welcome state from outside means minting a fresh account in a suite that
 * is stateful and filename-ordered.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The page reads `$page.url.searchParams` in a `$derived` that runs at init, so the store has to
// answer on subscribe. `vi.hoisted` because a `vi.mock` factory is hoisted above every other
// statement in the file and cannot close over an ordinary `const`.
const nav = vi.hoisted(() => ({ url: new URL('http://localhost/account') }));
vi.mock('$app/stores', () => ({
  page: {
    subscribe: (run) => {
      run({ url: nav.url });
      return () => {};
    }
  }
}));

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/passkeys.js', () => ({ registerPasskey: vi.fn(), supported: vi.fn(() => true) }));
vi.mock('$lib/session.svelte.js', () => ({ session: { user: { role: 'member' } }, bootstrap: vi.fn() }));
// The onboarding card owns four asynchronous browser facts, none of which is this file's subject;
// stubbed so it renders its settled state deterministically and the order is the only variable.
// Which platform the onboarding card renders for. Hoisted for the same reason `nav` above is:
// the mock factory is lifted over every other statement and cannot close over an ordinary const.
// The default is a desktop browser, which is what every ordering case wants; one case moves it,
// because the copy that names this page is written only for an iPhone in a Safari tab.
const where = vi.hoisted(() => ({ platform: 'browser' }));
vi.mock('$lib/push.js', () => ({
  completeOnboarding: vi.fn(),
  disablePush: vi.fn(),
  enablePush: vi.fn(),
  installPrompt: () => null,
  isStandalone: () => false,
  localEndpoint: async () => null,
  permissionState: async () => 'default',
  platform: () => where.platform,
  pushSupported: () => false,
  readState: async () => ({ onboarding_complete: true, vapid_public_key: null, subscriptions: [] }),
  showInstallPrompt: vi.fn(),
  syncSubscription: async () => null,
  watchInstallPrompt: () => () => {}
}));

import { get } from '$lib/api.js';
import AccountPage from './+page.svelte';

const TIERS = { tier_set: ['D', 'C', 'B', 'A'], min: 2, max: 12, warning: '' };

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  nav.url = new URL('http://localhost/account');
  where.platform = 'browser';
  vi.mocked(get).mockReset();
});

afterEach(() => target.remove());

/** The page's two reads, answered by path — `load()` fires both on mount. */
function answers({ credentials = [] } = {}) {
  vi.mocked(get).mockImplementation(async (path) =>
    path === '/auth/passkey/credentials' ? credentials : TIERS
  );
}

async function open() {
  const app = mount(AccountPage, { target });
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
  return app;
}

/** The cards in the order the member scrolls them, named by their own headings. */
const headings = () => [...target.querySelectorAll('h2')].map((h) => h.textContent.trim());

describe('the welcome hand-off from the forced password change', () => {
  it('puts the passkey card above the card that tells the member to leave', async () => {
    nav.url = new URL('http://localhost/account?welcome=1');
    answers({ credentials: [] });
    const app = await open();
    try {
      const order = headings();
      expect(order).toContain('Passkeys');
      expect(order).toContain('This device');
      expect(order.indexOf('Passkeys')).toBeLessThan(order.indexOf('This device'));
      // The banner §3.1's "prompted afterwards" puts at the top is still the thing being
      // answered, so the control it points at has to be above the fold with it.
      expect(target.querySelector('[data-passkey-prompt]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('never points the iPhone member at a card the inversion has put above the sentence', async () => {
    // The half of the inversion that lives in the OTHER file. `Onboarding.svelte` is written for
    // exactly this visit — an iPhone in a Safari tab, `?welcome=1`, no credential — and its copy
    // used to say "Adding a passkey below", while this page's `{#if welcome}` snippet renders
    // that card ABOVE it. So on the one journey finding 22 exists for, the sentence pointed the
    // member downward past Password, Switch PIN, Tier set and Jellyfin at something that had
    // been the first thing on their screen. The repair is directional-free copy rather than a
    // second re-ordering, because a component cannot know where its host mounts it; this is the
    // assertion that ties the two halves together, and it is here because this file owns the
    // order and `Onboarding.svelte.test.js` renders no host at all.
    nav.url = new URL('http://localhost/account?welcome=1');
    where.platform = 'ios-safari';
    answers({ credentials: [] });
    const app = await open();
    try {
      const order = headings();
      expect(order.indexOf('Passkeys')).toBeLessThan(order.indexOf('This device'));
      const steps = target.querySelector('[data-testid="onboarding-ios-steps"]');
      expect(steps, 'the iOS install steps did not render, so this proves nothing').not.toBeNull();
      expect(target.textContent).toContain('Adding a passkey on this page');
      expect(target.textContent).not.toMatch(/passkey (below|above)/);
    } finally {
      unmount(app);
    }
  });

  it('leaves the first-run step first on an ordinary visit', async () => {
    // The regression guard, not the fix: §3.1 makes onboarding the fifth setup step and it stays
    // first for everyone the hand-off is not about.
    answers({ credentials: [] });
    const app = await open();
    try {
      const order = headings();
      expect(order.indexOf('This device')).toBeLessThan(order.indexOf('Passkeys'));
      expect(target.querySelector('[data-passkey-prompt]')).toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('leaves it first for a welcome link opened on a device that already has a passkey', async () => {
    // `welcome` is three facts and not one query parameter: a member who has registered already
    // is not the person the inversion was written for, and re-ordering the page for them would
    // be a nag on an account §3.2 says is finished.
    nav.url = new URL('http://localhost/account?welcome=1');
    answers({
      credentials: [{ id: 1, label: 'iPhone', rp_id: 'localhost', sign_count: 3, usable: true }]
    });
    const app = await open();
    try {
      const order = headings();
      expect(order.indexOf('This device')).toBeLessThan(order.indexOf('Passkeys'));
    } finally {
      unmount(app);
    }
  });
});
