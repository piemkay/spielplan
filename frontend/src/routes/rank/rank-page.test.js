/**
 * @vitest-environment jsdom
 *
 * What the Rank surface DOES under a finger, on the payloads the first household's defects lived
 * in. Spec v2.1 §6.3, §6.8; decisions 295, 486, 494-496; owner instruction of 2026-09-25 after
 * the first household user test.
 *
 * Every defect here was in the markup: the tile was one button whose only handler lifted the
 * title, the chip that §6.3 makes the queue's entry point was a span inside it, the queue showed
 * two names in grey boxes, and the sheet carried one fixed line and no count. The store tests in
 * `rank.svelte.test.js` pin what each function does; only a mounted page can show which control a
 * tap reaches.
 *
 * NAMED `rank-page.test.js`, not `+page.svelte.test.js`, for `rate-page.test.js`'s reason:
 * SvelteKit reserves the `+` prefix inside `src/routes` and `vite build` refuses any other
 * `+`-named file.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT for the chip: a straddle chip exists only when the fit puts
 * a posterior across a cut, which a seeded e2e board does not do on demand. The e2e spec carries
 * the gestures a seeded board does support (open, Move, the round count).
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import RankPage from './+page.svelte';
import { rank, reset } from '$lib/rank.svelte.js';
import { session } from '$lib/session.svelte.js';

const entry = (over) => ({
  assigned_tier: null,
  straddle: null,
  straddle_badge: null,
  tension: null,
  ...over
});

/** One `GET /api/rank` payload, in `api/rank.py::_payload`'s shape. */
const board = (over = {}) => ({
  kind: 'movie',
  tier_set: ['F', 'D', 'C', 'B', 'A', 'A+', 'S'],
  tiers: [
    {
      index: 6,
      label: 'S',
      entries: [
        entry({
          title_id: 1, name: 'Heat', tier: 6, straddle: 5, straddle_badge: 'S/A+',
          badge: 'S — the only one'
        })
      ]
    },
    { index: 5, label: 'A+', entries: [] },
    {
      index: 4,
      label: 'A',
      entries: [
        entry({ title_id: 2, name: 'Drive', tier: 4, badge: 'A — just above Prisoners' }),
        entry({ title_id: 3, name: 'Prisoners', tier: 4, badge: 'A — just below Drive' })
      ]
    },
    { index: 3, label: 'B', entries: [] },
    { index: 2, label: 'C', entries: [] },
    { index: 1, label: 'D', entries: [] },
    { index: 0, label: 'F', entries: [] }
  ],
  rated: 3,
  rated_total: 40,
  fitting: false,
  queue_eligible: 1,
  why: '40 rated · 4 compared · tiers follow a typical split until you place a title yourself',
  filters: {},
  dna_tiers: null,
  ...over
});

const pair = (over = {}) => ({
  title_a: 1,
  title_b: 2,
  name_a: 'Heat',
  name_b: 'Drive',
  token: 'sealed-1',
  reason: 'Pick the one you enjoyed more — your answers are what put your board in order.',
  ...over
});

let fetchMock;
let posts;
let queueReplies;
let target;
let app;

/**
 * Answers by URL rather than by call order: the page reads the facets and the board at once on
 * mount, and the title card fetches on its own, so an order-based mock would be asserting the
 * scheduler.
 */
function route(url, init) {
  const method = init?.method ?? 'GET';
  const body = init?.body ? JSON.parse(init.body) : null;
  let payload;
  if (method !== 'GET') posts.push({ url, body });
  if (url.includes('/api/facets')) payload = { genres: [], decades: [] };
  else if (url.includes('/api/rank/queue/answer')) payload = queueReplies.shift();
  else if (url.includes('/api/rank/queue')) payload = queueReplies.shift();
  else if (url.includes('/api/rank/drop')) payload = board();
  else if (url.includes('/api/rank')) payload = board();
  else if (url.includes('/api/titles/')) {
    // `api/library.py`'s title payload, in the shape `TitleDetail.svelte.test.js` pins.
    payload = {
      title: {
        id: 2, name: 'Drive', kind: 'movie', year: 2011, runtime_min: 100, seen_state: 'seen',
        original_name: null, overview: null, trailer_key: null
      },
      model_line: { available: false, reason: 'no bundle' },
      credits: [],
      platform_ratings: { items: [], note: 'display-only' },
      dna: { extracted: [], projected: [] },
      actions: { play_on_jellyfin: null, show_on_map: { title_id: 2 } }
    };
  } else payload = {};
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => null },
    text: async () => JSON.stringify(payload ?? {})
  });
}

beforeEach(() => {
  posts = [];
  queueReplies = [];
  fetchMock = vi.fn(route);
  vi.stubGlobal('fetch', fetchMock);
  // The sheet measures itself (`bind:clientHeight`) to reserve room under the board, and jsdom
  // has no ResizeObserver; the measurement is layout, which is Playwright's to check.
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  );
  reset();
  rank.busy = false;
  session.user = {
    id: 5, name: 'Jenny', role: 'member', must_change_password: false, show_model: false
  };
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  reset();
  vi.unstubAllGlobals();
  target.remove();
});

