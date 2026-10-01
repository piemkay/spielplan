import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  MISSING_FOR_MS,
  artReady,
  noteMissing,
  personSrc,
  posterSrc,
  preloadPoster,
  ready,
  titleIdOf
} from './art.js';
import { preloadArt } from './rate.svelte.js';
import { session } from './session.svelte.js';

afterEach(() => {
  vi.unstubAllGlobals();
  session.artEpoch = null;
});

describe('posterSrc', () => {
  it('is the same-origin route for a catalog title keyed id', () => {
    expect(posterSrc({ id: 949, poster_path: 'https://image.tmdb.org/t/p/w500/a.jpg' })).toBe(
      '/api/art/949/poster'
    );
  });

  it('reads title_id, which every Tonight payload and the shelves send', () => {
    expect(posterSrc({ title_id: 12, name: 'Heat' })).toBe('/api/art/12/poster');
  });

  it('prefers title_id when a payload carries both, because there id is its own row', () => {
    expect(posterSrc({ id: 7001, title_id: 12, name: 'Heat' })).toBe('/api/art/12/poster');
  });

  it('asks for a title with no poster_path too, since Jellyfin or the lookup may hold one', () => {
    expect(posterSrc({ id: 5448, poster_path: null })).toBe('/api/art/5448/poster');
  });

  it('never builds a URL for a title it cannot name', () => {
    for (const title of [null, undefined, {}, { id: null }, { id: '' }, { id: 'x' }, { id: 0 }]) {
      expect(posterSrc(title)).toBeNull();
    }
    expect(titleIdOf({ title_id: '42' })).toBe(42);
  });

  // A re-seed reuses app ids and the browser keeps a 200 for 180 days, so the URL carries the epoch.
  it("versions an app-minted title's URL by the database, and leaves a corpus id bare", () => {
    session.artEpoch = 'a1b2c3';
    expect(posterSrc({ id: 1000000003 })).toBe('/api/art/1000000003/poster?v=a1b2c3');
    expect(posterSrc({ title_id: 949 })).toBe('/api/art/949/poster');
    session.artEpoch = 'ffee00';
    expect(posterSrc({ id: 1000000003 })).toBe('/api/art/1000000003/poster?v=ffee00');
    session.artEpoch = null;
    expect(posterSrc({ id: 1000000003 })).toBe('/api/art/1000000003/poster');
  });

  it('never names a third-party host', () => {
    const src = posterSrc({ id: 1, poster_path: 'https://image.tmdb.org/t/p/w500/a.jpg' });
    expect(src.startsWith('/api/')).toBe(true);
  });
});

describe('personSrc', () => {
  it('asks this origin for a photo only when the credit says there is one', () => {
    expect(personSrc({ person_id: 31, name: 'Al Pacino', photo: true })).toBe('/api/art/person/31');
    expect(personSrc({ person_id: 31, name: 'Al Pacino', photo: false })).toBeNull();
    expect(personSrc({ person_id: 31, name: 'Al Pacino' })).toBeNull();
    expect(personSrc({ person_id: null, photo: true })).toBeNull();
  });

  it("versions an app-minted person's URL as it does a title's, and remembers a 404", () => {
    session.artEpoch = 'a1b2c3';
    expect(personSrc({ person_id: 1000000007, photo: true })).toBe(
      '/api/art/person/1000000007?v=a1b2c3'
    );
    noteMissing('/api/art/person/4043');
    expect(personSrc({ person_id: 4043, photo: true })).toBeNull();
    expect(posterSrc({ id: 4043 }), 'a person and a title never share an answer').toBe(
      '/api/art/4043/poster'
    );
  });
});

describe('preloading', () => {
  it('asks the browser for the poster, decoded, before the card is drawn', () => {
    const made = [];
    const decoded = [];
    vi.stubGlobal(
      'Image',
      class {
        constructor() {
          made.push(this);
        }
        decode() {
          decoded.push(/** @type {any} */ (this).src);
          return Promise.resolve();
        }
      }
    );
    preloadPoster({ id: 3 });
    expect(made.map((image) => image.src)).toEqual(['/api/art/3/poster']);
    expect(decoded).toEqual(['/api/art/3/poster']);
  });

  it("preloads every title on Rate's held-back card: the film and its shelves' posters", () => {
    const made = [];
    vi.stubGlobal(
      'Image',
      class {
        constructor() {
          made.push(this);
        }
      }
    );
    preloadArt({ card: { title: { id: 5 }, shelves: [{ films: [{ id: 6 }, { id: 7 }] }, { films: [] }] } });
    preloadArt(null);
    expect(made.map((image) => image.src)).toEqual([
      '/api/art/5/poster',
      '/api/art/6/poster',
      '/api/art/7/poster'
    ]);
  });

  it('remembers a preload that found no image, so the card it was for draws no <img>', () => {
    const made = [];
    vi.stubGlobal(
      'Image',
      class {
        constructor() {
          made.push(this);
        }
      }
    );
    preloadPoster({ id: 4051 });
    made[0].onerror();
    expect(posterSrc({ id: 4051 })).toBeNull();
  });
});

describe('art readiness', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('waits for nothing, and arms no timer, when no image can decode', () => {
    vi.useFakeTimers();
    expect(ready([null, {}], 150)).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('waits for the decode, but never longer than the cap', async () => {
    vi.useFakeTimers();
    let done = false;
    ready([{ decode: () => Promise.resolve() }, { decode: () => new Promise(() => {}) }], 150).then(
      () => (done = true)
    );
    await vi.advanceTimersByTimeAsync(149);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(done).toBe(true);
  });

  it('hides art still on its way until it lands, and shows cached art at once', () => {
    const heard = {};
    const img = { complete: false, dataset: {}, addEventListener: (type, fn) => (heard[type] = fn) };
    artReady(img);
    expect(img.dataset.art).toBe('loading');
    heard.load();
    expect(img.dataset.art).toBe('in');
    const cached = { complete: true, dataset: {} };
    artReady(cached);
    expect(cached.dataset.art).toBeUndefined();
  });
});

describe('a poster the route had nothing for', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('is not asked for again by any card while the 404 is still fresh', () => {
    expect(posterSrc({ id: 4040 })).toBe('/api/art/4040/poster');
    noteMissing('/api/art/4040/poster');
    expect(posterSrc({ id: 4040 })).toBeNull();
    expect(posterSrc({ title_id: 4040 })).toBeNull();
    expect(posterSrc({ id: 4041 })).toBe('/api/art/4041/poster');
  });

  it('is asked for again once the shortest 404 max-age has passed', () => {
    vi.useFakeTimers();
    noteMissing('/api/art/4042/poster');
    vi.advanceTimersByTime(MISSING_FOR_MS - 1000);
    expect(posterSrc({ id: 4042 })).toBeNull();
    vi.advanceTimersByTime(2000);
    expect(posterSrc({ id: 4042 })).toBe('/api/art/4042/poster');
  });

  it("keys the memory on the URL, so another database's version of an app-minted id is asked", () => {
    session.artEpoch = 'aaa111';
    noteMissing(posterSrc({ id: 1000000044 }));
    expect(posterSrc({ id: 1000000044 })).toBeNull();
    session.artEpoch = 'bbb222';
    expect(posterSrc({ id: 1000000044 })).toBe('/api/art/1000000044/poster?v=bbb222');
  });
});
