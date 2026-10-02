/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const VOCAB = vi.hoisted(() => {
  const term = (id, label, owned, aliases = []) => ({ term: id, facet: id.split('.')[0], label, gloss: '', aliases, owned });
  return {
    version: 'v1',
    facets: [{ facet: 'mood', colour: '#c8613a' }, { facet: 'themes', colour: '#3f7f6f' }],
    terms: [
      term('mood.tense', 'tense', 328),
      term('mood.dark', 'dark', 230),
      term('mood.bleak', 'bleak', 198),
      term('mood.cozy', 'cozy & mellow', 51, ['cosy', 'cozy']),
      term('themes.heist', 'heist', 40)
    ]
  };
});
vi.mock('$lib/api.js', () => ({
  get: vi.fn(async (path) => (path.startsWith('/vocabulary') ? VOCAB : null)),
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

import { session } from '$lib/session.svelte.js';
import TermPicker from './TermPicker.svelte';

const COZY = { term: 'mood.cozy', label: 'cozy & mellow', facet: 'mood' };

let target;
let anchor;
let app;

beforeEach(() => {
  target = document.createElement('div');
  anchor = document.createElement('div');
  document.body.append(anchor, target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
  anchor.remove();
  session.user = null;
  history.replaceState(null, '');
});

/** @param {{width?: number, [prop: string]: any}} [options] */
async function open({ width = 390, ...props } = {}) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
  const all = {
    open: true, kinds: ['movie'], chosen: [], anchor,
    onInclude: vi.fn(), onLeaveOut: vi.fn(), onRemove: vi.fn(), onClose: vi.fn(),
    ...props
  };
  app = mount(TermPicker, { target, props: all });
  for (let i = 0; i < 4; i++) await Promise.resolve();
  flushSync();
  return all;
}

function type(text) {
  const field = target.querySelector('[data-testid="term-search"]');
  field.value = text;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

const rows = () => [...target.querySelectorAll('[data-testid="term-row"]')];
const labelOf = (row) => row.querySelector('.label').textContent;

describe('the term picker on a phone', () => {
  it('is a sheet that browses each facet in the vocabulary order, three terms each until All', async () => {
    await open();
    expect(target.querySelector('[role="dialog"]').getAttribute('aria-modal')).toBe('true');
    expect(target.querySelector('.popover')).toBeNull();
    const sections = [...target.querySelectorAll('section')];
    expect(sections.map((s) => s.getAttribute('aria-label'))).toEqual(['Mood', 'Themes']);
    expect([...sections[0].querySelectorAll('.label')].map((l) => l.textContent)).toEqual(['tense', 'dark', 'bleak']);
    expect(sections[0].querySelector('.meta').textContent).toBe('328 films');

    const all = sections[0].querySelector('.all');
    expect(all.textContent).toBe('All 4 in Mood');
    all.click();
    flushSync();
    expect(target.querySelectorAll('section')[0].querySelectorAll('[data-testid="term-row"]')).toHaveLength(4);
  });

  it('includes or leaves out a term, and a pressed button takes it back off', async () => {
    const props = await open({ chosen: [{ id: 'mood.tense', label: 'tense', facet: 'mood', mode: 'in' }] });
    const [tense, dark] = rows();
    dark.querySelector('[aria-label="Include dark"]').click();
    expect(props.onInclude).toHaveBeenCalledWith({ term: 'mood.dark', label: 'dark', facet: 'mood' });
    dark.querySelector('[aria-label="Leave out dark"]').click();
    expect(props.onLeaveOut).toHaveBeenCalledWith({ term: 'mood.dark', label: 'dark', facet: 'mood' });

    const pressed = tense.querySelector('[aria-label="Include tense"]');
    expect(pressed.getAttribute('aria-pressed')).toBe('true');
    pressed.click();
    expect(props.onRemove).toHaveBeenCalledWith({ term: 'mood.tense', label: 'tense', facet: 'mood' });
    expect(props.onInclude).toHaveBeenCalledOnce();
  });

  it('keeps the chosen chips on top, includes first, and flips one through the callbacks', async () => {
    const props = await open({
      chosen: [
        { id: 'themes.heist', label: 'heist', facet: 'themes', mode: 'out' },
        { id: 'mood.cozy', label: 'cozy & mellow', facet: 'mood', mode: 'in' }
      ]
    });
    const chips = [...target.querySelectorAll('[data-testid="picker-term-chip"]')];
    expect(chips.map((c) => c.dataset.mode)).toEqual(['in', 'out']);
    chips[0].querySelector('[aria-label="Switch cozy & mellow to leave out"]').click();
    expect(props.onLeaveOut).toHaveBeenCalledWith(COZY);
    chips[1].querySelector('[aria-label="Switch heist to include"]').click();
    expect(props.onInclude).toHaveBeenCalledWith({ term: 'themes.heist', label: 'heist', facet: 'themes' });
    chips[1].querySelector('[aria-label="Remove heist"]').click();
    expect(props.onRemove).toHaveBeenCalledWith({ term: 'themes.heist', label: 'heist', facet: 'themes' });
  });

  it('ranks what is typed, naming the facet, the alias that found it and the count', async () => {
    await open();
    type('cosy');
    expect(rows().map(labelOf)).toEqual(['cozy & mellow']);
    expect(rows()[0].querySelector('.meta').textContent).toBe('Mood · via cosy · 51 films');
  });

  it('says when nothing matches, and offers the title search when it is given one', async () => {
    const onSearchTitles = vi.fn();
    await open({ onSearchTitles });
    type('noir');
    const none = target.querySelector('[data-testid="term-none"]');
    expect(none.querySelector('p').textContent).toBe('No taste term matches noir');
    none.querySelector('button').click();
    expect(onSearchTitles).toHaveBeenCalledWith('noir');
  });

  it('shows the raw id beside the label only while Show the model is on', async () => {
    session.user = /** @type {any} */ ({ id: 1, name: 'Pat', role: 'admin', must_change_password: false, show_model: true });
    await open();
    expect(rows()[0].querySelector('.rawid').textContent).toBe('mood.tense');
    session.user.show_model = false;
    flushSync();
    expect(rows()[0].querySelector('.rawid')).toBeNull();
    expect(labelOf(rows()[0])).toBe('tense');
  });
});

describe('the term picker on a desktop', () => {
  it('is a popover under its cell, with the facets beside the open one\'s terms', async () => {
    await open({ width: 1280 });
    expect(target.querySelector('.popover')).not.toBeNull();
    expect(target.querySelector('[aria-modal="true"]')).toBeNull();
    expect(document.activeElement).toBe(target.querySelector('[data-testid="term-search"]'));

    const facets = [...target.querySelectorAll('.facet')];
    expect(facets.map((f) => f.querySelector('.name').textContent)).toEqual(['Mood', 'Themes']);
    expect(facets[0].querySelector('.preview').textContent).toBe('tense, dark, bleak');
    expect(rows().map(labelOf)).toEqual(['tense', 'dark', 'bleak', 'cozy & mellow']);
    expect(rows()[0].querySelector('.n').textContent).toBe('328');

    facets[1].click();
    flushSync();
    expect(facets[1].getAttribute('aria-pressed')).toBe('true');
    expect(rows().map(labelOf)).toEqual(['heist']);
  });

  it('is a sheet without a cell to hang from', async () => {
    await open({ width: 1280, anchor: null });
    expect(target.querySelector('.popover')).toBeNull();
    expect(target.querySelector('[aria-modal="true"]')).not.toBeNull();
  });
});
