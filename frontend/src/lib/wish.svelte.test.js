import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  clearWish,
  dismissArrival,
  groupHeading,
  likeLine,
  loadWorthGetting,
  othersLine,
  setWish,
  wishRowShown,
  wishSummary,
  wishes
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
    expect(wishSummary({ wanted: 4, both: 1 })).toBe('4 wanted, 1 by both of you');
    expect(wishSummary({ wanted: 2, both: 0 })).toBe('2 wanted');
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
});

describe('the wish list in the viewer\'s own words', () => {
  const me = { id: 1, name: 'Patrick' };
  const jenny = { id: 2, name: 'Jenny' };
  const sam = { id: 3, name: 'Sam' };

  it('heads each group by who wants it, the viewer as you', () => {
    expect(groupHeading([me], 1)).toBe('You want');
    expect(groupHeading([me, jenny], 1)).toBe('You both want');
    expect(groupHeading([jenny], 1)).toBe('Jenny wants');
    expect(groupHeading([jenny, sam], 1)).toBe('Jenny and Sam want');
    expect(groupHeading([me, jenny, sam], 1)).toBe('You, Jenny and Sam want');
  });

  it('says whether the others would likely enjoy it, and nothing where the scores do not say', () => {
    const others = [
      { ...jenny, likely: 'likely' },
      { ...me, likely: 'maybe' },
      { ...sam, likely: null }
    ];
    expect(othersLine(others, 1)).toBe('Jenny: likely too · you: maybe');
    expect(othersLine([], 1)).toBe('');
  });

  it("names a Worth getting row's liked film, or for two the other first", () => {
    const item = { like: { title_id: 9, name: 'Heat', terms: ['night city', 'heists'] } };
    expect(likeLine(item)).toBe('Like Heat · night city, heists');
    expect(likeLine(item, 'Jenny')).toBe('Jenny too · night city, heists');
    expect(likeLine({ like: null }, 'Jenny')).toBe('Jenny too');
    expect(likeLine({ like: null })).toBe('');
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

  it('moves nothing when the server refuses', async () => {
    answering({ detail: { reason: 'owned', message: "It's in the library already." } }, 422);
    const before = wishes.epoch;
    await expect(setWish(5, 'want')).rejects.toThrow("It's in the library already.");
    expect(wishes.epoch).toBe(before);
  });

  it('asks for For you or For you and the other by name', async () => {
    const fetch = answering({ items: [] });
    await loadWorthGetting('series', 'pair');
    expect(fetch.mock.calls[0][0]).toBe('/api/home/worth-getting?kind=series&with=pair');
  });
});
