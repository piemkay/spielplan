/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const DENIS = vi.hoisted(() => ({
  person_ids: [77], person_id: 77, name: 'Denis Villeneuve', photo: true, role: 'director', owned: 6, titles: 11
}));
vi.mock('$lib/api.js', () => ({
  get: vi.fn(async () => ({ people: [DENIS] })),
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
import PeoplePicker from './PeoplePicker.svelte';

const CAINE = { person_ids: [12, 13], person_id: 12, name: 'Michael Caine', photo: false };

let target;
let app;

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(get).mockClear();
  target = document.createElement('div');
  document.body.append(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  vi.useRealTimers();
  target.remove();
  history.replaceState(null, '');
});

/** @param {{[prop: string]: any}} [props] */
function open(props = {}) {
  const all = { open: true, kinds: ['movie'], chosen: [], onAdd: vi.fn(), onRemove: vi.fn(), onClose: vi.fn(), ...props };
  app = mount(PeoplePicker, { target, props: all });
  flushSync();
  return all;
}

const field = () => /** @type {HTMLInputElement} */ (target.querySelector('[data-testid="people-search"]'));

function type(text) {
  field().value = text;
  field().dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

async function wait(ms) {
  await vi.advanceTimersByTimeAsync(ms);
  flushSync();
}

const rows = () => [...target.querySelectorAll('[data-testid="person-row"]')];

describe('the people picker', () => {
  it('asks once the typing pauses for 220 ms, and not under two letters', async () => {
    open();
    type('v');
    await wait(500);
    expect(get).not.toHaveBeenCalled();
    type('vi');
    await wait(200);
    type('vil');
    await wait(219);
    expect(get).not.toHaveBeenCalled();
    await wait(1);
    expect(get).toHaveBeenCalledOnce();
    expect(get).toHaveBeenCalledWith('/people?q=vil&kind=movie&limit=8');
  });

  it('lists each person with a headshot, their role and their count in the library, and adds one', async () => {
    const props = open();
    type('vill');
    await wait(220);
    const [row] = rows();
    expect(row.getAttribute('aria-label')).toBe('Add Denis Villeneuve');
    expect(row.querySelector('.face img').getAttribute('src')).toBe('/api/art/person/77');
    expect(row.querySelector('.meta').textContent).toBe('Director · 6 in your library');
    row.click();
    flushSync();
    expect(props.onAdd).toHaveBeenCalledWith(DENIS);
    expect(field().value).toBe('');
    expect(rows()).toHaveLength(0);
  });

  it('marks someone already chosen as added, and keeps the chosen on top with the combined line', async () => {
    const props = open({ chosen: [CAINE, DENIS], combinedLine: 'Films with both: 4 in your library' });
    const chips = [...target.querySelectorAll('[data-testid="picker-person-chip"]')];
    expect(chips.map((c) => c.querySelector('.label').textContent)).toEqual(['Michael Caine', 'Denis Villeneuve']);
    expect(chips[0].querySelector('.face').textContent.trim()).toBe('MC');
    expect(target.querySelector('[data-testid="people-combined"]').textContent).toBe('Films with both: 4 in your library');
    chips[0].querySelector('[aria-label="Remove Michael Caine"]').click();
    expect(props.onRemove).toHaveBeenCalledWith(CAINE);

    type('vill');
    await wait(220);
    const [row] = rows();
    expect(row.disabled).toBe(true);
    expect(row.querySelector('.add').textContent).toBe('Added');
    row.click();
    expect(props.onAdd).not.toHaveBeenCalled();
  });

  it('says when nobody matches', async () => {
    vi.mocked(get).mockResolvedValueOnce({ people: [] });
    open();
    type('zzq');
    await wait(220);
    expect(target.querySelector('[data-testid="people-none"]').textContent).toBe('No one matches zzq');
  });

  it('does not say nobody matches when the search fails', async () => {
    vi.mocked(get).mockRejectedValueOnce(new Error('offline'));
    open();
    type('vill');
    await wait(220);
    expect(target.querySelector('[data-testid="people-none"]')).toBeNull();
    expect(rows()).toHaveLength(0);
  });

  it('forgets a search still pending when it closes', async () => {
    const props = $state({ open: true, kinds: ['movie'], chosen: [], onAdd: vi.fn(), onRemove: vi.fn(), onClose: vi.fn() });
    app = mount(PeoplePicker, { target, props });
    flushSync();
    type('vill');
    await wait(100);
    props.open = false;
    flushSync();
    await wait(50);
    props.open = true;
    flushSync();
    await wait(300);
    expect(get).not.toHaveBeenCalled();
    expect(field().value).toBe('');
    expect(rows()).toHaveLength(0);
  });

  it('is a sheet without `inline`', () => {
    open();
    expect(target.querySelector('[aria-modal="true"]')).not.toBeNull();
    expect(target.querySelector('.popover')).toBeNull();
  });
});

describe('the people field on a desktop', () => {
  it("is the cell's field with its people as chips, and what is typed drops under it", async () => {
    const props = open({ inline: true, open: false, chosen: [CAINE] });
    const cell = target.querySelector('[data-testid="filter-people"]');
    expect(cell.contains(field())).toBe(true);
    expect(field().placeholder).toBe('Add a person');
    const chip = cell.querySelector('[data-testid="person-chip"]');
    chip.querySelector('[aria-label="Remove Michael Caine"]').click();
    expect(props.onRemove).toHaveBeenCalledWith(CAINE);

    field().focus();
    type('vill');
    expect(target.querySelector('.popover'), 'nothing to show before the answer').toBeNull();
    await wait(220);
    const popover = target.querySelector('.popover');
    expect(popover.getAttribute('aria-label')).toBe('People');
    expect(popover.querySelector('[data-testid="people-search"]'), 'no second field in the list').toBeNull();
    expect(rows()[0].classList.contains('active')).toBe(true);
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    flushSync();
    expect(props.onAdd).toHaveBeenCalledWith(DENIS);
    expect(field().value).toBe('');
    expect(target.querySelector('.popover')).toBeNull();
    expect(document.activeElement).toBe(field());
  });
});
