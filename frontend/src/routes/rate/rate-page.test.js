/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The page reads `$page.url` synchronously at init, so the store answers on subscribe; a sheet
// stays open while its history entry is in `$page.state`.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/rate'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

import RatePage from './+page.svelte';
import { rate, reset } from '$lib/rate.svelte.js';
import { session } from '$lib/session.svelte.js';

const WORDS = [
  'Hated it', "Didn't like it", 'Not really for me', 'It was fine', 'Liked it', 'Loved it', 'All-time favourite'
];
const film = (id, name) => ({ id, name, original_name: name, original_language: 'en', poster_path: null });
const shelves = () =>
  [6, 5, 4, 3, 2, 1, 0].map((tier) => ({
    tier,
    word: WORDS[tier],
    count: tier === 5 ? 1 : 0,
    films: tier === 5 ? [film(9, 'Zodiac')] : []
  }));
const card = (over = {}) => ({
  token: 'tok-1',
  kind: 'movie',
  title: { ...film(41, 'Heat'), year: 1995, runtime_min: 170 },
  reason: "It's in your library.",
  shelves: shelves(),
  ...over
});
const DRIVE = card({ token: 'tok-2', title: { ...film(42, 'Drive'), year: 2011, runtime_min: 100 } });

/** One `GET /api/rate` envelope, in the shape of the plan's contract C7. */
const envelope = (over = {}) => ({
  setup: { done: true, earlier_ratings: 104, rated_before: 37 },
  session: { kinds: ['movie'], kind: 'movie', block: { slot: 4, size: 15, counter: '4 of 15' } },
  card: card(),
  preload: [],
  echo: null,
  done: null,
  drained: null,
  undo: { available: false, kind: null, name: null },
  ...over
});
const closed = (earlier) => ({
  setup: { done: false, earlier_ratings: earlier, rated_before: 0 },
  session: null,
  card: null,
  preload: [],
  echo: null,
  done: null,
  drained: null,
  undo: { available: false, kind: null, name: null }
});

let fetchMock;
let replies;
let target;
let app;

// Answers by URL: the page, the peek and the closed card read concurrently.
function route(url, init) {
  const path = url.replace(/^.*\/api/, '');
  let payload;
  if (path.startsWith('/ladder/setup')) {
    const steps = WORDS.slice().reverse().map((word, i) => ({ tier: 6 - i, word }));
    payload = { done: false, earlier_ratings: 0, steps };
  } else if (path.startsWith('/titles/')) {
    payload = { title: { id: 41, name: 'Heat', overview: 'A heist.' }, genres: [], credits: [] };
  } else payload = replies.shift() ?? envelope();
  void init;
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => null },
    text: async () => JSON.stringify(payload)
  });
}

beforeEach(() => {
  replies = [];
  fetchMock = vi.fn(route);
  vi.stubGlobal('fetch', fetchMock);
  // The store is module state shared by every case, so reset it.
  reset();
  rate.booted = false;
  rate.loading = true;
  rate.busy = false;
  rate.card = null;
  rate.setup = null;
  rate.session = null;
  rate.notice = '';
  rate.error = '';
  session.user = { id: 1, name: 'Patrick', role: 'member', must_change_password: false, show_model: false };
  nav.page.update((p) => ({ ...p, state: {} }));
  vi.spyOn(history, 'back').mockImplementation(() =>
    nav.page.update((p) => ({ ...p, state: { ...p.state, sheets: (p.state.sheets ?? []).slice(0, -1) } }))
  );
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

// A macrotask, not counted microtasks: `api.js` is three hops deep.
async function settle() {
  for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

async function open(payload) {
  replies.push(payload);
  app = mount(RatePage, { target });
  await settle();
}

const $ = (testid) => target.querySelector(`[data-testid="${testid}"]`);
const shelfRows = () => [...target.querySelectorAll('[data-testid="rate-shelf"]')];
const writes = () =>
  fetchMock.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([url, init]) => [url.replace(/^.*\/api/, ''), JSON.parse(init.body)]);
const press = (key) => window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));

describe('before the set-up (decision 550)', () => {
  it('is one card that explains the ladder and leads to the set-up, and nothing else', async () => {
    await open(closed(104));
    const card = $('rate-before-setup');
    expect(card.querySelector('h2').textContent).toBe('Rate on your own ladder.');
    expect(card.textContent).toContain('one of seven steps, from All-time favourite down to Hated it');
    expect($('rate-earlier').textContent).toContain('Your 104 earlier ratings are kept as history.');
    expect($('rate-setup-cta').getAttribute('href')).toBe('/rate/setup');
    for (const id of ['rate-card', 'rate-undo', 'rate-counter', 'rate-kind', 'rate-done', 'rate-drained']) {
      expect($(id), id).toBeNull();
    }
    expect($('rate-surface').querySelector('h1').textContent.trim()).toBe('Rate');

    press('1');
    press('n');
    await settle();
    expect(writes()).toEqual([]);
  });

  it('says nothing about earlier ratings when there were none', async () => {
    await open(closed(0));
    expect($('rate-before-setup')).not.toBeNull();
    expect($('rate-earlier')).toBeNull();
    expect($('rate-before-setup').textContent).not.toMatch(/earlier ratings/);
  });
});

