/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', async (importOriginal) => ({ ...(await importOriginal()), get: vi.fn() }));

import { get } from '$lib/api.js';
import { homeFilters, resetHomeFilters } from '$lib/homeFilters.svelte.js';
import RecipeGrid from './RecipeGrid.svelte';

const term = (label, quoted = true) => ({ term: `x.${label}`, label, facet: 'mood', quoted });
const card = (id, over = {}) => ({
  id, kind: 'movie', name: `Film ${id}`, year: 2001, runtime_min: 100, is_owned: true, seen_state: 'unseen',
  why: [{ title_id: 245, name: 'Knives Out', groups: [], like: true, terms: [term('murder mystery')] }],
  ...over
});

/** One `GET /api/mix/titles` answer, in the shape `home/mix_table.py` sends. */
function page(over = {}, recipe = {}) {
  return {
    kind: 'movie', pool: 'library', sort: 'match', for_you_available: true, total: 2, library_total: 2,
    beyond_total: 0, strong_total: null, limit: 60, offset: 0,
    recipe: {
      asks_for_like: false,
      ingredients: [{ title_id: 245, kind: 'movie', name: 'Knives Out', year: 2019, like: true, groups: [], terms: [], sheet: [] }],
      more: [term('murder mystery'), term('grand estate')], less: [], ...recipe
    },
    items: [card(1), card(2)],
    ...over
  };
}

let target;
let app;
let answers;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  homeFilters.like = ['245'];
  answers = () => page();
  vi.mocked(get).mockReset();
  vi.mocked(get).mockImplementation(async (path) => {
    const url = new URL(path, 'http://localhost/api/');
    if (url.pathname.endsWith('/mix/twists')) return { seed: 0, twists: [] };
    return answers(url.searchParams);
  });
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  resetHomeFilters();
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open(props = {}) {
  app = mount(RecipeGrid, { target, props: { kinds: ['movie'], params: { q: '', owned: 'only' }, ...props } });
  await settle();
}

const reads = () =>
  vi.mocked(get).mock.calls.map(([p]) => new URL(p, 'http://localhost/api/')).filter((u) => u.pathname.endsWith('/mix/titles'));
const byTestId = (id) => target.querySelector(`[data-testid="${id}"]`);
const text = (id) => byTestId(id)?.textContent.replace(/\s+/g, ' ').trim();

