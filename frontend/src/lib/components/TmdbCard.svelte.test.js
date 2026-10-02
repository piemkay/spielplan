/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn(), qs: () => '' }));

// The card is a sheet, a history entry: `pushState` adds it and Back (`history.back`) takes it away.
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

import { api, get } from '$lib/api.js';
import { tmdbWants } from '$lib/beyond.svelte.js';
import { wishes } from '$lib/wish.svelte.js';
import TitleDetail from './TitleDetail.svelte';
import TmdbCard from './TmdbCard.svelte';

const HARBOUR = {
  tmdb_id: 910001,
  kind: 'movie',
  name: 'Harbour Lights',
  original_name: 'Harbour Lights',
  year: 2024,
  overview: "A lighthouse keeper's daughter returns to the town that forgot her.",
  genres: ['Drama', 'Romance'],
  poster: '/api/art/tmdb/harbourlights.jpg',
  link: 'https://www.themoviedb.org/movie/910001'
};

let target;
let app;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(api).mockReset();
  vi.mocked(get).mockReset();
  nav.page.update((p) => ({ ...p, state: {} }));
  vi.spyOn(history, 'back').mockImplementation(() =>
    nav.page.update((p) => ({ ...p, state: { ...p.state, sheets: (p.state.sheets ?? []).slice(0, -1) } }))
  );
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  vi.restoreAllMocks();
  for (const key of Object.keys(tmdbWants)) delete tmdbWants[key];
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

const el = (sel) => target.querySelector(sel);
const want = () => el('[data-testid="tmdb-want"]');

function openHit(props = {}) {
  app = mount(TmdbCard, { target, props: { hit: HARBOUR, onClose: vi.fn(), onOpenTitle: vi.fn(), ...props } });
  flushSync();
}

describe('a title only TMDB knows (decision 558)', () => {
  it('shows its poster from this origin, name, year, genres and overview, and nothing of taste', () => {
    openHit();
    const dialog = el('[role="dialog"]');
    expect(dialog.getAttribute('aria-label')).toBe('Harbour Lights');
    expect(dialog.querySelector('h2').textContent).toBe('Harbour Lights');
    expect(dialog.textContent).toContain('2024');
    expect(el('[data-testid="tmdb-card-genres"]').textContent).toBe('Drama, romance');
    expect(dialog.textContent).toContain('returns to the town that forgot her');
    expect(dialog.querySelector('img').getAttribute('src')).toBe('/api/art/tmdb/harbourlights.jpg');
    expect(dialog.textContent).toContain('Not in Spielplan yet');
    expect(dialog.textContent).toContain('like any other film.');
    expect(dialog.textContent).toContain('Details from TMDB');
    const link = el('[data-testid="tmdb-link"]');
    expect([link.textContent.trim(), link.getAttribute('href'), link.target, link.rel]).toEqual([
      'View on TMDB', HARBOUR.link, '_blank', 'noreferrer'
    ]);
    for (const id of ['title-watched', 'title-not-seen', 'rank-card-tier', 'title-more', 'title-not-for-me']) {
      expect(el(`[data-testid="${id}"]`), id).toBeNull();
    }
    // Want it is the one action.
    expect([...dialog.querySelectorAll('button')].map((b) => b.textContent.trim())).toEqual(['', 'Want it']);
  });

  it('Want it puts it on the wish list, and a second tap takes it off', async () => {
    vi.mocked(api)
      .mockResolvedValueOnce({ title_id: 1000000012, state: 'want', owned: false, minted: true })
      .mockResolvedValueOnce({ state: null });
    const epoch = wishes.epoch;
    openHit();
    expect(want().classList.contains('btn-primary')).toBe(true);
    want().click();
    await settle();
    expect(vi.mocked(api).mock.calls[0]).toEqual(['/wish/tmdb/movie/910001', { method: 'PUT' }]);
    expect(want().textContent.trim()).toBe('On the wish list');
    expect(want().getAttribute('aria-pressed')).toBe('true');
    expect(want().classList.contains('btn-tinted')).toBe(true);
    expect(wishes.epoch).toBe(epoch + 1);

    want().click();
    await settle();
    expect(vi.mocked(api).mock.calls[1]).toEqual(['/wish/1000000012', { method: 'DELETE' }]);
    expect(want().textContent.trim()).toBe('Want it');
  });

  it("opens the library's own card instead when the library holds it", async () => {
    vi.mocked(api).mockResolvedValueOnce({ title_id: 4, state: null, owned: true, minted: false });
    const order = [];
    const onClose = vi.fn(() => order.push('closed'));
    const onOpenTitle = vi.fn(() => order.push('opened'));
    openHit({ hit: { ...HARBOUR, tmdb_id: 910003, name: 'Chungking Express', year: 1994 }, onClose, onOpenTitle });
    want().click();
    await settle();
    expect(order).toEqual(['closed', 'opened']);
    expect(onOpenTitle).toHaveBeenCalledWith({ id: 4, kind: 'movie', name: 'Chungking Express', year: 1994 });
    expect(el('[role="dialog"]')).toBeNull();
  });

  it('says so when TMDB does not answer, and stays as it was', async () => {
    const err = Object.assign(new Error('Service Unavailable'), { status: 503 });
    vi.mocked(api).mockRejectedValueOnce(err);
    openHit();
    want().click();
    await settle();
    expect(el('[role="status"]').textContent).toBe("TMDB didn't answer. Try again in a moment.");
    expect(want().textContent.trim()).toBe('Want it');
  });
});

