import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  MORE_LIMIT,
  kindNoun,
  moreQuery,
  tmdbKey,
  tmdbQuery,
  tmdbWants,
  toggleTmdbWant,
  wantError
} from './beyond.svelte.js';
import { wishes } from './wish.svelte.js';

/** A fetch that answers each call with the next of `answers`, as `[status, body]`. */
function answering(...answers) {
  const fetch = vi.fn(async (/** @type {string} */ _url, /** @type {any} */ _init) => {
    const [status, body] = answers.shift();
    return {
      ok: status < 400,
      status,
      statusText: status === 503 ? 'Service Unavailable' : '',
      headers: { get: () => null },
      text: async () => JSON.stringify(body)
    };
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

afterEach(() => {
  vi.unstubAllGlobals();
  for (const key of Object.keys(tmdbWants)) delete tmdbWants[key];
});

const HARBOUR = { tmdb_id: 910001, kind: 'movie', name: 'Harbour Lights', year: 2024 };

describe('the two reads beyond the library', () => {
  it('asks the catalogue for titles not owned, with the grid filters and its own pages', () => {
    const params = {
      q: 'dune', genre: 'Drama', decade: '', seen: 'any',
      term: ['mood.cosy'], not_term: [], person: ['12,13'], owned: 'any'
    };
    const first = new URL(moreQuery(['movie', 'series'], params), 'http://x');
    expect(first.pathname).toBe('/titles');
    expect(first.searchParams.getAll('kind')).toEqual(['movie', 'series']);
    expect(first.searchParams.get('q')).toBe('dune');
    expect(first.searchParams.get('genre')).toBe('Drama');
    expect(first.searchParams.getAll('term')).toEqual(['mood.cosy']);
    expect(first.searchParams.getAll('person')).toEqual(['12,13']);
    expect(first.searchParams.getAll('owned')).toEqual(['not']);
    expect(first.searchParams.get('limit')).toBe(String(MORE_LIMIT));
    const next = new URL(moreQuery(['movie'], params, { offset: 12 }), 'http://x');
    expect(next.searchParams.get('offset')).toBe('12');
  });

  it('asks TMDB from three letters, for every kind shown', () => {
    expect(tmdbQuery(['movie'], 'du')).toBeNull();
    expect(tmdbQuery(['movie'], '  du   ')).toBeNull();
    expect(tmdbQuery(['movie'], 'dun')).toBe('/wish/tmdb?kind=movie&q=dun');
    expect(tmdbQuery(['movie', 'series'], ' harbour  lights ')).toBe(
      '/wish/tmdb?kind=movie&kind=series&q=harbour+lights'
    );
  });

  it('names what TMDB holds by the kind shown', () => {
    expect(kindNoun(['movie'])).toBe('films');
    expect(kindNoun(['series'])).toBe('series');
    expect(kindNoun(['movie', 'series'])).toBe('titles');
  });
});

describe('Want it on a From TMDB title', () => {
  it('wishes the title it becomes, and a second tap takes the want back', async () => {
    const fetch = answering(
      [200, { title_id: 1000000012, state: 'want', owned: false, minted: true }],
      [200, { state: null }]
    );
    const before = wishes.epoch;
    await toggleTmdbWant(HARBOUR);
    expect(tmdbWants[tmdbKey(HARBOUR)]).toEqual({ title_id: 1000000012, state: 'want' });
    await toggleTmdbWant(HARBOUR);
    expect(tmdbWants[tmdbKey(HARBOUR)]).toEqual({ title_id: 1000000012, state: null });
    expect(fetch.mock.calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      'PUT /api/wish/tmdb/movie/910001',
      'DELETE /api/wish/1000000012'
    ]);
    expect(wishes.epoch).toBe(before + 2);
  });

  it('wishes nothing for a title the library holds, and says so', async () => {
    answering([200, { title_id: 4, state: null, owned: true, minted: false }]);
    const res = await toggleTmdbWant({ ...HARBOUR, tmdb_id: 910003, name: 'Chungking Express' });
    expect(res.owned).toBe(true);
    expect(res.title_id).toBe(4);
    expect(tmdbWants['movie:910003']).toBeUndefined();
  });

  it('says TMDB did not answer in the member words, and keeps no state', async () => {
    answering([503, { detail: { reason: 'tmdb_unavailable' } }]);
    const err = await toggleTmdbWant(HARBOUR).catch((e) => e);
    expect(wantError(err)).toBe("TMDB didn't answer. Try again in a moment.");
    expect(tmdbWants[tmdbKey(HARBOUR)]).toBeUndefined();
    expect(wantError({ status: 404, message: 'TMDB has no such title' })).toBe(
      'Could not save that — TMDB has no such title'
    );
  });
});
