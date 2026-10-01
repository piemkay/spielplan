import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cells,
  excluded,
  finish,
  hit,
  historyLine,
  more,
  next,
  payload,
  reset,
  search,
  setup,
  start,
  stepOf,
  toggle,
  total,
  undo
} from './ladderSetup.svelte.js';
import { homeKept } from './home.svelte.js';

const STEPS = [
  { tier: 6, word: 'All-time favourite', hint: 'Highest rated first.' },
  { tier: 5, word: 'Loved it', hint: 'Highest rated first.' },
  { tier: 0, word: 'Hated it', hint: 'Lowest rated first.' }
];

const film = (id, seen = false) => ({ id, name: `Film ${id}`, year: 2000, poster_path: null, seen });

let calls;
let replies;

beforeEach(() => {
  calls = [];
  replies = {};
  vi.stubGlobal(
    'fetch',
    vi.fn((url, init) => {
      const u = new URL(String(url), 'http://localhost');
      calls.push({ path: u.pathname, params: u.searchParams, body: init?.body ? JSON.parse(init.body) : null });
      const reply = replies[u.pathname] ?? { status: 404, body: {} };
      const { status = 200, body } = typeof reply === 'function' ? reply(u.searchParams) : reply;
      return Promise.resolve({
        ok: status < 400,
        status,
        headers: { get: () => null },
        text: async () => JSON.stringify(body)
      });
    })
  );
  replies['/api/ladder/setup'] = { body: { done: false, earlier_ratings: 0, steps: STEPS } };
  replies['/api/ladder/setup/films'] = (params) => {
    const start = Number(params.get('offset'));
    const left = [1, 2, 3, 4, 5].filter((id) => !(params.get('exclude') ?? '').split(',').includes(String(id)));
    return { body: { films: left.slice(start, start + 2).map((id) => film(id)), more: start + 2 < left.length } };
  };
  reset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const filmCalls = () => calls.filter((c) => c.path === '/api/ladder/setup/films');

describe('the draft', () => {
  it('opens on the best step with its first page, and pages on with the same exclusions', async () => {
    await start();
    expect(setup.status).toBe('steps');
    expect(setup.at).toBe(0);
    expect(filmCalls()[0].params.get('step')).toBe('6');
    expect(filmCalls()[0].params.get('offset')).toBe('0');
    expect(filmCalls()[0].params.get('limit')).toBe('12');
    expect(cells().map((f) => f.id)).toEqual([1, 2]);
    await more();
    expect(filmCalls()[1].params.get('offset')).toBe('2');
    expect(cells().map((f) => f.id)).toEqual([1, 2, 3, 4]);
    expect(setup.drafts[0].more).toBe(true);
  });

  it("leaves an earlier step's picks out of every later step's list", async () => {
    await start();
    toggle(film(1));
    toggle(film(2));
    toggle(film(2));
    expect(setup.drafts[0].picks.map((f) => f.id)).toEqual([1]);
    await next();
    expect(setup.at).toBe(1);
    expect(excluded()).toEqual([1]);
    expect(filmCalls().at(-1).params.get('step')).toBe('5');
    expect(filmCalls().at(-1).params.get('exclude')).toBe('1');
    expect(cells().map((f) => f.id)).toEqual([2, 3]);
    toggle(film(3));
    await next();
    expect(filmCalls().at(-1).params.get('exclude')).toBe('1,3');
  });

  it('moves a search hit off an earlier step onto this one, first and picked, and Undo puts it back', async () => {
    await start();
    toggle(film(1));
    toggle(film(2));
    await next();
    hit(film(1));
    expect(setup.drafts[0].picks.map((f) => f.id)).toEqual([2]);
    expect(setup.drafts[1].picks.map((f) => f.id)).toEqual([1]);
    expect(cells()[0].id).toBe(1);
    expect(stepOf(1)).toBe(1);
    expect(total()).toBe(2);

    undo();
    expect(setup.at).toBe(0);
    expect(setup.drafts[0].picks.map((f) => f.id)).toEqual([1, 2]);
    expect(setup.drafts[1].picks).toEqual([]);
    expect(cells().map((f) => f.id), 'the step comes back as it was, list included').toEqual([1, 2]);
  });

  it('keeps a found film first and once, even when its page lists it too', async () => {
    await start();
    hit(film(5));
    await more();
    await more();
    expect(cells().map((f) => f.id)).toEqual([5, 1, 2, 3, 4]);
    toggle(film(5));
    expect(cells()[0].id).toBe(5);
    expect(setup.drafts[0].picks).toEqual([]);
  });

  it('cannot undo past the first step', async () => {
    await start();
    toggle(film(1));
    undo();
    expect(setup.at).toBe(0);
    expect(setup.drafts[0].picks.map((f) => f.id)).toEqual([1]);
  });

  it('finishes with every pick at its step, and the kept Home is dropped', async () => {
    replies['/api/ladder/setup/finish'] = { body: { done: true, placed: 3, tiers: [] } };
    homeKept.payload = { setup_notice: {} };
    await start();
    toggle(film(1));
    await next();
    toggle(film(2));
    await next();
    toggle(film(3));
    expect(payload()).toEqual([
      { title_id: 1, tier: 6 },
      { title_id: 2, tier: 5 },
      { title_id: 3, tier: 0 }
    ]);
    await finish();
    expect(calls.at(-1).body).toEqual({ picks: payload() });
    expect(setup.status).toBe('done');
    expect(setup.result.placed).toBe(3);
    expect(homeKept.payload).toBeNull();
  });

  it('sends nothing with no pick, and reads a second finish as a ladder already set up', async () => {
    replies['/api/ladder/setup/finish'] = { status: 409, body: { detail: { reason: 'already_set_up', message: 'set' } } };
    await start();
    await finish();
    expect(calls.some((c) => c.path === '/api/ladder/setup/finish')).toBe(false);
    toggle(film(1));
    await finish();
    expect(setup.status).toBe('set');
  });

  it('keeps the picks and says why when the server refuses for another reason', async () => {
    replies['/api/ladder/setup/finish'] = {
      status: 409,
      body: { detail: { reason: 'bundle_swapped', message: 'Restart needed.' } }
    };
    await start();
    toggle(film(1));
    await finish();
    expect(setup.status).toBe('steps');
    expect(setup.error).toBe('Restart needed.');
    expect(total()).toBe(1);
  });

  it('opens on nothing for a member already set up', async () => {
    replies['/api/ladder/setup'] = { body: { done: true, earlier_ratings: 0, steps: STEPS } };
    await start();
    expect(setup.status).toBe('set');
    expect(filmCalls()).toEqual([]);
  });
});

describe('the search', () => {
  it('asks the library for films from two letters on, with their Watched marks', async () => {
    replies['/api/titles'] = {
      body: { items: [{ id: 9, kind: 'movie', name: 'Heat', year: 1995, poster_path: '/h.jpg', seen_state: 'seen' }] }
    };
    await search('h');
    expect(setup.hits).toEqual([]);
    expect(calls.some((c) => c.path === '/api/titles')).toBe(false);
    await search('he');
    const asked = calls.at(-1);
    expect([asked.params.get('kind'), asked.params.get('q'), asked.params.get('limit')]).toEqual(['movie', 'he', '16']);
    expect(setup.hits).toEqual([
      { id: 9, name: 'Heat', original_name: null, original_language: null, year: 1995, poster_path: '/h.jpg', seen: true }
    ]);
    await search('');
    expect(setup.hits).toBeNull();
  });

  it("leaves out the library's looser matches", async () => {
    const item = (id, name, match) => ({ id, kind: 'movie', name, year: 2000, poster_path: null, match });
    replies['/api/titles'] = {
      body: {
        items: [
          item(1, 'Western', 'strong'),
          item(2, 'Brazilian Western', 'strong'),
          item(3, 'Vampire Academy', 'weak'),
          item(4, 'A Tale of Two Sisters', 'weak')
        ]
      }
    };
    await search('western');
    expect(setup.hits.map((f) => f.name)).toEqual(['Western', 'Brazilian Western']);
  });
});

describe('the done screen', () => {
  it('names the history and the films waiting only when there are any', () => {
    expect(historyLine(104, 93)).toBe(
      'Your 104 earlier ratings are kept as history. The 93 others you rated before come back on ' +
        'Rate, one at a time, to find their step.'
    );
    expect(historyLine(2, 0)).toBe('Your 2 earlier ratings are kept as history.');
    expect(historyLine(0, 0)).toBe('');
  });
});
