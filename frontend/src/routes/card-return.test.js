/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { get as read } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

// Back lands on the entry a jump to Home stamped (decision 557 item 6): shallow routing on a store.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  goto: vi.fn(),
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  replaceState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

const HEAT = vi.hoisted(() => ({
  title: { id: 1, name: 'Heat', kind: 'movie', year: 1995, runtime_min: 170, seen_state: 'seen' },
  genres: [], credits: [], platform_ratings: { items: [], note: '' }, dna: { extracted: [], projected: [] },
  shares: [], actions: { play_on_jellyfin: null, play_reason: 'no_server' }
}));
// Each surface's own reads answer empty; only the card's read matters here.
vi.mock('$lib/api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  api: vi.fn(),
  post: vi.fn(),
  get: vi.fn(async (path) => {
    if (path === '/titles/1') return HEAT;
    if (path.startsWith('/rank')) {
      return { kind: 'movie', tier_set: [], tiers: [], rated: 0, rated_total: 0, set_up: true, filters: {} };
    }
    if (path.startsWith('/facets')) return { genres: [], decades: [] };
    if (path.startsWith('/taste/members')) return { members: [], default: [null, null] };
    return null;
  })
}));

import { session } from '$lib/session.svelte.js';
import RankPage from './rank/+page.svelte';
import TastePage from './taste/+page.svelte';
import ComparePage from './taste/compare/+page.svelte';

let app;
let target;

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target?.remove();
  session.user = null;
});

async function landOn(Page, state) {
  session.user = { id: 7, name: 'Jenny', role: 'member', must_change_password: false };
  nav.page.update((p) => ({ ...p, state }));
  target = document.createElement('div');
  document.body.appendChild(target);
  app = mount(Page, { target });
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

const card = () => document.querySelector('[role="dialog"][aria-label="Title detail"]');

describe.each([
  ['rank', RankPage],
  ['taste', TastePage],
  ['compare', ComparePage]
])('Back to %s from a jump to Home', (from, Page) => {
  it('reopens the card the jump left from and clears the stamp', async () => {
    await landOn(Page, { returnCard: { titleId: 1, from } });
    expect(card().querySelector('h2').textContent).toBe('Heat');
    expect(read(nav.page).state.returnCard).toBeUndefined();
  });

  it('opens nothing for a card another surface left', async () => {
    await landOn(Page, { returnCard: { titleId: 1, from: 'you' } });
    expect(card()).toBeNull();
    expect(read(nav.page).state.returnCard).toEqual({ titleId: 1, from: 'you' });
  });
});
