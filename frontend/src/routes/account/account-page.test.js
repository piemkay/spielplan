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

import { api, get, post } from '$lib/api.js';
import AccountPage from './+page.svelte';

const TIERS = { tier_set: ['D', 'C', 'B', 'A'], min: 2, max: 12, warning: '' };

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  nav.url = new URL('http://localhost/account');
  where.platform = 'browser';
  vi.mocked(get).mockReset();
  vi.mocked(api).mockReset();
  vi.mocked(post).mockReset();
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
  it('puts Sign-in above the group that tells the member to leave', async () => {
    nav.url = new URL('http://localhost/account?welcome=1');
    answers({ credentials: [] });
    const app = await open();
    try {
      const order = headings();
      expect(order).toContain('Sign-in');
      expect(order).toContain('This device');
      expect(order.indexOf('Sign-in')).toBeLessThan(order.indexOf('This device'));
      expect(target.querySelector('[data-passkey-prompt]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });

  it('never points the iPhone member at a group the inversion has put above the sentence', async () => {
    // A component cannot know where its host mounts it, so Onboarding's copy names no direction.
    nav.url = new URL('http://localhost/account?welcome=1');
    where.platform = 'ios-safari';
    answers({ credentials: [] });
    const app = await open();
    try {
      const order = headings();
      expect(order.indexOf('Sign-in')).toBeLessThan(order.indexOf('This device'));
      const steps = target.querySelector('[data-testid="onboarding-ios-steps"]');
      expect(steps, 'the iOS install steps did not render, so this proves nothing').not.toBeNull();
      expect(target.textContent).toContain('adding a passkey on this page');
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
      expect(order.indexOf('This device')).toBeLessThan(order.indexOf('Sign-in'));
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
      expect(order.indexOf('This device')).toBeLessThan(order.indexOf('Sign-in'));
    } finally {
      unmount(app);
    }
  });
});

describe('the account page in plain words', () => {
  const PASSKEY = {
    id: 1, label: 'iPhone', rp_id: 'spielplan.example', sign_count: 0, usable: true,
    created_at: '2026-09-01T10:00:00Z', last_used_at: '2026-09-28T20:00:00Z'
  };

  it('names passkeys, the PIN and the Rank letters by what they do, the letters best first', async () => {
    answers({ credentials: [PASSKEY] });
    const app = await open();
    try {
      const text = target.textContent;
      for (const word of ['WebAuthn', 'Switch PIN', 'Tier set', 'switch PIN', 'authenticator']) {
        expect(text, word).not.toContain(word);
      }
      // A synced passkey's sign count stays 0, so the row says when it was last used instead.
      const row = target.querySelector('[data-testid="passkey"]').textContent;
      expect(row).toContain('last used');
      expect(row).not.toMatch(/used \d+ time/);
      expect(target.querySelector('[data-testid="pin-card"] summary').textContent).toContain(
        'PIN for switching profiles'
      );
      expect(target.querySelector('[data-testid="tier-set-edit"] summary').textContent).toContain(
        'Rank letters'
      );
      expect(target.querySelector('[data-testid="tier-set-current"]').textContent).toBe('A B C D');
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
      expect(target.querySelector('[data-testid="passkey"]').textContent).toContain('spielplan.example');
    } finally {
      unmount(app);
    }
  });

  it('saves the letters as typed, best first, and says so under the editor', async () => {
    answers({ credentials: [PASSKEY] });
    vi.mocked(api).mockResolvedValue({
      tier_set: ['F', 'B', 'S'],
      k_changed: true,
      tier_edits_kept: 2
    });
    const app = await open();
    try {
      const input = target.querySelector('[data-testid="tier-set-input"]');
      input.value = 'S B F';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      [...target.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Save letters').click();
      for (let i = 0; i < 20; i++) await Promise.resolve();
      flushSync();

      expect(api).toHaveBeenCalledWith('/rank/tiers', {
        method: 'PUT',
        body: { tier_set: ['F', 'B', 'S'] }
      });
      expect(target.querySelector('[data-testid="tier-set-current"]').textContent).toBe('S B F');
      const said = target.querySelector('[data-testid="tier-set-edit"] [role="status"]');
      expect(said?.textContent).toContain('your 2 moves by hand are kept');
    } finally {
      unmount(app);
    }
  });

  it('shows a refused PIN inside the PIN row, not at the top of the page', async () => {
    answers({ credentials: [PASSKEY] });
    vi.mocked(post).mockRejectedValue(new Error('wrong current password'));
    const app = await open();
    try {
      const card = target.querySelector('[data-testid="pin-card"]');
      const [password, digits] = card.querySelectorAll('input');
      password.value = 'not-it';
      password.dispatchEvent(new Event('input', { bubbles: true }));
      digits.value = '12a34';
      digits.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      expect(digits.value).toBe('1234');
      [...card.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Save PIN').click();
      for (let i = 0; i < 20; i++) await Promise.resolve();
      flushSync();

      const alerts = target.querySelectorAll('[role="alert"]');
      expect(alerts).toHaveLength(1);
      expect(card.contains(alerts[0])).toBe(true);
      expect(alerts[0].textContent.trim()).toBe('wrong current password');
    } finally {
      unmount(app);
    }
  });
});
