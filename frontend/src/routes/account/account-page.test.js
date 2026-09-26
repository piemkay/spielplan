/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// `vi.hoisted`: a `vi.mock` factory is hoisted above the file and cannot close over a plain const.
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
// Desktop by default; one case moves it, because that copy is written for an iPhone in Safari.
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
      expect(target.querySelector('[data-passkey-prompt]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('never points the iPhone member at a card the inversion has put above the sentence', async () => {
    // A component cannot know where its host mounts it, so Onboarding's copy names no direction.
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

describe('the account page in plain words', () => {
  const PASSKEY = { id: 1, label: 'iPhone', rp_id: 'spielplan.example', sign_count: 3, usable: true };

  it('names passkeys, the PIN and the Rank letters by what they do', async () => {
    answers({ credentials: [PASSKEY] });
    const app = await open();
    try {
      const text = target.textContent;
      for (const word of ['WebAuthn', 'Switch PIN', 'Tier set', 'switch PIN', 'authenticator']) {
        expect(text, word).not.toContain(word);
      }
      expect(headings()).toContain('PIN for switching profiles');
      expect(headings()).toContain('Rank letters');
      expect(target.querySelector('[data-testid="tier-set-current"]').textContent).toBe('D · C · B · A');
    } finally {
      unmount(app);
    }
  });

  it('folds the Rank letters editor and the data sources, and drops neither', async () => {
    answers({ credentials: [PASSKEY] });
    const app = await open();
    try {
      const edit = target.querySelector('[data-testid="tier-set-edit"]');
      expect(edit.tagName).toBe('DETAILS');
      expect(edit.open).toBe(false);
      expect(edit.contains(target.querySelector('[data-testid="tier-set-input"]'))).toBe(true);

      const technical = target.querySelector('[data-testid="account-technical"]');
      expect(technical.tagName).toBe('DETAILS');
      expect(technical.open).toBe(false);
      expect(technical.contains(target.querySelector('[data-testid="data-sources"]'))).toBe(true);
      expect(target.querySelector('.list li').textContent).toContain('spielplan.example');
    } finally {
      unmount(app);
    }
  });
});
