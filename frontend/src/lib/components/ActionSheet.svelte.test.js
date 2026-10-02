/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The sheet is a history entry: `pushState` adds it, and Back takes it away a task later, as a
// browser's popstate does (`pop` below).
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

import ActionSheet from './ActionSheet.svelte';

let target;
let app;
let backs;

const sheets = () => get(nav.page).state.sheets ?? [];

beforeEach(() => {
  backs = 0;
  nav.page.update((p) => ({ ...p, state: {} }));
  vi.spyOn(history, 'back').mockImplementation(() => (backs += 1));
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  vi.restoreAllMocks();
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function pop() {
  nav.page.update((p) => ({ ...p, state: { ...p.state, sheets: sheets().slice(0, -1) } }));
  await settle();
}

async function render(onSelect) {
  const seen = [];
  const props = $state({
    open: true,
    title: 'Leave the set-up?',
    options: [{ label: 'Leave the set-up', destructive: true, onSelect: () => onSelect(seen) }],
    onClose: () => {
      seen.push('closed');
      props.open = false;
    }
  });
  app = mount(ActionSheet, { target, props });
  await settle();
  return seen;
}

const button = (label) => [...target.querySelectorAll('button')].find((b) => b.textContent.trim() === label);

describe('an action sheet', () => {
  it("runs the choice only once the sheet's history entry is gone, before it reports closed", async () => {
    const seen = await render((log) => log.push(`chose with ${sheets().length} sheets open`));
    expect(sheets()).toHaveLength(1);

    button('Leave the set-up').click();
    await settle();
    expect(backs, "the sheet's own Back").toBe(1);
    expect(seen, 'nothing runs in the tap, where a navigation would be dropped').toEqual([]);

    await pop();
    expect(seen).toEqual(['chose with 0 sheets open', 'closed']);
    expect(target.querySelector('[role="dialog"]')).toBeNull();
  });

  it('runs no choice on Cancel', async () => {
    const choose = vi.fn();
    const seen = await render(choose);
    button('Cancel').click();
    await settle();
    await pop();
    expect(choose).not.toHaveBeenCalled();
    expect(seen).toEqual(['closed']);
  });
});
