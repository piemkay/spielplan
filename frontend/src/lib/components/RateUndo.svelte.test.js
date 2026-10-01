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
  app = mount(RateUndo, { target, props: { undo, onUndo: () => {} } });
  flushSync();
  return target.querySelector('[data-testid="rate-undo"]');
}

describe('the round Undo (decision 550)', () => {
  it('names the film a placement put on the ladder, and keeps the raw kind on the attribute', () => {
    const el = chip({ available: true, kind: 'placement', name: 'Heat' });
    expect(el.disabled).toBe(false);
    expect(el.getAttribute('aria-label')).toBe('Undo placing Heat');
    expect(el.getAttribute('data-undo-kind')).toBe('placement');
  });

  it('says Undo not seen for a Not seen', () => {
    const el = chip({ available: true, kind: 'not_seen', name: 'Heat' });
    expect(el.getAttribute('aria-label')).toBe('Undo not seen');
    expect(el.getAttribute('data-undo-kind')).toBe('not_seen');
  });

  it('stays on screen, dimmed, with nothing to take back', () => {
    const el = chip({ available: false, kind: null, name: null });
    expect(el.disabled).toBe(true);
    expect(el.getAttribute('aria-label')).toBe('Undo');
    expect(el.getAttribute('data-undo-kind')).toBe('');
  });
});