describe('the ladder card (decisions 545 and 551)', () => {
  it('shows the film, its meta line, its reason, Not seen and seven shelves, no letter anywhere', async () => {
    await open(envelope());
    expect($('rate-card-title').textContent).toBe('Heat');
    expect($('rate-card-meta').textContent).toBe('1995 · 2h 50m');
    expect($('rate-reason').textContent.trim()).toBe("It's in your library.");
    expect($('rate-not-seen').getAttribute('aria-label')).toBe('Not seen: Heat');
    expect(shelfRows().map((row) => row.querySelector('.word').textContent)).toEqual(WORDS.slice().reverse());
    expect(shelfRows()[1].getAttribute('aria-label')).toBe('Loved it, with Zodiac');
    expect($('rate-card').textContent).not.toMatch(/\b(S|A\+|A|B|C|D|F)\b/);
    expect(target.textContent).not.toMatch(/P\(seen\)|cdf|guess/);

    expect($('rate-kind').textContent).toContain('Films');
    expect($('rate-counter').textContent).toBe('4 of 15');
    expect($('rate-undo').disabled).toBe(true);
  });

  it('places with a tap and names the film and its word on the next card for a moment', async () => {
    await open(envelope());
    replies.push(
      envelope({
        card: DRIVE,
        session: { kinds: ['movie'], kind: 'movie', block: { slot: 5, size: 15, counter: '5 of 15' } },
        echo: { title_id: 41, name: 'Heat', tier: 5, word: 'Loved it' },
        undo: { available: true, kind: 'placement', name: 'Heat' }
      })
    );
    shelfRows()[1].click();
    flushSync();
    expect(shelfRows()[1].classList.contains('lit'), 'the tapped shelf lights at once').toBe(true);
    await settle();

    expect(writes()).toEqual([['/rate/place', expect.objectContaining({ card_token: 'tok-1', tier: 5 })]]);
    expect($('rate-card-title').textContent).toBe('Drive');
    expect($('rate-echo').textContent.trim()).toBe('Heat · Loved it');
    expect($('rate-card-meta'), 'the echo stands in for the meta line').toBeNull();
    expect($('rate-counter').textContent).toBe('5 of 15');
    expect($('rate-undo').getAttribute('aria-label')).toBe('Undo placing Heat');
    expect($('rate-undo').getAttribute('data-undo-kind')).toBe('placement');
  });

  it('places by holding a shelf and releasing it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    replies.push(envelope());
    app = mount(RatePage, { target });
    await vi.runAllTimersAsync();
    flushSync();
    const list = target.querySelector('[role="group"]');
    list.getBoundingClientRect = () => /** @type {any} */ ({ top: 0, bottom: 504, left: 0, right: 358 });
    shelfRows().forEach((row, i) => {
      row.getBoundingClientRect = () => /** @type {any} */ ({ top: i * 72, bottom: (i + 1) * 72 });
    });
    const pointer = (type, y) =>
      Object.assign(new MouseEvent(type, { bubbles: true, clientX: 100, clientY: y }), { pointerId: 3 });
    replies.push(envelope({ card: DRIVE }));
    list.dispatchEvent(pointer('pointerdown', 300));
    vi.advanceTimersByTime(250);
    flushSync();
    expect(target.querySelector('.look .look-word').textContent).toBe('Not really for me');
    list.dispatchEvent(pointer('pointermove', 30));
    list.dispatchEvent(pointer('pointerup', 30));
    await vi.runAllTimersAsync();
    flushSync();
    vi.useRealTimers();
    expect(writes()).toEqual([['/rate/place', expect.objectContaining({ tier: 6 })]]);
  });

  it('adds the guess and P(seen) in the data voice only with Show the numbers on', async () => {
    session.user = { ...session.user, show_model: true };
    await open(envelope({ card: card({ model: { p_seen: 0.81 } }) }));
    expect($('rate-reason').textContent).toContain('P(seen) 0.81');
    replies.push(
      envelope({
        card: DRIVE,
        echo: { title_id: 41, name: 'Heat', tier: 5, word: 'Loved it', model: { guess_word: 'Liked it', cdf: 0.812 } }
      })
    );
    shelfRows()[1].click();
    await settle();
    expect($('rate-echo').textContent.trim()).toBe('Heat · Loved it · guess Liked it · cdf 0.81');
    expect($('rate-echo').textContent).not.toMatch(/\bA\+?\b/);
  });

  it('says Not seen in one tap, and from "About this film" too', async () => {
    await open(envelope());
    replies.push(envelope({ card: DRIVE, undo: { available: true, kind: 'not_seen', name: 'Heat' } }));
    $('rate-not-seen').click();
    await settle();
    expect(writes()).toEqual([['/rate/not-seen', expect.objectContaining({ card_token: 'tok-1' })]]);
    expect($('rate-echo'), 'Not seen echoes nothing').toBeNull();
    expect($('rate-undo').getAttribute('aria-label')).toBe('Undo not seen');

    target.querySelector('[aria-label="About Drive"]').click();
    await settle();
    expect($('rate-peek').textContent).toContain('Looking never counts as an answer.');
    replies.push(envelope());
    $('rate-peek-not-seen').click();
    await settle();
    expect(writes()[1]).toEqual(['/rate/not-seen', expect.objectContaining({ card_token: 'tok-2' })]);
  });

  it('takes the last answer back with Undo, and the film comes back', async () => {
    await open(envelope({ card: DRIVE, undo: { available: true, kind: 'placement', name: 'Heat' } }));
    replies.push(envelope());
    $('rate-undo').click();
    await settle();
    expect(writes()).toEqual([['/rate/undo', {}]]);
    expect($('rate-card-title').textContent).toBe('Heat');
    expect($('rate-undo').disabled).toBe(true);
  });

  it('switches between films and series from the title', async () => {
    await open(envelope());
    $('rate-kind').click();
    flushSync();
    const sheet = target.querySelector('[role="dialog"][aria-label="What to rate"]');
    const options = [...sheet.querySelectorAll('[role="menuitem"]')];
    expect(options.map((o) => o.textContent.trim())).toEqual(['Films', 'Series']);
    expect(options[0].getAttribute('aria-current')).toBe('true');
    replies.push(
      envelope({ session: { kinds: ['series'], kind: 'series', block: { slot: 1, size: 15, counter: '1 of 15' } } })
    );
    options[1].click();
    await settle();
    expect(writes()).toEqual([['/rate/session', { kinds: ['series'], head: [] }]]);
    expect($('rate-kind').textContent).toContain('Series');
  });
});

