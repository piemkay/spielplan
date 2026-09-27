/**
 * @vitest-environment jsdom
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

describe('the persistent Undo (decisions 35 and 486)', () => {
  for (const [kind, words] of [
    ['verdict', 'rating'],
    ['not_seen', 'not seen'],
    ['correction', 'not seen'],
    ['duel', 'pick'],
    ['tie', 'tie'],
    ['skip', 'skip']
  ]) {
    it(`names the ${words} it takes back, and keeps the raw kind on the attribute`, () => {
      const el = chip({ available: true, kind, reason: null });
      expect(el.textContent.trim()).toBe('Undo');
      expect(el.disabled).toBe(false);
      expect(el.getAttribute('aria-label')).toBe(`Undo the last ${words}`);
      expect(el.getAttribute('data-undo-kind')).toBe(kind);
      unmount(app);
      app = null;
    });
  }

  it('stays on screen while disabled, and says why', () => {
    const el = chip({ available: false, kind: null, reason: 'empty' });
    expect(el.textContent.trim()).toBe('Undo');
    expect(el.disabled).toBe(true);
    expect(el.getAttribute('aria-label')).toBe('Undo');
    const reason = target.querySelector('[data-testid="rate-undo-reason"]');
    expect(reason.textContent).toBe('Nothing to undo yet');
    expect(el.getAttribute('aria-describedby')).toBe(reason.id);
  });
});
