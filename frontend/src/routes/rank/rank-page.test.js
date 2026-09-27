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
        entry({ title_id: 2, name: 'Drive', year: 2011, tier: 4, badge: 'A — just above Prisoners' }),
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

// Answers by URL, not call order: the page and the title card fetch concurrently.
function route(url, init) {
  const method = init?.method ?? 'GET';
  const body = init?.body ? JSON.parse(init.body) : null;
  let payload;
  if (method !== 'GET') posts.push({ url, body });
  if (url.includes('/api/facets')) payload = { genres: ['Thriller'], decades: [1990] };
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

describe('the board (decision 527)', () => {
  it('heads each tier with its letter, the verdict it stands for and its count, best first', async () => {
    await open();
    const heads = [...target.querySelectorAll('[data-tier]')].map((t) => t.getAttribute('data-tier'));
    expect(heads).toEqual(['S', 'A+', 'A', 'B', 'C', 'D', 'F']);
    expect($('rank-letter-S').textContent).toBe('S');
    expect($('rank-tier-S').textContent).toContain('Liked');
    expect($('rank-tier-B').textContent).toContain('Fine');
    expect($('rank-tier-A').textContent).toContain('2');
    expect(target.querySelector('[data-tier="A+"]').textContent).toContain('Nothing here yet');
  });

  it('shows each row as its title and year, and counts the list', async () => {
    await open();
    expect($('rank-open-2').textContent).toContain('Drive');
    expect($('rank-open-2').textContent).toContain('2011');
    expect($('rank-count').textContent).toBe('3 of 40 films · best first');
  });

  it('opens the title card on a tap and writes nothing', async () => {
    await open();
    $('rank-open-2').click();
    await settle();
    expect(rank.opened).toBe(2);
    expect(fetchMock.mock.calls.some(([url]) => url.includes('/api/titles/2'))).toBe(true);
    expect(posts).toEqual([]);
  });

  it('offers Sharpen your list once there are two titles to compare', async () => {
    await open();
    expect($('rank-sharpen').textContent).toBe('Start');
  });
});

describe('Move opens an action sheet of the tiers (decision 527)', () => {
  it('lists the tiers best first with the current one checked, and Cancel writes nothing', async () => {
    await open();
    $('rank-move-1').click();
    await settle();

    const sheet = dialog('Move Heat');
    expect(sheet).toBeTruthy();
    const options = [...sheet.querySelectorAll('[role="menuitem"]')];
    expect(options.map((o) => o.querySelector('.label').textContent)).toEqual([
      'S', 'A+', 'A', 'B', 'C', 'D', 'F'
    ]);
    expect(options[0].textContent).toContain('Liked');
    expect(options[0].getAttribute('aria-current')).toBe('true');
    expect(options[1].getAttribute('aria-current')).toBeNull();

    [...sheet.querySelectorAll('button')].find((b) => b.textContent === 'Cancel').click();
    await settle();
    expect(dialog('Move Heat')).toBeNull();
    expect(posts).toEqual([]);
  });

  it('writes the same drop as before for the tier chosen, naming no neighbour', async () => {
    await open();
    $('rank-move-1').click();
    await settle();
    const options = [...dialog('Move Heat').querySelectorAll('[role="menuitem"]')];
    options[2].click();
    await settle();

    expect(posts).toHaveLength(1);
    expect(posts[0].url).toContain('/api/rank/drop');
    expect(posts[0].body).toEqual({ title_id: 1, tier: 4, above: null, below: null });
    expect(dialog('Move Heat')).toBeNull();
    expect(toast.message).toBe('Heat — moved to A');
  });
});

describe("the chip is the queue's entry point (§6.3, decision 496)", () => {
  it('reads as a question and opens the comparison queue, moving nothing', async () => {
    queueReplies.push({ kind: 'movie', pair: pair(), pool: 3 });
    await open();
    expect($('rank-chip-1').textContent).toBe('S or A+?');
    $('rank-chip-1').click();
    await settle();
    expect($('rank-queue')).toBeTruthy();
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

    expect(dialog('Sharpen your list')).toBeTruthy();
    const posters = [...$('rank-queue').querySelectorAll('[data-testid="rate-poster"]')];
    expect(posters.map((p) => p.getAttribute('data-title-id'))).toEqual(['1', '2']);
    expect($('rank-round').textContent).toBe('1 of 15 this round');
    expect($('rank-pair-tie').textContent).toBe('About the same');

    $('rank-pair-a').click();
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
    expect($('rank-pair-a')).toBeNull();
    $('rank-round-more').click();
    flushSync();
    expect($('rank-round-end')).toBeNull();
    expect($('rank-pair-a')).toBeTruthy();
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
