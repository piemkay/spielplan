/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  replaceState: () => {},
  beforeNavigate: () => {},
  afterNavigate: () => {}
}));
import HomePage from './+page.svelte';
import { homeKept } from '$lib/home.svelte.js';
import { homeFilters, resetHomeFilters } from '$lib/homeFilters.svelte.js';
import { session } from '$lib/session.svelte.js';

const MEMBER = { id: 5, name: 'Jenny', role: 'member', nav: { account: [{ key: 'account' }] } };
const term = (label) => ({ term: `x.${label}`, label, facet: 'structure', quoted: true });

const MIX = {
  kind: 'movie', pool: 'library', sort: 'match', for_you_available: false, total: 1, library_total: 1,
  beyond_total: 0, strong_total: null, limit: 60, offset: 0,
  recipe: {
    asks_for_like: false,
    ingredients: [{ title_id: 245, kind: 'movie', name: 'Knives Out', year: 2019, like: true, groups: [], terms: [], sheet: [] }],
    more: [term('murder mystery')], less: []
  },
  items: [{
    id: 7, kind: 'movie', name: 'Glass Onion', year: 2022, runtime_min: 139, is_owned: true, seen_state: 'unseen',
    why: [
      { title_id: 245, name: 'Knives Out', groups: [], like: true, terms: [term('murder mystery'), term('grand estate')] },
      { title_id: 11, name: 'Star Wars', groups: [], like: false, terms: [term('pulp')] }
    ]
  }]
};

const CAINE = { person_ids: [12, 13], person_id: 12, name: 'Michael Caine', photo: false };

const DETAIL = {
  title: { id: 7, name: 'Glass Onion', kind: 'movie', year: 2022, runtime_min: 139, seen_state: 'unseen', is_owned: true },
  why: 'Because you placed Knives Out high',
  genres: [], credits: [], platform_ratings: { items: [], note: 'display-only' },
  dna: { extracted: [], projected: [] }, shares: [], actions: { play_on_jellyfin: null, play_reason: 'no_server' }
};

let target;
let app;
let seen;

beforeEach(() => {
  seen = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url) => {
      seen.push(String(url));
      const u = new URL(String(url), 'http://localhost');
      let payload = {};
      if (u.pathname === '/api/mix/titles') payload = MIX;
      else if (u.pathname === '/api/mix/twists') payload = { seed: 0, twists: [] };
      else if (u.pathname === '/api/titles') {
        // An ordinary read echoes the people it was asked for, by name.
        const people = u.searchParams.getAll('person').includes('12,13') ? [CAINE] : [];
        payload = { items: [], total: 0, hidden: {}, applied: { terms: [], not_terms: [], people } };
      }
      else if (u.pathname === '/api/titles/7') payload = DETAIL;
      else if (u.pathname === '/api/facets') payload = { genres: [], decades: [] };
      else if (u.pathname.startsWith('/api/prompts/finish')) payload = [];
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(payload)
      });
    })
  );
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  resetHomeFilters();
  history.replaceState(null, '', '/');
  Object.assign(session, { user: null, hasBundle: null, restartRequired: null });
  Object.assign(homeKept, { user: null, epoch: 0, kinds: null, payload: null, scrollY: 0 });
  vi.unstubAllGlobals();
  target.remove();
});

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

// The same member's Home keeps its recipe; a new member's starts from the defaults.
async function open() {
  homeKept.user = MEMBER.id;
  Object.assign(session, { user: MEMBER, hasBundle: true, restartRequired: false });
  app = mount(HomePage, { target });
  await settle();
}

describe('a recipe on Home (decisions 559 and 560)', () => {
  it("takes the catalogue grid's place, its chips first in the chip row", async () => {
    homeFilters.like = ['245'];
    await open();
    expect(target.querySelector('[data-testid="home-mode"]').dataset.reason).toBe('recipe');
    expect(target.querySelector('.gridhead')).toBeNull();
    expect(seen.some((u) => u.startsWith('/api/mix/titles?') && u.includes('like=245'))).toBe(true);
    const first = target.querySelector('.chips').firstElementChild;
    expect(first.dataset.testid).toBe('recipe-chip');
    expect(first.querySelector('.label').textContent).toBe('Like Knives Out');
  });

  it("offers Like these films as the Filters panel's last cell", async () => {
    await open();
    target.querySelector('[data-testid="filter-toggle"]').click();
    flushSync();
    const panel = target.querySelector('#home-filters');
    expect(panel.lastElementChild.dataset.testid).toBe('filter-like');
    expect(panel.lastElementChild.textContent).toContain('Like these films');
  });

  it("opens a result's card with what it shares in place of the server's line", async () => {
    homeFilters.like = ['245'];
    homeFilters.less = ['11'];
    await open();
    target.querySelector('[data-testid="recipe-region-movie"] .card-wrap').click();
    await settle();
    expect(target.querySelector('[data-testid="title-why"]').textContent).toBe(
      'From Knives Out: murder mystery, grand estate · but pulp, like Star Wars'
    );
  });

  it('names a person a reloaded recipe address carries', async () => {
    history.replaceState(null, '', '/?person=12,13&like=245&kind=movie');
    await open();
    expect(target.querySelector('[data-testid="home-mode"]').dataset.reason).toBe('recipe');
    expect(target.querySelector('[data-testid="person-chip"] .label').textContent.trim()).toBe('Michael Caine');
  });

  it('returns the shelves once the last film is gone', async () => {
    homeFilters.like = ['245'];
    await open();
    target.querySelector('[data-testid="recipe-chip"] .x').click();
    await settle();
    expect(homeFilters.like).toEqual([]);
    expect(target.querySelector('[data-testid="home-mode"]').dataset.mode).toBe('shelves');
  });

  it('leaves the catalogue unread under a recipe, and reads it once the recipe goes', async () => {
    homeFilters.like = ['245'];
    homeFilters.terms = [{ id: 'x.heist', label: 'heist', facet: 'structure', mode: 'in' }];
    await open();
    const titles = () => seen.filter((u) => u.startsWith('/api/titles?'));
    expect(titles()).toEqual([]);
    target.querySelector('[data-testid="recipe-chip"] .x').click();
    await settle();
    // The grid's own read, then the empty grid's drop counts.
    expect(titles()[0]).toMatch(/term=x\.heist.*limit=60/);
    expect(target.querySelector('[data-testid="home-mode"]').dataset.reason).toBe('filter');
  });
});
