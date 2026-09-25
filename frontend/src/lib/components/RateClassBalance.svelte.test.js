/**
 * @vitest-environment jsdom
 *
 * §6.1's class-balance widget in the member register: decision 491's floor and copy, decision
 * 486's plain words. The sentence itself is the server's (`rate/balance.py`) and is rendered as
 * sent; what this pins is the frame around it.
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

function open(props) {
  app = mount(RateClassBalance, { target, props: { balance: props } });
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

  it('renders the warning as sent, and no threshold caption beside it', () => {
    const copy =
      "Heavy on 'liked'. Spreading your ratings across all three answers matters about five " +
      "times more than anything else you can do here. Rate some titles you didn't enjoy as " +
      'well - but never change an honest answer to even things out.';
    open(balance({ counts: [2, 3, 12], total: 17, warn: true, copy }));
    expect(target.querySelector('[data-testid="rate-balance-warning"]').textContent).toBe(copy);
    expect(target.querySelector('[data-testid="rate-balance-threshold"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-balance-arming"]')).toBeNull();
    expect(target.textContent).not.toMatch(/60%|warn >/);
  });
});
