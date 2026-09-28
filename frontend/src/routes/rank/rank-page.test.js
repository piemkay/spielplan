/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Sheets are history entries: `pushState` adds one and Back takes it away.
const nav = vi.hoisted(() => ({ state: {}, stack: [], subscribers: new Set() }));
function publish() {
  for (const run of nav.subscribers) run({ url: new URL('http://localhost/rank'), state: nav.state });
}
vi.mock('$app/stores', () => ({
  page: {
    subscribe: (run) => {
      nav.subscribers.add(run);
      run({ url: new URL('http://localhost/rank'), state: nav.state });
      return () => nav.subscribers.delete(run);
    }
  }
}));
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => {
    nav.stack.push(nav.state);
    nav.state = state;
    publish();
  }
}));

import RankPage from './+page.svelte';
import { rank, reset } from '$lib/rank.svelte.js';
import { session } from '$lib/session.svelte.js';
import { hideToast, toast } from '$lib/toast.svelte.js';

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
      verdict: 'Liked',
      entries: [
        entry({
          title_id: 1, name: 'Heat', year: 1995, tier: 6, straddle: 5, straddle_badge: 'S or A+?',
          badge: 'S — the only one'
        })
      ]
    },
    { index: 5, label: 'A+', verdict: 'Liked', entries: [] },
    {
      index: 4,
      label: 'A',
      verdict: 'Liked',
      entries: [
        entry({
          title_id: 2, name: 'Drive', year: 2011, tier: 4, assigned_tier: 4, badge: 'A — just above Prisoners',
          tension: 'You put it in A — your other answers still point to C'
        }),
        entry({ title_id: 3, name: 'Prisoners', year: 2013, tier: 4, badge: 'A — just below Drive' })
      ]
    },
    { index: 3, label: 'B', verdict: 'Fine', entries: [] },
    { index: 2, label: 'C', verdict: 'Disliked', entries: [] },
    { index: 1, label: 'D', verdict: 'Disliked', entries: [] },
    { index: 0, label: 'F', verdict: 'Disliked', entries: [] }
  ],
  rated: 3,
  rated_total: 40,
  fitting: false,
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
let boardOver;
let tierReply;

/** A phone's width, read by the page as it mounts. */
function phone(width = 390) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true, writable: true });
}

// Answers by URL, not call order: the page and the title card fetch concurrently.
function route(url, init) {
  const method = init?.method ?? 'GET';
  const body = init?.body ? JSON.parse(init.body) : null;
  let payload;
  if (method !== 'GET') posts.push({ url, body });
  if (url.includes('/api/facets')) payload = { genres: ['Thriller'], decades: [1990] };
  else if (url.includes('/api/rank/queue/answer')) payload = queueReplies.shift();
  else if (url.includes('/api/rank/queue')) payload = queueReplies.shift();
  else if (url.includes('/api/rank/drop')) payload = board(boardOver);
  else if (url.includes('/api/rank/tier')) payload = tierReply;
  else if (url.includes('/api/rank')) payload = board(boardOver);
  else if (url.includes('/api/titles/')) {
    // `api/library.py`'s title payload, in the shape `TitleDetail.svelte.test.js` pins; its
    // `ranking` read off the same board, as `rank/read.py::standing` reads it.
    const id = Number(url.match(/\/api\/titles\/(\d+)/)[1]);
    const { tiers } = board(boardOver);
    const on = tiers.flatMap((t) => t.entries).find((e) => e.title_id === id);
    payload = {
      title: {
        id, name: on?.name ?? 'Drive', kind: 'movie', year: 2011, runtime_min: 100, seen_state: 'seen',
        original_name: null, overview: null, trailer_key: null
      },
      model_line: { available: false, reason: 'no bundle' },
      credits: [],
      platform_ratings: { items: [], note: 'display-only' },
      dna: { extracted: [], projected: [] },
      my_verdict: { value: 2, label: 'liked' },
      ranking: {
        tier: on?.tier ?? null,
        tension: on?.tension ?? null,
        tiers: tiers.map(({ index, label, verdict, count, entries }) => ({
          index, label, verdict, count: count ?? entries.length, entries: []
        }))
      },
      actions: { play_on_jellyfin: null, show_on_map: { title_id: id } }
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
  boardOver = {};
  tierReply = null;
  phone(1024);
  // jsdom measures nothing, so the desktop grid reads as one column wide.
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  // jsdom has no Web Animations; the board's animate:flip asks a leaving cell for its running ones.
  Element.prototype.getAnimations ??= () => [];
  // jsdom here has no storage; the page reads and writes the poster size through this one.
  const saved = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k) => saved.get(k) ?? null,
    setItem: (k, v) => saved.set(k, String(v))
  });
  nav.state = {};
  nav.stack = [];
  fetchMock = vi.fn(route);
  vi.stubGlobal('fetch', fetchMock);
  vi.spyOn(history, 'back').mockImplementation(() => {
    nav.state = nav.stack.pop() ?? {};
    publish();
  });
  reset();
  hideToast();
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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  target.remove();
});

