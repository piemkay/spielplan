/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const server = vi.hoisted(() => ({ switchable: [], summary: { wanted: 3, both: 1, members: 2 } }));
const COLLATERAL = vi.hoisted(() => ({
  title: { id: 3, name: 'Collateral', kind: 'movie', year: 2004, runtime_min: 120, seen_state: 'unseen' },
  genres: [], credits: [], platform_ratings: { items: [], note: '' }, dna: { extracted: [], projected: [] },
  shares: [], actions: { play_on_jellyfin: null, play_reason: 'no_server' }
}));
const LISTING = vi.hoisted(() => ({
  mine: [{ title_id: 3, kind: 'movie', name: 'Collateral', year: 2004, since: '2026-09-14T08:00:00Z',
           mine: true, likely: null, likely_too: [], wanters: [{ id: 7, name: 'Jenny' }], link: null }],
  others: [],
  copy_text: 'Collateral (2004)'
}));
vi.mock('$lib/api.js', () => ({
  api: vi.fn(),
  get: vi.fn(async (path) => {
    if (path === '/auth/switchable') return server.switchable;
    if (path === '/wish/summary') return server.summary;
    if (path === '/titles/3') return COLLATERAL;
    return path === '/wish' ? LISTING : [];
  }),
  post: vi.fn(),
  qs: (params) => `?${new URLSearchParams(params)}`
}));

