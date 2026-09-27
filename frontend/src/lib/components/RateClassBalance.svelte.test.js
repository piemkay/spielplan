/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import RateClassBalance from './RateClassBalance.svelte';

const balance = (over = {}) => ({
  counts: [0, 0, 1],
  shares: [0, 0, 1],
  labels: ['disliked', 'fine', 'liked'],
  total: 1,
  warn: false,
  copy: null,
  threshold: 0.6,
  arms_at: 15,
  ...over
});

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

function open(props, compact = false) {
  app = mount(RateClassBalance, { target, props: { balance: props, compact } });
  flushSync();
}

describe('the balance widget (decisions 486 and 491)', () => {
  it('counts ratings from the first one and says when the check begins', () => {
    open(balance());
    expect(target.querySelector('[data-testid="rate-balance-total"]').textContent).toBe(
      '1 rating'
    );
    expect(target.querySelector('[data-testid="rate-balance-arming"]').textContent.trim()).toBe(
      'A balance check starts at 15 ratings.'
    );
    expect(target.querySelector('[data-testid="rate-balance-warning"]')).toBeNull();
    expect(target.textContent).not.toMatch(/labels|CLASS BALANCE|running distribution/);
  });

  it('stops saying when the check begins once it has armed, and shows no threshold', () => {
    // The warning itself is the page's to place, verbatim (see rate-page.test.js).
    open(balance({ counts: [2, 3, 12], shares: [2 / 17, 3 / 17, 12 / 17], total: 17, warn: true }));
    expect(target.querySelector('[data-testid="rate-balance"]').getAttribute('data-warn')).toBe(
      'true'
    );
    expect(target.querySelector('[data-testid="rate-balance-threshold"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-balance-arming"]')).toBeNull();
    expect(target.textContent).not.toMatch(/60%|warn >/);
    const rows = [...target.querySelectorAll('[data-testid^="rate-balance-count-"]')];
    expect(rows.map((li) => li.textContent.replace(/\s+/g, ' ').trim())).toEqual([
      'Liked 12',
      'Fine 3',
      'Disliked 2'
    ]);
  });

  it('says the mix in words on the small bar, where there is no room for the counts', () => {
    open(balance({ counts: [2, 3, 12], total: 17 }), true);
    expect(target.querySelector('[data-testid="rate-mix"]').getAttribute('aria-label')).toBe(
      'Your mix: 2 disliked, 3 fine, 12 liked'
    );
    expect(target.querySelector('[data-testid="rate-balance"]')).toBeNull();
    expect(target.querySelector('[data-testid^="rate-balance-segment-"]')).toBeNull();
  });
});
