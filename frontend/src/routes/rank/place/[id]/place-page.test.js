/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$app/navigation', () => ({ afterNavigate: vi.fn(), pushState: vi.fn() }));

import PlacePage from './+page.svelte';
import { place } from '$lib/place.svelte.js';

const zodiac = { id: 2, kind: 'movie', name: 'Zodiac', year: 2007, runtime_min: 157 };
const sicario = { id: 3, kind: 'movie', name: 'Sicario', year: 2015, runtime_min: 121 };

const pair = {
  kind: 'movie',
  token: 't1',
  tier: 'A',
  left: { ...sicario, outcome: 'A' },
  right: { ...zodiac, outcome: 'B' },
  progress: { low: 58, high: 88, size: 150, asked: 3, estimate: 8 }
};

let target;
let app;
let replies;

beforeEach(() => {
  replies = [pair];
  vi.stubGlobal(
    'fetch',
    vi.fn((url) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: async () =>
          JSON.stringify(url.includes('/api/titles/') ? { title: {}, credits: [] } : replies.shift())
      })
    )
  );
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function open() {
  app = mount(PlacePage, { target, props: { params: { id: '3' } } });
  for (let i = 0; i < 5; i++) await tick();
  flushSync();
}

const $ = (id) => /** @type {HTMLElement} */ (document.querySelector(`[data-testid="${id}"]`));

describe('Place with questions', () => {
  it('asks under a narrowing bar, with Not seen under the neighbour alone', async () => {
    await open();
    expect(document.querySelector('h1').textContent).toBe('Place Sicario');
    expect($('rank-place-where').textContent).toBe('Somewhere between #58 and #88 of 150 in A');
    expect(target.textContent).toContain('Question 4 of about 8');
    expect($('rate-correction-right').getAttribute('aria-label')).toBe('Not seen: Zodiac');
    expect($('rate-correction-left')).toBeNull();
  });

  it('shows the placed title without a Not seen, and the neighbour with one', async () => {
    await open();
    $('rate-battle-left').click();
    flushSync();
    expect($('rate-peek')).not.toBeNull();
    expect($('rate-peek-not-seen')).toBeNull();
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    expect($('rate-peek')).toBeNull();
    $('rate-battle-right').click();
    flushSync();
    expect($('rate-peek-not-seen').getAttribute('aria-label')).toBe('Not seen: Zodiac');
  });

  it('ends on the new spot, ringed among its neighbours', async () => {
    replies = [
      {
        done: true,
        kind: 'movie',
        title_id: 3,
        tier: 'A',
        above: zodiac,
        below: null,
        asked: 8,
        around: [zodiac, sicario]
      }
    ];
    await open();
    expect($('rank-place-done').textContent).toBe('Sicario sits in A');
    expect(target.textContent).toContain('At the bottom of A, below Zodiac — 8 questions');
    expect(target.querySelector('[aria-current="true"]').textContent).toContain('New spot');
    expect($('rank-place-finish').getAttribute('href')).toBe('/rank');
    expect(place.pair).toBeNull();
  });
});
