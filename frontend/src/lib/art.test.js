/**
 * Where a poster is asked for. Spec v2.1 §6.8; decisions 483 and 484.
 *
 * The helper every 2:3 card reads its `<img src>` from, and the preload Rate runs during its
 * reveal hold. What is pinned is the three things a card can get wrong without anything looking
 * broken: a third-party URL, the wrong key (`/api/art/undefined/poster` on every Tonight card),
 * and the wrong title (the finish prompt's `id` is its own row, not the film's).
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { MISSING_FOR_MS, noteMissing, posterSrc, preloadPoster, titleIdOf } from './art.js';
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

  // A re-seed mints app ids from 1000000000 again for other titles, and the browser keeps a 200
  // for 180 days without asking: the URL has to change with the database or the old art stays.
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

describe('preloading', () => {
  it('asks the browser for the poster before the card is drawn', () => {
    const made = [];
    vi.stubGlobal(
      'Image',
      class {
        constructor() {
          made.push(this);
        }
      }
    );
    preloadPoster({ id: 3 });
    expect(made.map((image) => image.src)).toEqual(['/api/art/3/poster']);
  });

  it("preloads every title on Rate's held-back card: the sweep's one or the battle's two", () => {
    const made = [];
    vi.stubGlobal(
      'Image',
      class {
        constructor() {
          made.push(this);
        }
      }
    );
    preloadArt({ type: 'sweep', title: { id: 5 } });
    preloadArt({ type: 'battle', left: { id: 6 }, right: { id: 7 } });
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

// A card whose poster answered 404 dropped its <img>, and the next card for the same title - on
// the shelf, in the search grid, on the title card - drew one again, failed again and printed
// another 404 into the console. The page remembers the answer for as long as the browser's own
// cache would have given the same one.
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
