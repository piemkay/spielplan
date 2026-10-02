import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  clearWish,
  dismissArrival,
  likeLine,
  likelyTooLine,
  loadWishSummary,
  loadWorthGetting,
  peopleLine,
  restoreArrival,
  setWish,
  setWishTmdb,
  wishRowShown,
  wishSummary,
  wishes,
  worthLede,
  youLine
} from './wish.svelte.js';

/** A fetch that answers `status` with `body`, recording what was asked. */
function answering(body, status = 200) {
  const fetch = vi.fn(async (/** @type {string} */ _url, /** @type {any} */ _init) => ({
    ok: status < 400,
    status,
    statusText: '',
    headers: { get: () => null },
    text: async () => JSON.stringify(body)
  }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

afterEach(() => vi.unstubAllGlobals());

describe("Home's wish list row", () => {
  it('counts the list and how much of it more than one of you wants', () => {
    expect(wishSummary({ wanted: 4, both: 1, members: 2 })).toBe('4 wanted, 1 by both of you');
    expect(wishSummary({ wanted: 3, both: 1, members: 4 })).toBe('3 wanted, 1 by more than one of you');
    expect(wishSummary({ wanted: 2, both: 0, members: 2 })).toBe('2 wanted');
    expect(wishSummary({ wanted: 0, both: 0 })).toBe('Nothing on it yet');
    expect(wishSummary(undefined)).toBe('Nothing on it yet');
  });

  it('stands under Worth getting, or after the last shelf while anything is wanted', () => {
    const shelf = { id: 'worth_getting', sections: [] };
    expect(wishRowShown({ shelves: [shelf], wish: { wanted: 0 } })).toBe(true);
    expect(wishRowShown({ shelves: [], wish: { wanted: 1 } })).toBe(true);
    expect(wishRowShown({ shelves: [{ id: 'top_of_ledger' }], wish: { wanted: 0 } })).toBe(false);
    expect(wishRowShown(null)).toBe(false);
  });

  it('stands down for the day once the member puts it away (decision 554)', () => {
    const shelf = { id: 'worth_getting', sections: [] };
    expect(wishRowShown({ shelves: [shelf], wish: { wanted: 3, hidden: true } })).toBe(false);
    expect(wishRowShown({ shelves: [shelf], wish: { wanted: 3, hidden: false } })).toBe(true);
  });
});

describe('the wish list in the viewer\'s own words', () => {
  const me = { id: 1, name: 'Patrick' };
  const jenny = { id: 2, name: 'Jenny' };
  const sam = { id: 3, name: 'Sam' };

  it('names people as the viewer reads them, however many', () => {
    expect(peopleLine([me], 1)).toBe('You');
    expect(peopleLine([me, jenny], 1)).toBe('You and Jenny');
    expect(peopleLine([jenny, sam], 1)).toBe('Jenny and Sam');
    expect(peopleLine([me, jenny, sam], 1)).toBe('You, Jenny and Sam');
  });

  it("says on others' titles whether the viewer would enjoy it, and nothing the scores do not say", () => {
    expect(youLine('likely')).toBe('you: likely too');
    expect(youLine('maybe')).toBe('you: maybe');
    expect(youLine(null)).toBe('');
  });

  it("says on an unowned title's card who else would likely enjoy it, and nothing for nobody", () => {
    expect(likelyTooLine(['Jenny'])).toBe('Jenny would likely enjoy it too.');
    expect(likelyTooLine(['Jenny', 'Sam'])).toBe('Jenny and Sam would likely enjoy it too.');
    expect(likelyTooLine([])).toBe('');
    expect(likelyTooLine(undefined)).toBe('');
  });

  it("names a Worth getting row's liked film, or nothing", () => {
    const item = { like: { title_id: 9, name: 'Heat', terms: ['night city', 'heists'] } };
    expect(likeLine(item)).toBe('Like Heat · night city, heists');
    expect(likeLine({ like: { title_id: 9, name: 'Heat', terms: [] } })).toBe('Like Heat');
    expect(likeLine({ like: null })).toBe('');
  });

  it('says whom a Worth getting list is for, and names no one for everyone', () => {
    expect(worthLede('movie', { id: 1, name: 'Patrick' }, 1)).toBe(
      "Films the library doesn't have, closest to the ones you rate highest."
    );
    expect(worthLede('series', jenny, 1)).toBe(
      "Series the library doesn't have, closest to the ones Jenny rates highest."
    );
    expect(worthLede('movie', 'everyone', 1)).toBe(
      "Films the library doesn't have that would suit all of you, leaving out what anyone avoids."
    );
  });
});

describe('the wish routes', () => {
  it('writes the viewer\'s own row and tells Home to re-read', async () => {
    const fetch = answering({ state: 'want' });
    const before = wishes.epoch;
    expect(await setWish(5, 'want')).toEqual({ state: 'want' });
    const [url, init] = fetch.mock.calls[0];
    expect([url, init.method, JSON.parse(init.body)]).toEqual(['/api/wish/5', 'PUT', { state: 'want' }]);
    expect(wishes.epoch).toBe(before + 1);

    answering({ state: null });
    await clearWish(5);
    expect(vi.mocked(globalThis.fetch).mock.calls[0][1].method).toBe('DELETE');
    answering({ dismissed: true });
    await dismissArrival(5);
    const [dismissUrl, dismissInit] = vi.mocked(globalThis.fetch).mock.calls[0];
    expect([dismissUrl, dismissInit.method]).toEqual(['/api/wish/5/dismiss', 'POST']);
    expect(wishes.epoch).toBe(before + 3);
  });

  it("puts a dismissed arrival back with its own date, and reads You's count", async () => {
    const fetch = answering({ restored: true });
    const before = wishes.epoch;
    await restoreArrival(5, '2026-09-12T10:00:00Z');
    const [url, init] = fetch.mock.calls[0];
    expect([url, init.method, JSON.parse(init.body)]).toEqual([
      '/api/wish/5/restore', 'POST', { since: '2026-09-12T10:00:00Z' }
    ]);
    expect(wishes.epoch).toBe(before + 1);

    answering({ wanted: 3, both: 1, members: 2 });
    expect(await loadWishSummary()).toEqual({ wanted: 3, both: 1, members: 2 });
    expect(vi.mocked(globalThis.fetch).mock.calls[0][0]).toBe('/api/wish/summary');
  });

  it('wants a title only TMDB knows by its kind and TMDB id, and tells Home to re-read', async () => {
    const answer = { title_id: 1000000012, state: 'want', owned: false, minted: true };
    const fetch = answering(answer);
    const before = wishes.epoch;
    expect(await setWishTmdb('series', 920001)).toEqual(answer);
    const [url, init] = fetch.mock.calls[0];
    expect([url, init.method, init.body]).toEqual(['/api/wish/tmdb/series/920001', 'PUT', undefined]);
    expect(wishes.epoch).toBe(before + 1);
  });

  it('moves nothing when the server refuses', async () => {
    answering({ detail: { reason: 'owned', message: "It's in the library already." } }, 422);
    const before = wishes.epoch;
    await expect(setWish(5, 'want')).rejects.toThrow("It's in the library already.");
    expect(wishes.epoch).toBe(before);
  });

  it('asks for the viewer by default, or names a member or everyone', async () => {
    const fetch = answering({ items: [] });
    await loadWorthGetting('series');
    await loadWorthGetting('movie', 5);
    await loadWorthGetting('movie', 'everyone');
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/home/worth-getting?kind=series',
      '/api/home/worth-getting?kind=movie&for=5',
      '/api/home/worth-getting?kind=movie&for=everyone'
    ]);
  });
});
