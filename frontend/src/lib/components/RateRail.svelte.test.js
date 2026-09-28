/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import RateRail from './RateRail.svelte';

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

function open(props) {
  app = mount(RateRail, { target, props });
  flushSync();
}

describe('the rail (decisions 486 and 491)', () => {
  it('speaks in ratings and names no section, with the switch off', () => {
    open({ balance: { total: 12 }, mode: 'mix', showModel: false });
    const text = target.textContent;
    expect(target.querySelector('[data-testid="rate-label-count"]').textContent).toContain(
      '12 ratings'
    );
    expect(text).toContain('Sharpen my ranking');
    expect(text).not.toMatch(/§|\blabels?\b|1\.6|1\.0/);
    expect(target.querySelector('[data-testid="rate-margin-weights"]')).toBeNull();
  });

  it('shows the margin weights beside their names once Show the model is on', () => {
    open({ balance: { total: 12 }, mode: 'battle', showModel: true });
    expect(target.querySelector('[data-testid="rate-margin-weights"]').textContent).toBe(
      'much more 1.6 · more 1.0 · same 1.0'
    );
  });
});
