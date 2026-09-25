/**
 * @vitest-environment jsdom
 *
 * The poster on every 2:3 card. Spec v2.1 §6.8 ("Poster-forward 2:3 cards"), §6.1; decision 483.
 *
 * Mounted, because each claim is about the element a phone receives: an `<img>` whose src is this
 * app's own poster route for THIS title, drawn over the tinted panel, removed when the route has
 * nothing (so the panel is what stays, never a broken image), and inert to the finger so §6.1's
 * long press on a battle poster is not taken by the image callout. The two cards own the image;
 * every other surface renders one of them, and the title card and the finish prompt are asserted
 * here as two of those surfaces.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), post: vi.fn() }));

import { get } from '$lib/api.js';
import FinishPrompt from './FinishPrompt.svelte';
import PosterCard from './PosterCard.svelte';
import RatePoster from './RatePoster.svelte';
import TitleDetail from './TitleDetail.svelte';

let target;
let app;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockReset();
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

describe('RatePoster', () => {
  it('draws the same-origin poster over the tinted panel, eagerly and without alt text', () => {
    app = mount(RatePoster, { target, props: { title: { id: 949, name: 'Heat' } } });
    flushSync();
    const poster = target.querySelector('[data-testid="rate-poster"]');
    const img = poster.querySelector('img');
    expect(img.getAttribute('src')).toBe('/api/art/949/poster');
    expect(img.getAttribute('alt')).toBe('');
    expect(img.getAttribute('loading')).toBe('eager');
    expect(poster.getAttribute('style')).toContain('linear-gradient');
    expect(poster.textContent).toContain('Heat');
  });

  it('takes a Tonight payload keyed title_id, and names the title it draws', () => {
    app = mount(RatePoster, { target, props: { title: { title_id: 12, name: 'Prisoners' } } });
    flushSync();
    const poster = target.querySelector('[data-testid="rate-poster"]');
    expect(poster.querySelector('img').getAttribute('src')).toBe('/api/art/12/poster');
    expect(poster.getAttribute('data-title-id')).toBe('12');
  });

  it('drops the image on error and keeps the tinted panel, then tries the next title again', () => {
    const props = $state({ title: { id: 1, name: 'Tampopo' } });
    app = mount(RatePoster, { target, props });
    flushSync();
    target.querySelector('img').dispatchEvent(new Event('error'));
    flushSync();
    expect(target.querySelector('img')).toBeNull();
    expect(target.querySelector('[data-testid="rate-poster"]').getAttribute('style')).toContain(
      'linear-gradient'
    );

    props.title = { id: 2, name: 'Heat' };
    flushSync();
    expect(target.querySelector('img').getAttribute('src')).toBe('/api/art/2/poster');
  });

  // A browser keeps an <img>'s old picture on screen until the new src has loaded, so a reused
  // element put the next battle's names over the last pair's art for the whole fetch.
  it("draws a fresh image for the next title, so the last title's art never sits under its name", () => {
    const props = $state({ title: { id: 3, name: 'Paddington 2' } });
    app = mount(RatePoster, { target, props });
    flushSync();
    const first = target.querySelector('img');
    props.title = { id: 2, name: 'Prisoners' };
    flushSync();
    const next = target.querySelector('img');
    expect(next.getAttribute('src')).toBe('/api/art/2/poster');
    expect(next).not.toBe(first);
    expect(first.isConnected).toBe(false);
  });

  it('draws no image for a card with no title to ask about', () => {
    app = mount(RatePoster, { target, props: { title: null } });
    flushSync();
    expect(target.querySelector('img')).toBeNull();
  });
});

describe('PosterCard', () => {
  it('draws the poster lazily under its chips, and drops it on error', () => {
    app = mount(PosterCard, {
      target,
      props: {
        title: { id: 949, name: 'Heat', year: 1995, seen_state: 'seen', e_source: 'warm' },
        onSelect: () => {}
      }
    });
    flushSync();
    const img = target.querySelector('.poster img');
    expect(img.getAttribute('src')).toBe('/api/art/949/poster');
    expect(img.getAttribute('loading')).toBe('lazy');
    expect(img.nextElementSibling?.textContent).toBe('seen');

    img.dispatchEvent(new Event('error'));
    flushSync();
    expect(target.querySelector('.poster img')).toBeNull();
    expect(target.querySelector('.poster').getAttribute('style')).toContain('linear-gradient');
  });
});

describe('the surfaces that render a poster', () => {
  it("puts the title's poster on the title card", async () => {
    vi.mocked(get).mockResolvedValue({
      title: { id: 6, name: 'Severance', kind: 'series', year: 2022, seen_state: 'unseen' },
      model_line: { available: false, reason: 'no bundle' },
      credits: [],
      platform_ratings: { items: [], note: 'display-only' },
      dna: { extracted: [], projected: [] },
      actions: { play_on_jellyfin: null, show_on_map: { title_id: 6 } }
    });
    app = mount(TitleDetail, {
      target,
      props: { titleId: 6, onClose: () => {}, onPerson: () => {}, onStateChange: () => {} }
    });
    await settle();
    const poster = target.querySelector('[data-testid="rate-poster"]');
    expect(poster.querySelector('img').getAttribute('src')).toBe('/api/art/6/poster');
  });

  it('keys the finish prompt poster on the title and not on the prompt row', async () => {
    vi.mocked(get).mockResolvedValue([
      { id: 7001, title_id: 12, name: 'Prisoners', progress: 0.96 }
    ]);
    app = mount(FinishPrompt, { target });
    await settle();
    const img = target.querySelector('[data-finish-prompt] img');
    expect(img.getAttribute('src')).toBe('/api/art/12/poster');
  });
});