describe('the keyboard (decision 550)', () => {
  it('places with 1 to 7 from the top, says Not seen with N and undoes with Z', async () => {
    await open(envelope({ undo: { available: true, kind: 'placement', name: 'Up' } }));
    for (const key of ['1', '7', 'n', 'Z']) {
      replies.push(envelope({ undo: { available: true, kind: 'placement', name: 'Up' } }));
      press(key);
      await settle();
    }
    expect(writes().map(([url, body]) => [url, body.tier])).toEqual([
      ['/rate/place', 6],
      ['/rate/place', 0],
      ['/rate/not-seen', undefined],
      ['/rate/undo', undefined]
    ]);
  });

  it('keys only the shelves there are, and nothing while a sheet is open or a field is typed in', async () => {
    await open(envelope({ card: card({ shelves: shelves().slice(0, 3) }) }));
    press('4');
    await settle();
    expect(writes()).toEqual([]);

    $('rate-kind').click();
    flushSync();
    press('1');
    await settle();
    expect(writes()).toEqual([]);
  });
});

describe("a block's end (decision 550)", () => {
  it('is a screen of its own that counts the films still waiting, then Rate 15 more goes on', async () => {
    const undo = { available: true, kind: 'placement', name: 'Heat' };
    await open(envelope({ card: DRIVE, done: { rated_before: 3, noun: 'films' }, undo }));
    const done = $('rate-done');
    expect(done.querySelector('h2').textContent).toBe("That's 15.");
    expect(done.textContent).toContain('Your suggestions just got sharper.');
    expect($('rate-done-waiting').textContent.trim()).toBe('3 films you rated before still wait for their step.');
    expect($('rate-done-home').getAttribute('href')).toBe('/');
    expect($('rate-undo').disabled, 'the fifteenth stays undoable').toBe(false);
    expect($('rate-card')).toBeNull();

    $('rate-done-more').click();
    flushSync();
    expect($('rate-done')).toBeNull();
    expect($('rate-card-title').textContent).toBe('Drive');
  });

  it('leaves out the waiting line when none wait', async () => {
    await open(envelope({ done: { rated_before: 0, noun: 'films' } }));
    expect($('rate-done')).not.toBeNull();
    expect($('rate-done-waiting')).toBeNull();
  });

  it('says so plainly when there is nothing more to rate', async () => {
    await open(envelope({ card: null, drained: { line: "There's nothing more to rate right now." } }));
    expect($('rate-drained').textContent).toBe("There's nothing more to rate right now.");
    expect($('rate-card')).toBeNull();
  });
});
