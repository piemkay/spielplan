/**
 * Where a poster is asked for. Spec v2.1 §6.8; decisions 483 and 484.
 *
 * The helper every 2:3 card reads its `<img src>` from, and the preload Rate runs during its
 * reveal hold. What is pinned is the three things a card can get wrong without anything looking
 * broken: a third-party URL, the wrong key (`/api/art/undefined/poster` on every Tonight card),
 * and the wrong title (the finish prompt's `id` is its own row, not the film's).
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { posterSrc, preloadPoster, titleIdOf } from './art.js';
import { preloadArt } from './rate.svelte.js';

afterEach(() => {
  vi.unstubAllGlobals();
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
});
