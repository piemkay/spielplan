/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', async (importOriginal) => ({ ...(await importOriginal()), get: vi.fn() }));

import { get } from '$lib/api.js';
import { homeFilters, resetHomeFilters } from '$lib/homeFilters.svelte.js';
import { hideToast, toast } from '$lib/toast.svelte.js';
import TwistRow from './TwistRow.svelte';

const t = (label) => ({ term: `x.${label}`, label, facet: 'mood' });
const SHINING = {
  title_id: 694, name: 'The Shining', year: 1980, group: 'mood', group_name: 'Mood', colour: '#c8613a',
  terms: [t('claustrophobic'), t('harrowing'), t('atmospheric')], library_n: 10
};
const MAD_MAX = {
  title_id: 76341, name: 'Mad Max: Fury Road', year: 2015, group: 'pace', group_name: 'Pace', colour: '#8b6bd6',
  terms: [t('relentless')], library_n: 13
};

let target;
let app;

beforeEach(() => {
  homeFilters.like = ['245'];
  vi.mocked(get).mockReset();
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  hideToast();
  resetHomeFilters();
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open(answer) {
  vi.mocked(get).mockImplementation(async (path) => answer(new URL(path, 'http://localhost/api/').searchParams));
  app = mount(TwistRow, { target, props: { kind: 'movie', query: { like: ['245'], genre: 'Crime' } } });
  await settle();
}

const twists = () => [...target.querySelectorAll('[data-testid="twist"]')];

describe('Try a twist (decision 560 item 7)', () => {
  it('is absent when the server offers none', async () => {
    await open(() => ({ seed: 0, twists: [] }));
    expect(target.querySelector('[data-testid="twist-row"]')).toBeNull();
  });

  it('names each twist with its count and terms, from the recipe and its filters', async () => {
    await open(() => ({ seed: 0, twists: [SHINING, MAD_MAX] }));
    const asked = new URL(vi.mocked(get).mock.calls[0][0], 'http://localhost/api/');
    expect(asked.pathname).toBe('/mix/twists');
    expect(Object.fromEntries(asked.searchParams)).toMatchObject({ like: '245', genre: 'Crime', kind: 'movie', seed: '0' });
    expect(twists()[0].getAttribute('aria-label')).toBe(
      'Add Mood like The Shining, 10 films in your library fit: claustrophobic, harrowing, atmospheric'
    );
    expect(twists()[0].textContent.replace(/\s+/g, ' ').trim()).toBe(
      'Mood like The Shining 10 fit · claustrophobic, harrowing, atmospheric'
    );
  });

  it('asks for the next set on each shuffle', async () => {
    await open((q) => ({ seed: Number(q.get('seed')), twists: q.get('seed') === '0' ? [SHINING] : [MAD_MAX] }));
    const shuffle = target.querySelector('button[aria-label^="Other twists"]');
    shuffle.click();
    await settle();
    shuffle.click();
    await settle();
    expect(vi.mocked(get).mock.calls.map(([p]) => new URL(p, 'http://localhost/api/').searchParams.get('seed'))).toEqual([
      '0', '1', '2'
    ]);
    expect(twists()[0].textContent).toContain('Pace like Mad Max: Fury Road');
  });

  it('adds a twist with an Undo toast that puts the recipe back', async () => {
    await open(() => ({ seed: 0, twists: [SHINING] }));
    twists()[0].click();
    flushSync();
    expect(homeFilters.like).toEqual(['245', '694:mood']);
    expect(toast.message).toBe('Added Mood like The Shining');
    expect(toast.actionLabel).toBe('Undo');
    toast.action();
    expect(homeFilters.like).toEqual(['245']);
  });
});