describe("a recipe's reads", () => {
  it('asks once per kind, films first, with the recipe and the filters but no owned scope', async () => {
    homeFilters.less = ['11'];
    await open({ kinds: ['movie', 'series'], params: { q: 'night', genre: 'Crime', term: ['mood.dread'], owned: 'only' } });
    const [films, series] = reads();
    expect(films.searchParams.get('kind')).toBe('movie');
    expect(series.searchParams.get('kind')).toBe('series');
    expect(films.searchParams.getAll('like')).toEqual(['245']);
    expect(films.searchParams.getAll('less')).toEqual(['11']);
    expect(films.searchParams.get('q')).toBe('night');
    expect(films.searchParams.getAll('term')).toEqual(['mood.dread']);
    expect(films.searchParams.has('owned')).toBe(false);
    expect(films.searchParams.has('pool')).toBe(false);
    expect([...target.querySelectorAll('.kindhead')].map((h) => h.textContent)).toEqual(['Films', 'Series']);
  });

  it('waits for a typed search to pause before it asks again', async () => {
    vi.useFakeTimers();
    try {
      app = mount(RecipeGrid, {
        target,
        props: {
          kinds: ['movie'],
          get params() {
            return { q: homeFilters.q, owned: 'only' };
          }
        }
      });
      await settle();
      for (const q of ['g', 'gl']) {
        homeFilters.q = q;
        flushSync();
        await settle();
      }
      expect(reads()).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(220);
      await settle();
      expect(reads().map((u) => u.searchParams.get('q'))).toEqual([null, 'gl']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('marks itself as the grid of a recipe', async () => {
    await open();
    expect(byTestId('home-mode').dataset.reason).toBe('recipe');
  });
});

describe("the recipe's head", () => {
  it('reads "more: ... · less: ..." and the hint while every film is whole', async () => {
    answers = () => page({}, { less: [term('pulp', false)] });
    await open();
    expect(text('recipe-derived')).toBe('more: murder mystery, grand estate · less: pulp');
    expect(target.querySelector('.hint').textContent).toBe(
      'Tap a film to take one or more parts, like its mood or look.'
    );
  });

  it('says what replace did once a group is lent, in place of both', async () => {
    homeFilters.like = ['245', '275:mood'];
    answers = () => page({}, {
      ingredients: [
        { title_id: 245, name: 'Knives Out', like: true, groups: [], terms: [], sheet: [] },
        { title_id: 275, name: 'Fargo', like: true, groups: ['mood'], terms: [], sheet: [] }
      ]
    });
    await open();
    expect(text('recipe-sentence')).toBe("Knives Out, with Fargo's mood in place of its own");
    expect(byTestId('recipe-derived')).toBeNull();
    expect(target.querySelector('.hint')).toBeNull();
  });

  it('asks for a film to like when nothing is liked', async () => {
    homeFilters.like = [];
    homeFilters.less = ['11'];
    answers = () => page({ total: 0, library_total: 0, items: [] }, { asks_for_like: true });
    await open();
    expect(text('recipe-asks')).toContain('Like a film to start');
    expect(byTestId('recipe-count')).toBeNull();
  });
});

describe('the count', () => {
  it('counts the library, and says when fewer than ten fit', async () => {
    answers = () => page({ total: 37, library_total: 37 });
    await open();
    expect(text('recipe-count')).toBe('37 in your library');
    unmount(app);
    answers = () => page({ total: 4, library_total: 4 });
    await open();
    expect(text('recipe-count')).toBe('Only 4 films in your library fit');
    unmount(app);
    answers = () => page({ total: 0, library_total: 0, items: [] });
    await open();
    expect(text('recipe-count')).toBe('No films in your library fit');
    expect(byTestId('recipe-empty')).not.toBeNull();
  });
});

describe('the films beyond the library (decision 559 item 6)', () => {
  it('wait behind one fold while Only in library is on, read only when opened', async () => {
    answers = (q) => (q.get('pool') === 'beyond' ? page({ pool: 'beyond', total: 12, items: [card(90, { is_owned: false })] }) : page({ beyond_total: 12 }));
    await open();
    expect(reads().some((u) => u.searchParams.get('pool') === 'beyond')).toBe(false);
    const fold = byTestId('recipe-beyond-open');
    expect(fold.textContent.trim()).toBe('Show 12 more beyond the library');
    fold.click();
    await settle();
    expect(reads().at(-1).searchParams.get('pool')).toBe('beyond');
    expect(text('recipe-beyond-head')).toBe('Beyond the library');
    expect(byTestId('recipe-beyond').querySelectorAll('.card-wrap')).toHaveLength(1);
  });

  it('follow the library with no fold while Only in library is off', async () => {
    answers = (q) => (q.get('pool') === 'beyond' ? page({ pool: 'beyond', total: 1, items: [card(90, { is_owned: false })] }) : page({ beyond_total: 1 }));
    await open({ params: { q: '', owned: 'any' } });
    expect(byTestId('recipe-beyond-open')).toBeNull();
    expect(byTestId('recipe-beyond').querySelectorAll('.card-wrap')).toHaveLength(1);
  });
});

describe('the grid', () => {
  it("folds a capped director's films into one cell that opens in place", async () => {
    answers = () => page({
      total: 4, library_total: 4,
      items: [card(1), { fold: { person_ids: [301, 302], name: 'Joel Coen & Ethan Coen', items: [card(3, { name: 'Fargo' }), card(4, { name: 'Blood Simple' })] } }, card(2)]
    });
    await open();
    const cell = byTestId('recipe-fold');
    expect(cell.getAttribute('aria-label')).toBe('Show 2 more by Joel Coen & Ethan Coen: Fargo, Blood Simple');
    expect(cell.textContent.replace(/\s+/g, ' ').trim()).toBe('+2 more by Joel Coen & Ethan Coen');
    cell.click();
    flushSync();
    expect(byTestId('recipe-fold')).toBeNull();
    expect([...target.querySelectorAll('.card-wrap .name')].map((n) => n.textContent)).toEqual([
      'Film 1', 'Fargo', 'Blood Simple', 'Film 2'
    ]);
  });

  it('keeps an include\'s looser matches behind "Show N more that might fit"', async () => {
    answers = () => page({ total: 3, library_total: 3, strong_total: 1, items: [card(1, { match: 'strong' }), card(2, { match: 'weak' }), card(3, { match: 'weak' })] });
    await open();
    expect(target.querySelectorAll('.card-wrap')).toHaveLength(1);
    byTestId('recipe-weak').click();
    flushSync();
    expect(text('recipe-weak-head')).toBe('Might also fit');
    expect(target.querySelectorAll('.card-wrap')).toHaveLength(3);
  });

  it('captions each poster and hands the card its why line', async () => {
    const onSelect = vi.fn();
    await open({ onSelect });
    expect(target.querySelector('[data-testid="recipe-caption"]').textContent.trim()).toBe('From Knives Out: murder mystery');
    target.querySelector('.card-wrap').click();
    expect(onSelect.mock.calls[0][0]).toMatchObject({ id: 1, whyLine: 'From Knives Out: murder mystery' });
  });

  it('is Best match first, with For you one tap away as the server echoes it', async () => {
    await open();
    expect(byTestId('recipe-sort-match').getAttribute('aria-pressed')).toBe('true');
    answers = () => page({ sort: 'for_you' });
    byTestId('recipe-sort-for_you').click();
    await settle();
    expect(reads().at(-1).searchParams.get('sort')).toBe('for_you');
    expect(byTestId('recipe-sort-for_you').getAttribute('aria-pressed')).toBe('true');
  });
});
