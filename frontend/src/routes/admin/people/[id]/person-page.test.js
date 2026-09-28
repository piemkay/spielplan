/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/jellyfin.js', () => ({ jellyfinDirectory: vi.fn() }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  return {
    page: writable({ url: new URL('http://localhost/admin/people/2'), params: { id: '2' }, state: {} })
  };
});
vi.mock('$app/navigation', async () => {
  const stores = /** @type {any} */ (await import('$app/stores'));
  return {
    pushState: (_url, state) => stores.page.update((p) => ({ ...p, state })),
    goto: vi.fn(async () => {})
  };
});

import * as stores from '$app/stores';
import { goto } from '$app/navigation';
import { api, get, post } from '$lib/api.js';
import { jellyfinDirectory } from '$lib/jellyfin.js';
import { session } from '$lib/session.svelte.js';
import PersonPage from './+page.svelte';

const page = /** @type {import('svelte/store').Writable<any>} */ (/** @type {unknown} */ (stores.page));

const person = (over) => ({
  id: 2,
  name: 'Jenny',
  role: 'member',
  is_active: true,
  jellyfin_user_id: null,
  jellyfin_link_state: null,
  has_pin: true,
  passkeys: 0,
  has_jellyfin_token: false,
  ...over
});

let target;
let roster;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  for (const fn of [api, get, post, goto]) vi.mocked(fn).mockReset();
  session.user = /** @type {any} */ ({ id: 1, name: 'admin', role: 'admin' });
  roster = [person({ id: 1, name: 'admin', role: 'admin', has_pin: false }), person()];
  vi.mocked(get).mockImplementation((path) =>
    Promise.resolve(path === '/admin/users' ? roster : [])
  );
  vi.mocked(jellyfinDirectory).mockResolvedValue({ cfg: { configured: true }, users: [] });
  // Back pops the sheet's history entry, as the browser would.
  vi.spyOn(history, 'back').mockImplementation(() =>
    page.update((p) => ({ ...p, state: { ...p.state, sheets: (p.state.sheets ?? []).slice(0, -1) } }))
  );
});

afterEach(() => {
  target.remove();
  session.user = null;
  page.update((p) => ({ ...p, state: {} }));
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open(id = 2) {
  page.update((p) => ({ ...p, params: { id: String(id) } }));
  const app = mount(PersonPage, { target });
  await settle();
  return app;
}

const button = (text) =>
  [...document.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith(text));

async function choose(label) {
  const option = [...document.querySelectorAll('button[role=menuitem]')].find(
    (b) => b.querySelector('.label')?.textContent === label
  );
  if (!(option instanceof HTMLElement)) throw new Error(`the confirmation offers no "${label}"`);
  option.click();
  await settle();
}

describe("a person's page (§6.6, decision 527)", () => {
  it('holds the last active admin to every floor, on the controls, and says why', async () => {
    const app = await open(1);
    try {
      for (const floor of ['demote', 'reset-password', 'reset-pin', 'disable', 'delete']) {
        const control = target.querySelector(`[data-floor="${floor}"]`);
        expect(control, floor).not.toBeNull();
        if (control.tagName === 'BUTTON') expect(control.disabled, floor).toBe(true);
      }
      expect(target.textContent).toContain('the only admin');
      expect(target.textContent).toContain("You can't reset your own password or PIN here.");
      expect(target.textContent).not.toMatch(/§|decision \d/);
    } finally {
      unmount(app);
    }
  });

  it('asks before it deletes, and only the confirmation writes', async () => {
    vi.mocked(api).mockResolvedValue({ ok: true });
    const app = await open();
    try {
      button('Delete Jenny').click();
      await settle();
      expect(api, 'opening the question wrote nothing').not.toHaveBeenCalled();
      expect(document.body.textContent).toContain("It can't be undone.");

      await choose('Delete Jenny');
      expect(api).toHaveBeenCalledWith('/admin/users/2', { method: 'DELETE' });
      expect(goto).toHaveBeenCalledWith('/admin/people', { replaceState: true });
    } finally {
      unmount(app);
    }
  });

  it('shows the one-time password right where the reset issued it, and only then', async () => {
    vi.mocked(post).mockResolvedValue({ ok: true, one_time_password: 'NEW-ONE-TIME' });
    const app = await open();
    try {
      expect(target.querySelector('[data-testid="user-otp"]')).toBeNull();
      button('Reset password').click();
      await settle();
      expect(post).not.toHaveBeenCalled();
      await choose('Reset password');
      expect(post).toHaveBeenCalledWith('/admin/users/2/reset-password');
      expect(target.querySelector('[data-testid="user-otp-value"]').textContent).toBe('NEW-ONE-TIME');
      expect(button('Copy')).toBeTruthy();
    } finally {
      unmount(app);
    }
  });

  it('asks before a PIN reset or an unlink as well', async () => {
    roster[1] = person({ jellyfin_user_id: 'jf-jenny', jellyfin_link_state: 'linked' });
    vi.mocked(post).mockResolvedValue({ ok: true });
    vi.mocked(api).mockResolvedValue({ ok: true });
    const app = await open();
    try {
      button('PIN for switching').click();
      await settle();
      await choose('Remove PIN');
      expect(post).toHaveBeenCalledWith('/admin/users/2/reset-pin');

      button('Unlink').click();
      await settle();
      expect(api).not.toHaveBeenCalled();
      await choose('Unlink');
      expect(api).toHaveBeenCalledWith('/admin/users/2/jellyfin', { method: 'DELETE' });
    } finally {
      unmount(app);
    }
  });
});
