/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// A title card is a sheet, which pushes a history entry as it opens.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  goto: vi.fn(),
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  replaceState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  beforeNavigate: () => {},
  afterNavigate: () => {}
}));

import HomePage from './+page.svelte';
import { tmdbWants } from '$lib/beyond.svelte.js';
import { homeKept } from '$lib/home.svelte.js';
import { homeFilters, resetHomeFilters } from '$lib/homeFilters.svelte.js';
import { session } from '$lib/session.svelte.js';
import { hideToast } from '$lib/toast.svelte.js';
import { wishes } from '$lib/wish.svelte.js';

const MEMBER = { id: 5, name: 'Jenny', role: 'member', nav: { account: [{ key: 'account' }] } };
const film = (id, name, owned) => ({ id, kind: 'movie', name, year: 2000, is_owned: owned });
const OWNED = [film(1, 'Dune', true)];
const NOT_OWNED = Array.from({ length: 12 }, (_, i) => film(100 + i, `Dune ${i + 1}`, false));
const hit = (tmdb_id, name) => ({
  tmdb_id, kind: 'movie', name, original_name: name, year: 2026, overview: '', genres: ['Drama'],
  poster: null, link: `https://www.themoviedb.org/movie/${tmdb_id}`
});

let target;
let app;

/**
 * The routes Home reads. `tmdb(params)` may return a promise, to answer late; `want` answers the PUT.
 *
 * @param {{tmdb?: (p: URLSearchParams) => any, want?: () => any}} [routes]
 */
function backend({
  tmdb = (_p) => ({ available: true, items: [] }),
  want = () => ({ title_id: 1000000012, state: 'want', owned: false, minted: true })
} = {}) {
  const seen = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, init) => {
      const method = init?.method ?? 'GET';
      seen.push(`${method} ${url}`);
      const u = new URL(String(url), 'http://localhost');
      let payload = {};
      if (u.pathname === '/api/titles') {
        const beyond = u.searchParams.get('owned') === 'not';
        const offset = Number(u.searchParams.get('offset') ?? 0);
        payload = beyond
          ? { items: NOT_OWNED.slice(offset, offset + 6), total: 14, hidden: {} }
          : { items: OWNED, total: 1, hidden: {} };
      } else if (u.pathname === '/api/facets') payload = { genres: ['Drama'], decades: [] };
      else if (u.pathname === '/api/home') payload = { kinds: ['movie'], library: { movie: 3 }, shelves: [] };
      else if (u.pathname === '/api/wish/tmdb') payload = await tmdb(u.searchParams);
      else if (u.pathname.startsWith('/api/wish/tmdb/')) payload = want();
      else if (u.pathname.startsWith('/api/titles/')) {
        const id = Number(u.pathname.split('/').at(-1));
        payload = {
          title: { id, kind: 'movie', name: 'Chungking Express', year: 1994, is_owned: true, origin: 'bundle' },
          genres: [], credits: [], platform_ratings: { items: [], note: '' },
          dna: { extracted: [], projected: [] }, shares: [], wish: { state: null, likely_too: [] },
          actions: { play_on_jellyfin: null, play_reason: 'no_server' }
        };
      } else if (u.pathname.startsWith('/api/prompts/finish')) payload = [];
      return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(payload) };
    })
  );
  return seen;
}

const wait = async (ms) => {
  await vi.advanceTimersByTimeAsync(ms);
  flushSync();
};

async function openHome() {
  Object.assign(session, { user: MEMBER, hasBundle: true, restartRequired: false });
  app = mount(HomePage, { target });
  await wait(0);
}