async function settle() {
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

async function open() {
  app = mount(RankPage, { target });
  await settle();
}

const $ = (testid) => target.querySelector(`[data-testid="${testid}"]`);
const $$ = (selector) => document.body.querySelector(selector);

describe('a tap opens a title and Move moves it (decision 496)', () => {
  it('opens the title card on a tap and writes nothing', async () => {
    await open();
    $('rank-open-2').click();
    await settle();
    expect($$('aside[aria-label="Title detail"]')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => url.includes('/api/titles/2'))).toBe(true);
    expect($('rank-moving')).toBeNull();
    expect(posts).toEqual([]);
  });

  it('lifts on Move, and a tap on a title in another tier drops it into that tier', async () => {
    await open();
    $('rank-move-1').click();
    flushSync();
    expect($('rank-moving').textContent).toContain('Heat');

    $('rank-open-3').click();
    await settle();
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toContain('/api/rank/drop');
    expect(posts[0].body).toEqual({ title_id: 1, tier: 4, above: null, below: null });
    expect($$('aside[aria-label="Title detail"]')).toBeNull();
  });
});

describe("the chip is the queue's entry point (§6.3, decision 295)", () => {
  it('opens the comparison queue and lifts nothing', async () => {
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    await open();
    $('rank-chip-1').click();
    await settle();
    expect($('rank-queue')).toBeTruthy();
    expect($('rank-moving')).toBeNull();
    expect(posts).toEqual([]);
    expect(fetchMock.mock.calls.some(([url]) => url.includes('/api/rank/queue?'))).toBe(true);
  });
});

describe('the comparison sheet (decisions 483, 495)', () => {
  it('shows the pair as two posters carrying their titles, and counts the round', async () => {
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    queueReplies.push({
      kind: 'movie',
      pair: pair({ title_a: 3, name_a: 'Prisoners', token: 'sealed-2' }),
      placed: [
        { title_id: 1, name: 'Heat', tier: 6, badge: 'S — just above Drive' },
        { title_id: 2, name: 'Drive', tier: 6, badge: 'S — just below Heat' }
      ]
    });
    await open();
    $('rank-sharpen').click();
    await settle();

    const posters = [...$('rank-queue').querySelectorAll('[data-testid="rate-poster"]')];
    expect(posters.map((p) => p.getAttribute('data-title-id'))).toEqual(['1', '2']);
    expect($('rank-round').textContent).toBe('1 of 15 this round');
    expect($('rank-pair-reason').textContent).not.toMatch(/one more comparison/);

    $('rank-pair-a').click();
    await settle();
    expect($('rank-round').textContent).toBe('2 of 15 this round');
    expect($('rank-placed-1').textContent).toBe('Heat: S — just above Drive');
    expect($('rank-placed-2').textContent).toBe('Drive: S — just below Heat');
  });

  it('ends the round with a choice, and Keep going serves the pair it held', async () => {
    queueReplies.push({ kind: 'movie', pair: pair({ token: 'held' }), pool: 3 });
    await open();
    rank.queueOpen = true;
    rank.pair = pair({ token: 'held' });
    rank.roundAnswered = 15;
    rank.roundDone = true;
    flushSync();

    expect($('rank-round-end').textContent).toContain('15 comparisons for this round');
    expect($('rank-pair-a')).toBeNull();
    $('rank-round-more').click();
    flushSync();
    expect($('rank-round-end')).toBeNull();
    expect($('rank-pair-a')).toBeTruthy();
    expect($('rank-round').textContent).toBe('1 of 15 this round');
  });

  it("shows the queue's selection label only with Show the model on (decision 117)", async () => {
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    await open();
    rank.queueOpen = true;
    rank.pair = pair({ model: { arm: 'boundary', reason: 'its posterior crosses this boundary' } });
    flushSync();
    expect($('rank-pair-arm')).toBeNull();

    session.user = { ...session.user, show_model: true };
    flushSync();
    expect($('rank-pair-arm').textContent).toContain('boundary');
  });
});

describe('the board in the member register (decision 486)', () => {
  it('puts each tier letter and its count in its gutter', async () => {
    await open();
    expect($('rank-letter-S').textContent).toBe('S');
    expect($('rank-tier-S').textContent).toContain('1');
    expect($('rank-tier-A').textContent).toContain('2');
  });

  it('claims for each filter box only what it searches, and names no DNA or model noun', async () => {
    await open();
    expect($('rank-filter').getAttribute('placeholder')).toBe('filter by title');
    expect($('rank-dna').getAttribute('placeholder')).toBe('tag, e.g. cosy');
    const text = target.textContent;
    for (const noun of ['cutpoint', 'refit', 'tier_edit', 'ledger', 'straddle', 'DNA term']) {
      expect(text).not.toContain(noun);
    }
  });
});
