/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import TasteRow from './TasteRow.svelte';

const film = (id) => ({ id, name: `Film ${id}`, poster_path: null });
const ROW = {
  term: 'mood.dark',
  label: 'dark comedy',
  facet: 'mood',
  pos: 0.6,
  films: [1, 2, 3].map(film),
  more: 3
};
const MARKS = [{ pos: 0.6, initials: 'P', colour: null, who: 'You' }];

let target;
let row;

beforeEach(() => {
  // The track measures itself.
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (row) unmount(row);
  row = null;
  target.remove();
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
  flushSync();
}

function show(props) {
  row = mount(TasteRow, { target, props: { row: ROW, marks: MARKS, onOpen: () => {}, ...props } });
  flushSync();
}

describe('a Taste row', () => {
  it('reads its term in sentence case', () => {
    show();
    expect(target.querySelector('.label').textContent).toBe('Dark comedy');
  });

  it('opens in place onto every film behind the term, named, from the row or its "+N"', async () => {
    const expand = vi.fn(async () => [
      { head: 'These sit high for you', films: [1, 2, 3, 4].map(film) },
      { head: 'These land lower for you', films: [5, 6].map(film) }
    ]);
    const onOpen = vi.fn();
    show({ expand, onOpen });

    const toggle = target.querySelector('.toggle');
    toggle.click();
    await settle();

    expect(expand).toHaveBeenCalledWith('mood.dark');
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const panel = target.querySelector('[data-testid="taste-films"]');
    expect([...panel.querySelectorAll('.ghead')].map((h) => h.textContent)).toEqual([
      'PThese sit high for you',
      'PThese land lower for you'
    ]);
    expect([...panel.querySelectorAll('.names')].map((p) => p.textContent)).toEqual([
      'Film 1 · Film 2 · Film 3 · Film 4',
      'Film 5 · Film 6'
    ]);
    panel.querySelector('button[aria-label="About Film 6"]').click();
    expect(onOpen).toHaveBeenCalledWith(film(6));

    target.querySelector('.plus').click();
    flushSync();
    expect(target.querySelector('[data-testid="taste-films"]')).toBeNull();
    target.querySelector('.plus').click();
    await settle();
    expect(target.querySelector('[data-testid="taste-films"]')).not.toBeNull();
    expect(expand).toHaveBeenCalledTimes(1);
  });

  it('says so when no film behind the term is shared', async () => {
    show({ expand: async () => [], none: "None of the films you've both placed has this." });
    target.querySelector('.toggle').click();
    await settle();
    expect(target.querySelector('[data-testid="taste-films"]').textContent.trim()).toBe(
      "None of the films you've both placed has this."
    );
  });

  it('cannot be opened without the films behind it', () => {
    show({ row: { ...ROW, films: [], more: 0 } });
    expect(target.querySelector('.toggle')).toBeNull();
    expect(target.querySelector('.sr-only').textContent).toBe('Dark comedy. You: sits high.');
  });
});