async function type(text) {
  const box = target.querySelector('[data-testid="home-search"]');
  box.value = text;
  box.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

const el = (sel) => target.querySelector(sel);
const names = (sel) => [...target.querySelectorAll(`${sel} .name`)].map((n) => n.textContent);
const reads = (seen, path) => seen.filter((line) => line.startsWith(`GET /api${path}`));

beforeEach(() => {
  vi.useFakeTimers();
  target = document.createElement('div');
  document.body.appendChild(target);
  // The same member's Home keeps its filters; a new member's starts from the defaults.
  homeKept.user = MEMBER.id;
  homeFilters.owned = false;
});

afterEach(() => {
  hideToast();
  if (app) unmount(app);
  app = null;
  resetHomeFilters();
  for (const key of Object.keys(tmdbWants)) delete tmdbWants[key];
  Object.assign(session, { user: null, hasBundle: null, restartRequired: null });
  Object.assign(homeKept, { user: null, epoch: 0, kinds: null, payload: null, scrollY: 0 });
  vi.useRealTimers();
  vi.unstubAllGlobals();
  target.remove();
});

describe('a search with Only in library off answers in three sections (decision 558)', () => {
  it('heads the library grid and lists the catalogue beyond it under the same filters', async () => {
    const seen = backend();
    await openHome();
    el('[data-testid="filter-toggle"]').click();
    flushSync();
    const genre = el('[data-testid="filter-genre"]');
    genre.value = 'Drama';
    genre.dispatchEvent(new Event('change', { bubbles: true }));
    await type('dune');
    await wait(400);

    expect(el('[data-testid="library-section-head"]').textContent).toBe('In your library');
    const more = el('[data-testid="beyond-catalogue"]');
    expect(more.querySelector('h2').textContent).toBe('More in Spielplan');
    expect(more.textContent).toContain('Not in your library yet');
    expect(names('[data-testid="beyond-catalogue"]')).toEqual(NOT_OWNED.slice(0, 6).map((t) => t.name));

    // The grid's own filters, over the titles not owned.
    const [grid] = reads(seen, '/titles?').filter((line) => line.includes('q=dune') && !line.includes('owned=not'));
    const [beyond] = reads(seen, '/titles?').filter((line) => line.includes('owned=not'));
    const gridParams = new URL(grid.split(' ')[1], 'http://x').searchParams;
    const beyondParams = new URL(beyond.split(' ')[1], 'http://x').searchParams;
    expect(beyondParams.get('genre')).toBe('Drama');
    for (const [key, value] of gridParams) {
      if (['owned', 'limit', 'offset', 'sort'].includes(key)) continue;
      expect(beyondParams.getAll(key), key).toContain(value);
    }
    expect(beyondParams.get('offset')).toBe('0');

    el('[data-testid="beyond-catalogue-more"]').click();
    await wait(0);
    expect(reads(seen, '/titles?').at(-1)).toContain('offset=6');
    expect(names('[data-testid="beyond-catalogue"]')).toHaveLength(12);
    expect(el('[data-testid="beyond-catalogue-more"]').textContent.trim()).toBe('Show 2 more');

    // A page of the last query must not land on the next one's list.
    await type('dunes');
    expect(el('[data-testid="beyond-catalogue-more"]').disabled).toBe(true);
  });

  it('shows none of it while Only in library is on', async () => {
    homeFilters.owned = true;
    const seen = backend();
    await openHome();
    await type('dune');
    await wait(1000);
    expect(el('[data-testid="library-section-head"]')).toBeNull();
    expect(el('[data-testid="beyond-sections"]')).toBeNull();
    expect(reads(seen, '/wish/tmdb')).toEqual([]);
  });

  it('opens a title of the catalogue on its own card', async () => {
    backend();
    await openHome();
    await type('dune');
    await wait(400);
    el('[data-testid="beyond-catalogue"] .card-wrap').click();
    await wait(0);
    expect(el('[role="dialog"][aria-label="Title detail"]')).not.toBeNull();
  });
});

describe('From TMDB', () => {
  it('asks from three letters, once the typing rests, and names what Spielplan does not know', async () => {
    const seen = backend({ tmdb: () => ({ available: true, items: [hit(910001, 'Harbour Lights')] }) });
    await openHome();
    await type('ha');
    await wait(1000);
    expect(reads(seen, '/wish/tmdb')).toEqual([]);
    expect(el('[data-testid="beyond-tmdb"]')).toBeNull();

    await type('har');
    await wait(300);
    expect(reads(seen, '/wish/tmdb'), 'still typing').toEqual([]);
    await wait(60);
    expect(reads(seen, '/wish/tmdb')).toEqual(['GET /api/wish/tmdb?kind=movie&q=har']);
    const section = el('[data-testid="beyond-tmdb"]');
    expect(section.querySelector('h2').textContent).toBe('From TMDB');
    expect(section.textContent).toContain("Spielplan doesn't know these films yet");
    expect(names('[data-testid="tmdb-hit"]')).toEqual(['Harbour Lights']);
    expect(el('[data-testid="tmdb-hit"] .meta').textContent).toBe('2026 · TMDB');
  });

  it('drops an answer to a query it has typed past', async () => {
    let late = () => {};
    backend({
      tmdb: (p) =>
        p.get('q') === 'gla'
          ? new Promise((resolve) => (late = () => resolve({ available: true, items: [hit(1, 'Glacier')] })))
          : { available: true, items: [hit(910002, 'Glass Orchard')] }
    });
    await openHome();
    await type('gla');
    await wait(400);
    await type('glass');
    await wait(400);
    expect(names('[data-testid="tmdb-hit"]')).toEqual(['Glass Orchard']);
    late();
    await wait(0);
    expect(names('[data-testid="tmdb-hit"]')).toEqual(['Glass Orchard']);
  });

  it('is absent when TMDB is not available', async () => {
    backend({ tmdb: () => ({ available: false, items: [] }) });
    await openHome();
    await type('harbour');
    await wait(400);
    expect(el('[data-testid="beyond-catalogue"]')).not.toBeNull();
    expect(el('[data-testid="beyond-tmdb"]')).toBeNull();
  });

  it("Want it on a row puts it on the wish list and tells Home's shelves", async () => {
    const seen = backend({ tmdb: () => ({ available: true, items: [hit(910001, 'Harbour Lights')] }) });
    await openHome();
    await type('harbour');
    await wait(400);
    const epoch = wishes.epoch;
    const button = el('[data-testid="tmdb-hit-want"]');
    expect(button.textContent.trim()).toBe('Want it');
    button.click();
    await wait(0);
    expect(seen).toContain('PUT /api/wish/tmdb/movie/910001');
    expect(button.textContent.trim()).toBe('On the wish list');
    expect(button.getAttribute('aria-pressed')).toBe('true');
    expect(wishes.epoch).toBe(epoch + 1);
  });

  it("opens the library's own card when the title turns out to be owned", async () => {
    const seen = backend({
      tmdb: () => ({ available: true, items: [hit(910003, 'Chungking Express')] }),
      want: () => ({ title_id: 4, state: null, owned: true, minted: false })
    });
    await openHome();
    await type('chungking');
    await wait(400);
    el('[data-testid="tmdb-hit-want"]').click();
    await wait(0);
    expect(seen).toContain('GET /api/titles/4');
    expect(el('[role="dialog"][aria-label="Title detail"]')).not.toBeNull();
    expect(el('[data-testid="tmdb-hit-want"]').textContent.trim()).toBe('Want it');
  });

  it('opens a row on the short card', async () => {
    backend({ tmdb: () => ({ available: true, items: [hit(910001, 'Harbour Lights')] }) });
    await openHome();
    await type('harbour');
    await wait(400);
    el('[data-testid="tmdb-hit"] .open').click();
    flushSync();
    const dialog = el('[role="dialog"][aria-label="Harbour Lights"]');
    expect(dialog.querySelector('[data-testid="tmdb-want"]').textContent.trim()).toBe('Want it');
  });
});
