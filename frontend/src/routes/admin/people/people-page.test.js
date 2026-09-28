/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
import { session } from '$lib/session.svelte.js';
import PeoplePage from './+page.svelte';

const person = (over) => ({
  id: 2,
  name: 'Jenny',
  role: 'member',
  is_active: true,
  jellyfin_user_id: null,
  jellyfin_link_state: null,
  has_pin: false,
  passkeys: 0,
  has_jellyfin_token: false,
  ...over
});

let target;
let roster;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
  session.user = /** @type {any} */ ({ id: 1, name: 'admin', role: 'admin' });
  session.publicUrl = 'https://spielplan.example';
  roster = [
    person({ id: 1, name: 'admin', role: 'admin' }),
    person({
      id: 2,
      name: 'Jenny',
      jellyfin_user_id: 'jf-jenny',
      jellyfin_link_state: 'linked',
      passkeys: 2,
      has_pin: true
    }),
    person({
      id: 3,
      name: 'Patrick',
      is_active: false,
      jellyfin_user_id: 'jf-patrick',
      jellyfin_link_state: 'needs_relink'
    })
  ];
  vi.mocked(get).mockImplementation(() => Promise.resolve(roster));
});

afterEach(() => {
  target.remove();
  session.user = null;
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open() {
  const app = mount(PeoplePage, { target });
  await settle();
  return app;
}

const lines = (attr) => [...target.querySelectorAll(`[${attr}]`)].map((el) => el.textContent.trim());

describe('People (decision 527)', () => {
  it('lists everyone with role, state, Jellyfin, passkeys and PIN, each opening their page', async () => {
    const app = await open();
    try {
      expect(lines('data-user-facts')).toEqual([
        'Admin · you',
        'Member · linked to Jellyfin',
        'Member · disabled · Jellyfin needs sign-in'
      ]);
      expect(lines('data-user-signin')).toEqual([
        'No passkey · no PIN',
        '2 passkeys · PIN set',
        'No passkey · no PIN'
      ]);
      const hrefs = [...target.querySelectorAll('[data-testid="user-row"]')].map((a) =>
        a.getAttribute('href')
      );
      expect(hrefs).toEqual(['/admin/people/1', '/admin/people/2', '/admin/people/3']);
      expect(target.textContent.replace(/\s+/g, ' ')).toContain('1 of 3 has a passkey.');
      expect(target.querySelector('[data-testid="users-public-url"]').textContent).toContain(
        'https://spielplan.example'
      );
      expect(target.textContent).not.toMatch(/§|decision \d/);
    } finally {
      unmount(app);
    }
  });

  it('adds a person and shows their one-time password where it was issued, once', async () => {
    vi.mocked(post).mockResolvedValue({
      id: 4,
      name: 'Mia',
      role: 'admin',
      one_time_password: 'K7F2-9QPD-XXXX',
      note: 'shown once'
    });
    const app = await open();
    try {
      const input = target.querySelector('input[aria-label="New person\'s name"]');
      input.value = '  Mia ';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      const admin = [...target.querySelectorAll('[role=group] button')].find(
        (b) => b.textContent.trim() === 'Admin'
      );
      admin.click();
      flushSync();
      target.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }));
      await settle();

      expect(post).toHaveBeenCalledWith('/admin/users', { name: 'Mia', role: 'admin' });
      const otp = target.querySelector('[data-testid="user-otp"]');
      expect(otp.querySelector('[data-testid="user-otp-value"]').textContent).toBe('K7F2-9QPD-XXXX');
      expect(otp.textContent).toContain('One-time password for Mia');
      expect(otp.textContent).toContain('Shown once');
      expect(get, 'the roster is read again').toHaveBeenCalledTimes(2);
    } finally {
      unmount(app);
    }

    const again = await open();
    try {
      expect(target.querySelector('[data-testid="user-otp"]')).toBeNull();
    } finally {
      unmount(again);
    }
  });
});
