/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const FOUND = vi.hoisted(() => [
  { id: 245, kind: 'movie', name: 'Knives Out', year: 2019, is_owned: true },
  { id: 1396, kind: 'series', name: 'Broadchurch', year: 2013, is_owned: false },
  { id: 275, kind: 'movie', name: 'Fargo', year: 1996, is_owned: false }
]);
vi.mock('$lib/api.js', () => ({
  get: vi.fn(async () => ({ items: FOUND })),
  qs: (params) => `?${new URLSearchParams(params)}`
}));

const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state })),
  beforeNavigate: () => {}
}));

import { get } from '$lib/api.js';
import { homeFilters, resetHomeFilters } from '$lib/homeFilters.svelte.js';
import { FULL, remember } from '$lib/recipe.svelte.js';
import FilmPicker from './FilmPicker.svelte';
import LikeFilmsCell from './LikeFilmsCell.svelte';

let target;
let app;

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(get).mockClear();
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  vi.useRealTimers();
  resetHomeFilters();
  target.remove();
});

function open({ width = 1280, ...props } = {}) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
  app = mount(FilmPicker, { target, props });
  flushSync();
}

const field = () => /** @type {HTMLInputElement} */ (target.querySelector('[data-testid="film-search"]'));

async function type(text) {
  field().focus();
  field().value = text;
  field().dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  await vi.advanceTimersByTimeAsync(220);
  flushSync();
}

const key = (k, shiftKey = false) => {
  field().dispatchEvent(new KeyboardEvent('keydown', { key: k, shiftKey, bubbles: true }));
  flushSync();
};
const rows = () => [...target.querySelectorAll('[data-testid="film-row"]')];
const press = (rowIndex, name) => {
  [...rows()[rowIndex].querySelectorAll('button')].find((b) => b.textContent === name).click();
  flushSync();
};

describe('the film picker on a desktop', () => {
  it('asks as the typing pauses and lists titles of either kind, owned or not', async () => {
    open({ inline: true });
    await type('kn');
    expect(vi.mocked(get).mock.calls.at(-1)[0]).toBe('/mix/films?q=kn&limit=8');
    expect(target.querySelector('[role="dialog"]').getAttribute('aria-label')).toBe('Films to like or less like');
    expect(rows().map((r) => r.querySelector('.meta').textContent)).toEqual([
      '2019 · In your library', '2013 · Series · Not in the library', '1996 · Not in the library'
    ]);
  });

  it('likes the highlighted film on Enter, and makes the next one less like on Shift+Enter', async () => {
    open({ inline: true });
    await type('o');
    key('Enter');
    key('ArrowDown');
    key('Enter', true);
    expect(homeFilters.like).toEqual(['245']);
    expect(homeFilters.less).toEqual(['1396']);
    expect(rows()[1].classList.contains('active')).toBe(true);
  });

  it('takes a film back out when its pressed Like is pressed again', async () => {
    open({ inline: true });
    await type('o');
    press(0, 'Like');
    expect(rows()[0].querySelector('[aria-pressed="true"]').textContent).toBe('Like');
    press(0, 'Like');
    expect(homeFilters.like).toEqual([]);
  });

  it('refuses a fifth film, saying why', async () => {
    for (const id of [1, 2, 3, 4]) remember({ id, name: `Film ${id}` });
    homeFilters.like = ['1', '2', '3', '4'];
    open({ inline: true });
    await type('o');
    expect(target.querySelector('[data-testid="film-full"]').textContent).toBe(FULL);
    expect(rows()[0].querySelector('button').disabled).toBe(true);
    key('Enter');
    expect(homeFilters.like).toEqual(['1', '2', '3', '4']);
  });
});

describe('the film picker on a phone', () => {
  it('is a sheet whose chips switch a film between like and less like', async () => {
    remember({ id: 245, name: 'Knives Out' });
    homeFilters.like = ['245'];
    open({ width: 390, open: true, onClose: () => {} });
    expect(target.querySelector('[role="dialog"]').getAttribute('aria-label')).toBe('Like these films');
    const chip = target.querySelector('[data-testid="recipe-chip"] .body');
    expect(chip.getAttribute('aria-label')).toBe('Like Knives Out. Switch to less like');
    chip.click();
    flushSync();
    expect(homeFilters.less).toEqual(['245']);
    await type('far');
    expect(rows()).toHaveLength(3);
  });
});

describe("Like these films' cell", () => {
  function cell(width) {
    Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
    app = mount(LikeFilmsCell, { target });
    flushSync();
    return target.querySelector('[data-testid="filter-like"]');
  }

  it('holds the field itself from 721 px, across the panel', () => {
    const el = cell(1024);
    expect(el.classList.contains('wide')).toBe(true);
    expect(el.querySelector('[data-testid="film-search"]').placeholder).toBe('Add a film');
  });

  it('is a row on a phone that opens the picker as a sheet', () => {
    const el = cell(390);
    expect(el.textContent.replace(/\s+/g, ' ').trim()).toBe('Like these films Add');
    expect(target.querySelector('[role="dialog"]')).toBeNull();
    el.click();
    flushSync();
    expect(target.querySelector('[role="dialog"]').getAttribute('aria-label')).toBe('Like these films');
  });
});
