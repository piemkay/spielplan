/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(async () => []), post: vi.fn() }));

const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  goto: vi.fn(),
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

import { session } from '$lib/session.svelte.js';
import AccountChip from './AccountChip.svelte';

const TASTE = { key: 'taste', href: '/taste', label: 'Your taste' };
const ACCOUNT = { key: 'account', href: '/account', label: 'Account & passkeys' };

let target;
let chip;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (chip) unmount(chip);
  chip = null;
  target.remove();
  session.user = null;
});

async function openYou(account) {
  session.user = { id: 7, name: 'Jenny', role: 'member', must_change_password: false, nav: { surfaces: [], account } };
  chip = mount(AccountChip, { target, props: { onLogout: () => {} } });
  flushSync();
  target.querySelector('[data-testid="account-chip"]').click();
  for (let i = 0; i < 5; i++) await Promise.resolve();
  flushSync();
  return target.querySelector('[role="dialog"]');
}

describe('You', () => {
  it('opens Your taste from its own row under the head, and names nobody in its footnote', async () => {
    const sheet = await openYou([TASTE, ACCOUNT]);
    const row = sheet.querySelector('[data-testid="taste-row"]');

    expect(row.getAttribute('href')).toBe('/taste');
    expect(row.textContent.trim()).toBe('Your taste');
    const group = row.closest('section');
    expect(group.previousElementSibling.querySelector('h2').textContent).toBe('Jenny');
    expect(group.querySelector('.list-footer').textContent).toBe(
      'What sits high on your ladder, and where you and someone else meet and part.'
    );
    // Taste is its own row, not one of the account entries below.
    expect(sheet.querySelectorAll('a[href="/taste"]')).toHaveLength(1);
    expect(sheet.querySelector('a[data-nav="account"]')).not.toBeNull();
  });

  it('has no Taste row when the server sends none', async () => {
    const sheet = await openYou([ACCOUNT]);
    expect(sheet.querySelector('[data-testid="taste-row"]')).toBeNull();
    expect(sheet.querySelector('a[href="/taste"]')).toBeNull();
  });
});