describe('a title minted for a wish, opened as a title card', () => {
  const card = (title = {}, wish = { state: 'want' }) => ({
    title: {
      id: 1000000012, kind: 'series', name: 'Northern Lights', year: 2025, origin: 'wished', is_owned: false,
      seen_state: 'unseen', tmdb_id: 920001, overview: 'A coastal town keeps its lights on.', ...title
    },
    genres: ['Mystery', 'Drama'],
    credits: [],
    platform_ratings: { items: [], note: '' },
    dna: { extracted: [], projected: [] },
    shares: [],
    ranking: { set_up: true, tier: null, tension: null, tiers: [] },
    wish: { ...wish, likely_too: [] },
    actions: { play_on_jellyfin: null, play_reason: 'not_in_library' }
  });

  function openCard(payload) {
    vi.mocked(get).mockResolvedValue(payload);
    app = mount(TitleDetail, {
      target,
      props: { titleId: 1000000012, onClose: () => {}, onPerson: () => {}, onStateChange: () => {} }
    });
  }

  it('keeps the short card while it is not owned, Want it its one action', async () => {
    vi.mocked(api).mockResolvedValueOnce({ state: null });
    openCard(card());
    await settle();
    const dialog = el('[role="dialog"]');
    expect(dialog.getAttribute('aria-label')).toBe('Title detail');
    expect(el('[data-testid="tmdb-card"]')).not.toBeNull();
    expect(dialog.querySelector('img').getAttribute('src')).toBe('/api/art/1000000012/poster');
    expect(el('[data-testid="tmdb-card-genres"]').textContent).toBe('Mystery, drama');
    expect(dialog.textContent).toContain('like any other series.');
    expect(el('[data-testid="tmdb-link"]').getAttribute('href')).toBe('https://www.themoviedb.org/tv/920001');
    for (const id of ['title-watched', 'title-seen-rate', 'title-not-for-me', 'rank-card-tier', 'title-more']) {
      expect(el(`[data-testid="${id}"]`), id).toBeNull();
    }
    expect(want().textContent.trim()).toBe('On the wish list');
    want().click();
    await settle();
    expect(vi.mocked(api).mock.calls[0]).toEqual(['/wish/1000000012', { method: 'DELETE' }]);
    expect(want().textContent.trim()).toBe('Want it');
  });

  it('is the full card once it is owned, or when it came from the bundle', async () => {
    openCard(card({ origin: 'acquired', is_owned: true }));
    await settle();
    expect(el('[data-testid="tmdb-card"]')).toBeNull();
    expect(el('[data-testid="title-watched"]')).not.toBeNull();
    unmount(app);

    openCard(card({ origin: 'bundle' }));
    await settle();
    expect(el('[data-testid="tmdb-card"]')).toBeNull();
    expect(el('[data-testid="title-unowned"]')).not.toBeNull();
  });
});
