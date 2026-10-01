import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  facetsOf,
  gateLine,
  loadCompare,
  loadMembers,
  loadTaste,
  markPair,
  markX,
  mostAlike,
  mostDifferent,
  ownGroups,
  pickerOrder,
  placeWord,
  seatsReady,
  sharedGroups,
  showAll,
  strip,
  takeSeat
} from './taste.svelte.js';

describe('the track', () => {
  it('centres the middle and keeps the strongest term inside the ends', () => {
    expect(markX(0)).toBe(76);
    expect(markX(1)).toBe(140);
    expect(markX(-1)).toBe(12);
    expect(markX(0.9)).toBeCloseTo(133.6);
  });

  it('leaves two markers apart where they are, and parts two that would overlap, order kept', () => {
    expect(markPair(0.9, -0.9)).toEqual([markX(0.9), markX(-0.9)]);
    const [a, b] = markPair(0.1, 0.05);
    expect(a - b).toBeCloseTo(20);
    expect((a + b) / 2).toBeCloseTo((markX(0.1) + markX(0.05)) / 2);
    // At the end of the track the pair stays on it.
    expect(markPair(-1, -1)).toEqual([12, 32]);
    expect(markPair(1, 0.98)).toEqual([140, 120]);
  });

  it('reads a position out in words and never calls lower a dislike', () => {
    expect([0.9, 0.3, 0, -0.3, -0.9].map(placeWord)).toEqual([
      'sits high',
      'sits a little high',
      'near the middle',
      'lands a little lower',
      'lands lower'
    ]);
  });
});

describe('the poster strip', () => {
  const films = [1, 2, 3, 4].map((id) => ({ id }));

  it('shows every film that fits', () => {
    expect(strip(films, 0)).toEqual({ shown: films, plus: 0 });
    expect(strip(films.slice(0, 2), 0)).toEqual({ shown: films.slice(0, 2), plus: 0 });
  });

  it('turns the last slot into a count of the rest', () => {
    expect(strip(films, 2)).toEqual({ shown: films.slice(0, 3), plus: 3 });
  });
});

describe("Compare's orders", () => {
  const row = (term, pa, pb, facet = 'mood') => ({ term, label: term, facet, pa, pb });
  const rows = [
    row('a', 0.9, -0.9),
    row('b', 0.6, 0.6, 'era'),
    row('c', 0.1, 0.1),
    row('d', -0.3, 0.3, 'era'),
    row('e', -0.6, -0.5)
  ];
  const terms = (list) => list.map((r) => r.term);

  it('puts the widest gap first', () => {
    expect(terms(mostDifferent(rows))).toEqual(['a', 'd', 'e', 'b', 'c']);
  });

  it('puts terms clearly to one side for both before the closest pair near the middle', () => {
    expect(terms(mostAlike(rows))).toEqual(['b', 'e', 'c', 'd', 'a']);
  });

  it('filters Show all to a facet, and sections it by facet in the vocabulary order', () => {
    expect(showAll(rows, { sort: 'diff', facet: 'era' })).toEqual([
      { key: 'all', head: 'Era', facet: 'era', rows: [rows[3], rows[1]] }
    ]);
    expect(showAll(rows).map((s) => s.head)).toEqual(['All 5 terms']);
    const byFacet = showAll(rows, { sort: 'facet' });
    expect(byFacet.map((s) => [s.head, terms(s.rows)])).toEqual([
      ['Mood', ['a', 'c', 'e']],
      ['Era', ['b', 'd']]
    ]);
    expect(facetsOf(rows)).toEqual([
      ['mood', 'Mood'],
      ['era', 'Era']
    ]);
  });
});

describe("Compare's seats", () => {
  const members = [
    { id: 1, pickable: true },
    { id: 7, pickable: true },
    { id: 9, pickable: false }
  ];

  it('fills the seat being changed, and swaps when the pick sits in the other seat', () => {
    expect(takeSeat([1, 7], 1, 9)).toEqual([1, 9]);
    expect(takeSeat([1, 7], 0, 7)).toEqual([7, 1]);
    const same = [1, 7];
    expect(takeSeat(same, 0, 1)).toBe(same);
  });

  it('reads only two different people who can both be picked', () => {
    expect(seatsReady([1, 7], members)).toBe(true);
    expect(seatsReady([1, 9], members)).toBe(false);
    expect(seatsReady([1, null], members)).toBe(false);
    expect(seatsReady([1, 1], members)).toBe(false);
  });

  it('lists the viewer first in the picker, then everyone else in the roster order', () => {
    const roster = [{ id: 3 }, { id: 9 }, { id: 7 }, { id: 1 }];
    expect(pickerOrder(roster, 7).map((m) => m.id)).toEqual([7, 3, 9, 1]);
  });

  it('says what opens Compare in the kind it is on', () => {
    expect(gateLine('series')).toBe('Comparing series opens once two of you have each placed 20 series.');
  });
});

describe('the reads', () => {
  let fetchMock;
  const answer = (body) => ({
    ok: true,
    status: 200,
    headers: { get: () => null },
    text: async () => JSON.stringify(body)
  });

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(answer({}));
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  it('asks for one kind, and Compare for its two seats', async () => {
    await loadTaste('series');
    await loadMembers('movie');
    await loadCompare('movie', 1, 7);
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      '/api/taste?kind=series',
      '/api/taste/members?kind=movie',
      '/api/taste/compare?kind=movie&a=1&b=7'
    ]);
  });

  it("opens a row of Your taste onto the person's films above their middle, then below", async () => {
    const film = (id) => ({ id, name: `Film ${id}`, poster_path: null });
    fetchMock.mockResolvedValueOnce(answer({ term: 'mood.dark', high: [film(2), film(1)], low: [film(5)] }));
    fetchMock.mockResolvedValueOnce(answer({ term: 'mood.cosy', high: [], low: [film(5)] }));

    const dark = await ownGroups('movie', 'mood.dark');
    const cosy = await ownGroups('movie', 'mood.cosy');

    expect(fetchMock.mock.calls[0][0]).toBe('/api/taste/term?kind=movie&term=mood.dark');
    expect(dark.map((g) => [g.head, g.films.map((f) => f.id)])).toEqual([
      ['These sit high for you', [2, 1]],
      ['These land lower for you', [5]]
    ]);
    expect(cosy.map((g) => g.head)).toEqual(['These land lower for you']);
  });

  it('opens a row of Compare onto the films both placed as one group, never split by who', async () => {
    const films = [{ id: 4, name: 'Heat', poster_path: null }];
    fetchMock.mockResolvedValueOnce(answer({ term: 'mood.dark', films }));
    fetchMock.mockResolvedValueOnce(answer({ term: 'mood.cosy', films: [] }));

    expect(await sharedGroups('movie', 1, 7, 'mood.dark')).toEqual([{ head: "You've both placed these", films }]);
    expect(await sharedGroups('movie', 1, 7, 'mood.cosy')).toEqual([]);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/taste/compare/term?kind=movie&a=1&b=7&term=mood.dark');
  });
});