const nav = vi.hoisted(() => ({ page: null, arrived: [] }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  afterNavigate: (fn) => nav.arrived.push(fn),
  goto: vi.fn(),
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  replaceState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

import { get as read } from 'svelte/store';
import { get } from '$lib/api.js';
import { session } from '$lib/session.svelte.js';
import AccountChip from './AccountChip.svelte';

const TASTE = { key: 'taste', href: '/taste', label: 'Your taste' };
const ACCOUNT = { key: 'account', href: '/account', label: 'Account & passkeys' };
const JENNY = { id: 7, name: 'Jenny', role: 'member' };

let target;
let chip;

beforeEach(() => {
  nav.arrived = [];
  nav.page.update((p) => ({ ...p, state: {} }));
  target = document.createElement('div');
  document.body.appendChild(target);
  server.switchable = [];
  server.summary = { wanted: 3, both: 1, members: 2 };
});

afterEach(() => {
  if (chip) unmount(chip);
  chip = null;
  target.remove();
  session.user = null;
});

async function openYou(account) {
  session.user = { ...JENNY, must_change_password: false, nav: { surfaces: [], account } };
  chip = mount(AccountChip, { target, props: { onLogout: () => {} } });
  flushSync();
  target.querySelector('[data-testid="account-chip"]').click();
  for (let i = 0; i < 5; i++) await Promise.resolve();
  flushSync();
  return target.querySelector('[role="dialog"]');
}

describe('You (decision 553)', () => {
  it('says nothing under its rows, and reads no roster of the household', async () => {
    const sheet = await openYou([TASTE, ACCOUNT]);
    expect(sheet.querySelectorAll('.list-footer')).toHaveLength(0);
    expect(vi.mocked(get).mock.calls.map(([path]) => path)).not.toContainEqual(
      expect.stringMatching(/^\/taste\/members/)
    );
  });

  it('keeps Your taste and the Wish list in one group, the list counting what is wanted', async () => {
    const sheet = await openYou([TASTE, ACCOUNT]);
    const taste = sheet.querySelector('[data-testid="taste-row"]');
    const wish = sheet.querySelector('[data-testid="you-wish-row"]');

    expect(taste.getAttribute('href')).toBe('/taste');
    expect(taste.closest('.list-group')).toBe(wish.closest('.list-group'));
    expect(taste.nextElementSibling).toBe(wish);
    expect(taste.closest('section').previousElementSibling.querySelector('h2').textContent).toBe('Jenny');
    expect(wish.querySelector('.value').textContent).toBe('3');
    expect(wish.getAttribute('aria-label')).toBe('Wish list, 3 wanted');
    // Taste is its own row, not one of the account entries below.
    expect(sheet.querySelectorAll('a[href="/taste"]')).toHaveLength(1);
    expect(sheet.querySelector('a[data-nav="account"]')).not.toBeNull();
  });

  it('has no Taste row when the server sends none, and the wish list stays', async () => {
    const sheet = await openYou([ACCOUNT]);
    expect(sheet.querySelector('[data-testid="taste-row"]')).toBeNull();
    expect(sheet.querySelector('[data-testid="you-wish-row"]')).not.toBeNull();
  });

  it('has no switch group at all while nobody else has set a PIN', async () => {
    server.switchable = [JENNY];
    const sheet = await openYou([TASTE, ACCOUNT]);
    expect(sheet.textContent).not.toContain('Switch profile');
    expect(sheet.textContent).not.toContain('No one else yet');
  });

  it('lists one row for each other member with a PIN, never the viewer', async () => {
    server.switchable = [JENNY, { id: 1, name: 'Patrick', role: 'admin' }, { id: 6, name: 'Sam', role: 'member' }];
    const sheet = await openYou([TASTE, ACCOUNT]);
    const group = [...sheet.querySelectorAll('section')].find(
      (s) => s.querySelector('h3')?.textContent === 'Switch profile'
    );
    const rows = [...group.querySelectorAll('.list-row')];
    expect(rows.map((r) => r.lastElementChild.textContent)).toEqual(['Patrick', 'Sam']);
    expect(group.previousElementSibling.querySelector('[data-testid="you-wish-row"]')).not.toBeNull();
  });

  it('opens the wish list over itself', async () => {
    const sheet = await openYou([TASTE, ACCOUNT]);
    sheet.querySelector('[data-testid="you-wish-row"]').click();
    for (let i = 0; i < 5; i++) await Promise.resolve();
    flushSync();
    expect(vi.mocked(get)).toHaveBeenCalledWith('/wish');
    const list = target.querySelector('[data-testid="wish-list-sheet"]');
    expect(list.closest('[role="dialog"]').getAttribute('aria-label')).toBe('Wish list');
    expect([...list.querySelectorAll('h3')].map((h) => h.textContent)).toEqual(['You want']);
    expect(list.querySelector('[data-testid="wish-item"]').textContent).toContain('Collateral');
    expect(target.querySelector('[aria-label="You"]'), 'You stays under it, for Back').not.toBeNull();

    // Back from the list, after a Remove there: You reads the count again.
    server.summary = { wanted: 2, both: 0, members: 2 };
    nav.page.update((p) => ({ ...p, state: { ...p.state, sheets: p.state.sheets.slice(0, -1) } }));
    for (let i = 0; i < 5; i++) await Promise.resolve();
    flushSync();
    expect(target.querySelector('[data-testid="wish-list-sheet"]')).toBeNull();
    expect(sheet.querySelector('[data-testid="you-wish-row"] .value').textContent).toBe('2');
  });
});

describe('Back from a jump to Home (decision 557 item 6)', () => {
  async function landOn(state) {
    session.user = { ...JENNY, must_change_password: false, nav: { surfaces: [], account: [TASTE] } };
    chip = mount(AccountChip, { target, props: { onLogout: () => {} } });
    flushSync();
    nav.page.update((p) => ({ ...p, state }));
    for (const arrive of nav.arrived) arrive({ type: 'popstate' });
    for (let i = 0; i < 10; i++) await Promise.resolve();
    flushSync();
  }
  const card = () => target.querySelector('[role="dialog"][aria-label="Title detail"]');

  it('reopens the card a jump from You left, alone, and only once', async () => {
    await landOn({ returnCard: { titleId: 3, from: 'you' } });
    expect(card().querySelector('h2').textContent).toBe('Collateral');
    expect(target.querySelector('[aria-label="You"]')).toBeNull();
    expect(target.querySelector('[data-testid="wish-list-sheet"]')).toBeNull();
    expect(read(nav.page).state.returnCard).toBeUndefined();
  });

  it("leaves a card another surface stamped to that surface", async () => {
    await landOn({ returnCard: { titleId: 3, from: 'rank' } });
    expect(card()).toBeNull();
    expect(read(nav.page).state.returnCard).toEqual({ titleId: 3, from: 'rank' });
  });
});
