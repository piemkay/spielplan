/**
 * @vitest-environment jsdom
 *
 * Decision 35's chip in the member register (decision 486): "undo rating", not "undo verdict" or
 * "undo not_seen". The journal's own kind stays on `data-undo-kind`, which the browser specs read.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import RateUndo from './RateUndo.svelte';

let target;
let app;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
});

function chip(undo) {
  app = mount(RateUndo, { target, props: { undo, busy: false, onUndo: () => {} } });
  flushSync();
  return target.querySelector('[data-testid="rate-undo"]');
}

describe('the undo chip (decision 486)', () => {
  for (const [kind, words] of [
    ['verdict', 'rating'],
    ['not_seen', 'not seen'],
    ['correction', 'not seen'],
    ['duel', 'pick'],
    ['tie', 'tie'],
    ['skip', 'skip']
  ]) {
    it(`says "undo ${words}" for a ${kind}, and keeps the raw kind on the attribute`, () => {
      const el = chip({ available: true, kind, reason: null });
      expect(el.textContent.trim()).toBe(`undo ${words}`);
      expect(el.getAttribute('aria-label')).toBe(`Undo the last ${words}`);
      expect(el.getAttribute('data-undo-kind')).toBe(kind);
      unmount(app);
      app = null;
    });
  }

  it('says only "undo" while disabled, with the reason beside it', () => {
    const el = chip({ available: false, kind: null, reason: 'empty' });
    expect(el.textContent.trim()).toBe('undo');
    expect(el.disabled).toBe(true);
    expect(target.querySelector('[data-testid="rate-undo-reason"]').textContent).toBe(
      'nothing to undo in this block'
    );
  });
});