/** Holds the reply to one route until `release()`; the call itself is recorded at once. */
function hold(part) {
  let release = () => {};
  fetchMock.mockImplementation((url, init) => {
    const reply = route(url, init);
    return url.includes(part) ? new Promise((r) => (release = () => r(reply))) : reply;
  });
  return () => release();
}

async function settle() {
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

async function open() {
  app = mount(RankPage, { target });
  await settle();
}

const $ = (testid) => target.querySelector(`[data-testid="${testid}"]`);
const dialog = (name) => target.querySelector(`[role="dialog"][aria-label="${name}"]`);

describe('a slow first read', () => {
  it("draws the board's shape and says so, naming no tier", async () => {
    const release = hold('/api/rank?');
    await open();
    expect($('rank-loading').querySelector('[role="status"]').textContent).toBe('Loading your list…');
    expect(target.querySelector('[data-tier]')).toBeNull();
    release();
    await settle();
    expect($('rank-loading')).toBeNull();
    expect(target.querySelector('[data-tier]')).toBeTruthy();
  });
});

describe('the board (decision 528)', () => {
  it('heads each tier with its letter, the verdict it stands for and its count, best first', async () => {
    await open();
    const heads = [...target.querySelectorAll('[data-tier]')].map((t) => t.getAttribute('data-tier'));
    expect(heads).toEqual(['S', 'A+', 'A', 'B', 'C', 'D', 'F']);
    expect($('rank-letter-S').textContent).toBe('S');
    expect($('rank-tier-S').textContent).toContain('Liked');
    expect($('rank-tier-B').textContent).toContain('Fine');
    expect($('rank-tier-A').textContent).toContain('2 films');
    expect(target.querySelector('[data-tier="A+"]').textContent).toContain('Nothing here yet');
  });

  it('shows each title as a lazy poster with no name beneath, and the count in the search field', async () => {
    await open();
    const drive = $('rank-open-2');
    expect(drive.getAttribute('aria-label')).toBe('Drive, 2011');
    expect(drive.textContent.trim()).toBe('');
    expect(drive.querySelector('img').getAttribute('loading')).toBe('lazy');
    expect($('rank-filter').getAttribute('placeholder')).toBe('Search 40 rated films');
  });

  it('marks a title that sits between two tiers with a dot, and says so', async () => {
    await open();
    expect($('rank-open-1').querySelector('.dot')).toBeTruthy();
    expect($('rank-open-1').getAttribute('aria-label')).toBe('Heat, 1995, between two tiers');
    expect($('rank-open-2').querySelector('.dot')).toBeNull();
  });

  it('opens the title card on a tap and writes nothing', async () => {
    await open();
    $('rank-open-2').click();
    await settle();
    expect(rank.opened).toBe(2);
    expect(fetchMock.mock.calls.some(([url]) => url.includes('/api/titles/2'))).toBe(true);
    expect(posts).toEqual([]);
  });
});

describe('Needs a look (§6.3)', () => {
  const look = () => target.querySelector('[aria-label="Needs a look"]');

  it('offers Sharpen your list with Start while no title sits between two tiers', async () => {
    await open();
    expect(look().textContent).toContain('Sharpen your list');
    expect($('rank-sharpen').textContent).toMatch(/^Start/);
  });

  it('counts the titles between two tiers, and Sharpen opens the round, moving nothing', async () => {
    boardOver = { straddling: 12 };
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    await open();
    expect(look().textContent).toContain('12 titles sit between two tiers');
    expect($('rank-sharpen').textContent).toMatch(/^Sharpen/);
    $('rank-sharpen').click();
    await settle();
    expect($('rank-queue')).toBeTruthy();
    expect(posts).toEqual([]);
  });

  it('holds its count while the round runs, and shows the new one once it closes', async () => {
    boardOver = { straddling: 12 };
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    queueReplies.push({ kind: 'movie', pair: pair({ token: 'sealed-2' }) });
    await open();
    $('rank-sharpen').click();
    await settle();
    boardOver = { straddling: 7 };
    $('rate-duel-A').click();
    await settle();
    expect(rank.straddling).toBe(7);
    expect(look().textContent).toContain('12 titles sit between two tiers');
    $('rank-queue-close').click();
    await settle();
    expect(look().textContent).toContain('7 titles sit between two tiers');
  });
});

describe('the title card opened from Rank (decision 528)', () => {
  it('shows where the title sits, and its tier sheet moves it with no neighbour', async () => {
    await open();
    $('rank-open-1').click();
    await settle();
    const row = $('rank-card-tier');
    expect(row.getAttribute('aria-label')).toBe('In your ranking: S, Liked');
    // Alone in its tier, it has nothing to be placed against.
    expect($('rank-card-place')).toBeNull();
    expect($('rank-card-tension')).toBeNull();

    row.click();
    await settle();
    const sheet = dialog('Move Heat');
    const options = [...sheet.querySelectorAll('[role="menuitem"]')];
    expect(options.map((o) => o.querySelector('.label').textContent)).toEqual([
      'S', 'A+', 'A', 'B', 'C', 'D', 'F'
    ]);
    expect(options[0].textContent).toContain('Liked');
    expect(options[0].getAttribute('aria-current')).toBe('true');
    [...sheet.querySelectorAll('button')].find((b) => b.textContent === 'Cancel').click();
    await settle();
    expect(dialog('Move Heat')).toBeNull();
    expect(posts).toEqual([]);

    $('rank-card-tier').click();
    await settle();
    [...dialog('Move Heat').querySelectorAll('[role="menuitem"]')][2].click();
    await settle();
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toContain('/api/rank/drop');
    expect(posts[0].body).toEqual({ title_id: 1, tier: 4, above: null, below: null });
    expect(toast.message).toBe('Heat moved to A');
    expect($('rank-card-tier').getAttribute('aria-label')).toBe('In your ranking: A, Liked');
  });

  it('offers Place with questions, about log2(n) of them', async () => {
    await open();
    $('rank-open-2').click();
    await settle();
    // The model's disagreement stays visible where the tier is changed (§6.3).
    expect($('rank-card-tension').textContent).toBe('You put it in A — your other answers still point to C');
    const place = $('rank-card-place');
    expect(place.getAttribute('href')).toBe('/rank/place/2?kind=movie');
    expect(place.textContent).toContain('1 quick question');
  });
});

describe('a tier shows two rows until opened (decision 528)', () => {
  const many = (n, from, tier = 4) =>
    Array.from({ length: n }, (_, i) =>
      entry({ title_id: from + i, name: `Film ${from + i}`, year: 2000, tier })
    );

  it('shows seven posters and +N on a phone, opens in place, and Show less closes it', async () => {
    phone();
    boardOver = { tiers: [{ index: 4, label: 'A', verdict: 'Liked', count: 12, entries: many(8, 10) }] };
    tierReply = { index: 4, count: 12, offset: 8, entries: many(4, 30) };
    await open();
    const reads = fetchMock.mock.calls.map(([url]) => url).filter((u) => u.includes('/api/rank?'));
    expect(reads[0]).toContain('per_tier=8');
    const tier = target.querySelector('[data-tier="A"]');
    expect(tier.querySelectorAll('[data-title]')).toHaveLength(7);
    expect($('rank-more-A').textContent).toContain('+5');
    expect($('rank-more-A').getAttribute('aria-label')).toBe('Show all 12 in A');

    $('rank-more-A').click();
    await settle();
    expect(tier.querySelectorAll('[data-title]')).toHaveLength(12);
    expect($('rank-more-A')).toBeNull();

    [...tier.querySelectorAll('button')].find((b) => b.textContent === 'Show less').click();
    flushSync();
    expect(tier.querySelectorAll('[data-title]')).toHaveLength(7);
    expect(posts).toEqual([]);
  });

  it('keeps a strip of the tiers on a phone that jumps to one and marks it', async () => {
    phone();
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    await open();
    const chips = [...target.querySelectorAll('nav[aria-label="Tiers"] button')];
    expect(chips.slice(0, 3).map((c) => c.getAttribute('aria-label'))).toEqual([
      'S, 1 film',
      'A+, 0 films',
      'A, 2 films'
    ]);
    expect(chips[0].getAttribute('aria-current')).toBe('true');
    chips[2].click();
    flushSync();
    expect(scrolled).toHaveBeenCalled();
    expect(chips[2].getAttribute('aria-current')).toBe('true');
    delete Element.prototype.scrollIntoView;
  });

  it('remembers a desktop poster size on the device', async () => {
    await open();
    target.querySelector('[aria-label="Large posters"]').click();
    flushSync();
    expect(localStorage.getItem('rank-poster-size')).toBe('large');
    unmount(app);
    app = null;
    await open();
    expect(target.querySelector('[aria-label="Large posters"]').getAttribute('aria-pressed')).toBe('true');
  });
});

describe('the keyboard lifts, moves and drops a poster (decision 528)', () => {
  const press = (el, key) => el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  const live = () => target.querySelector('[aria-live="polite"]').textContent;

  it('moves through the grid and past a tier edge, says where, and drops between neighbours', async () => {
    await open();
    const drive = $('rank-open-2');
    drive.focus();
    press(drive, ' ');
    flushSync();
    expect(live()).toBe('Drive, lifted. At the top of A.');
    press(drive, 'ArrowRight');
    flushSync();
    expect(live()).toBe('At the bottom of A.');
    press(drive, 'ArrowRight');
    flushSync();
    expect(live()).toBe('In B.');
    press(drive, 'ArrowLeft');
    flushSync();
    expect(live()).toBe('At the bottom of A.');

    press(drive, ' ');
    await settle();
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({ title_id: 2, tier: 4, above: 3, below: null });
    expect(toast.message).toBe('Drive moved to A');
  });

  it('rests the poster in its new spot until the board lands, never back where it was', async () => {
    await open();
    const release = hold('/api/rank/drop');
    const drive = $('rank-open-2');
    press(drive, ' ');
    press(drive, 'ArrowRight');
    flushSync();
    press(drive, ' ');
    await settle();
    expect(posts).toHaveLength(1);
    const tier = target.querySelector('[data-tier="A"]');
    expect(tier.querySelector('[data-title="2"]')).toBeNull();
    expect(tier.querySelector('.slot [data-testid="rate-poster"]').getAttribute('data-title-id')).toBe('2');

    release();
    await settle();
    expect(tier.querySelector('.slot')).toBeNull();
    expect(document.activeElement).toBe(tier.querySelector('[data-title="2"]'));
  });

  it('puts the poster back on Esc and writes nothing', async () => {
    await open();
    const drive = $('rank-open-2');
    press(drive, ' ');
    press(drive, 'ArrowRight');
    press(drive, 'Escape');
    flushSync();
    expect(live()).toBe('Drive, put back.');
    press(drive, ' ');
    press(drive, ' ');
    await settle();
    expect(posts).toEqual([]);
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

    expect(dialog('Sharpen your list')).toBeTruthy();
    const posters = [...$('rank-queue').querySelectorAll('[data-testid="rate-poster"]')];
    expect(posters.map((p) => p.getAttribute('data-title-id'))).toEqual(['1', '2']);
    expect($('rank-round').textContent).toBe('1 of 15 this round');
    expect($('rate-duel-TIE').getAttribute('aria-label')).toBe('About the same');
    expect($('rate-duel-A-much')).toBeNull();

    $('rate-duel-A').click();
    await settle();
    expect($('rank-round').textContent).toBe('2 of 15 this round');
    expect($('rank-placed-1').textContent).toContain('Heat');
    expect($('rank-placed-1').textContent).toContain('S — just above Drive');
    expect($('rank-placed-2').textContent).toContain('S — just below Heat');
  });

  it('ends the round with a choice, and Keep going serves the pair it held', async () => {
    queueReplies.push({ kind: 'movie', pair: pair({ token: 'held' }), pool: 3 });
    await open();
    rank.queueOpen = true;
    rank.pair = pair({ token: 'held' });
    rank.roundAnswered = 15;
    rank.roundDone = true;
    await settle();

    expect($('rank-round-end').textContent).toContain("That's 15.");
    expect($('rate-duel-A')).toBeNull();
    $('rank-round-more').click();
    flushSync();
    expect($('rank-round-end')).toBeNull();
    expect($('rate-duel-A')).toBeTruthy();
    expect($('rank-round').textContent).toBe('1 of 15 this round');
  });

  it('closes on Done and forgets the round', async () => {
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    await open();
    $('rank-sharpen').click();
    await settle();
    $('rank-queue-close').click();
    await settle();
    expect($('rank-queue')).toBeNull();
    expect(rank.queueOpen).toBe(false);
  });

  it("shows the queue's selection label only with Show the model on (decision 117)", async () => {
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    await open();
    rank.queueOpen = true;
    rank.pair = pair({ model: { arm: 'boundary', reason: 'its posterior crosses this boundary' } });
    await settle();
    expect($('rank-pair-arm')).toBeNull();

    session.user = { ...session.user, show_model: true };
    flushSync();
    expect($('rank-pair-arm').textContent).toContain('boundary');
  });
});

describe('search and one Filters control (§6.3)', () => {
  it('searches by title as the person types, without waiting for Enter (round-2 R5)', async () => {
    await open();
    const box = $('rank-filter');
    box.value = 'Taxi';
    box.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    await new Promise((resolve) => setTimeout(resolve, 260));
    await settle();
    const reads = fetchMock.mock.calls.map(([url]) => url).filter((u) => u.includes('/api/rank?'));
    expect(reads.at(-1)).toContain('q=Taxi');
  });

  it('holds genre, decade, seen, runtime and the taste tag, each named in its row', async () => {
    await open();
    expect($('rank-genre')).toBeNull();
    $('rank-filters').click();
    await settle();
    expect(dialog('Filters')).toBeTruthy();
    for (const [testid, name] of [
      ['rank-genre', 'Genre'],
      ['rank-decade', 'Decade'],
      ['rank-seen', 'Seen'],
      ['rank-runtime', 'Max length'],
      ['rank-dna', 'Taste tag']
    ]) {
      const label = $(testid).closest('label');
      expect(label, `${testid} has no label`).toBeTruthy();
      expect(label.textContent).toContain(name);
    }
  });

  it('shows a set filter as a chip that removes it', async () => {
    await open();
    $('rank-filters').click();
    await settle();
    const genre = $('rank-genre');
    genre.value = 'Thriller';
    genre.dispatchEvent(new Event('change', { bubbles: true }));
    await settle();
    expect(fetchMock.mock.calls.map(([url]) => url).at(-1)).toContain('genre=Thriller');

    const chip = $('rank-filter-chip-genre');
    expect(chip.textContent).toContain('Thriller');
    expect($('rank-filters').textContent.trim()).toBe('Filters · 1');
    chip.click();
    await settle();
    expect($('rank-filter-chip-genre')).toBeNull();
    expect($('rank-filters').textContent.trim()).toBe('Filters');
    expect(fetchMock.mock.calls.map(([url]) => url).at(-1)).not.toContain('genre=');
  });

  it('names no model noun on the board', async () => {
    await open();
    const text = target.textContent;
    for (const noun of ['cutpoint', 'refit', 'tier_edit', 'ledger', 'straddle', 'DNA', 'inferred']) {
      expect(text).not.toContain(noun);
    }
  });
});
