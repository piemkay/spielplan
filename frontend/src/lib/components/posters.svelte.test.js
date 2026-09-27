/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), post: vi.fn() }));
// The title card is a sheet, which pushes a history entry as it opens.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

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

  // A reused <img> keeps showing the old picture until the new src has loaded.
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
  it('draws the poster lazily under its badges, and drops it on error', () => {
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
    expect(img.nextElementSibling?.getAttribute('aria-label')).toBe('Seen');

    img.dispatchEvent(new Event('error'));
    flushSync();
    expect(target.querySelector('.poster img')).toBeNull();
    expect(target.querySelector('.poster').getAttribute('style')).toContain('linear-gradient');
  });
});

describe('a poster that failed on one card', () => {
  it('is not drawn as an <img> by the next card for the same title', () => {
    app = mount(PosterCard, {
      target,
      props: { title: { id: 4060, name: 'Moulin Rouge', year: 1952 }, onSelect: () => {} }
    });
    flushSync();
    target.querySelector('.poster img').dispatchEvent(new Event('error'));
    flushSync();
    unmount(app);

    app = mount(RatePoster, { target, props: { title: { title_id: 4060, name: 'Moulin Rouge' } } });
    flushSync();
    const poster = target.querySelector('[data-testid="rate-poster"]');
    expect(poster.querySelector('img')).toBeNull();
    expect(poster.getAttribute('style')).toContain('linear-gradient');
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
