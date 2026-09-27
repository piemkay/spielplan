/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import RateBattleCard from './RateBattleCard.svelte';

const LEFT = '[data-testid="rate-battle-left"]';

/** One battle card, in the shape `rate/session.payload` sends. */
const pair = (token, names = ['Heat', 'Drive']) => ({
  type: 'battle',
  token,
  kind: 'movie',
  left: { id: 1, name: names[0], year: 1995, runtime_min: 170, outcome: 'A' },
  right: { id: 2, name: names[1], year: 2011, runtime_min: 100, outcome: 'B' },
  reason: 'queued because: both of these you rated liked',
  substituted_for: null,
  corrections: { label: 'not seen', sides: ['left', 'both', 'right'] }
});

let duels = [];
let target;

beforeEach(() => {
  vi.useFakeTimers();
  duels = [];
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  vi.useRealTimers();
  target.remove();
});

function open(token = 't1') {
  const props = $state({
    card: pair(token),
    decisive: false,
    busy: false,
    onDuel: (outcome, opts = {}) => duels.push({ outcome, ...opts }),
    onCorrect: () => {},
    onDecisive: () => {},
    onWhy: () => {}
  });
  const app = mount(RateBattleCard, { target, props });
  flushSync();
  return { props, app };
}

// jsdom has no PointerEvent, and the listener is by name.
function pressLeft() {
  const el = target.querySelector(LEFT);
  expect(el, 'the left poster is not on screen at all').not.toBeNull();
  el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
}

describe("proposal 51's long press", () => {
  it('writes the decisive duel against the card the finger landed on', () => {
    const { app } = open('t1');
    try {
      pressLeft();
      vi.advanceTimersByTime(500);
      expect(duels).toEqual([{ outcome: 'A', decisive: true, token: 't1' }]);
    } finally {
      unmount(app);
    }
  });

  it('writes nothing when that card has gone by the time the press fires', () => {
    const { props, app } = open('t1');
    try {
      pressLeft();
      // The swap, mid-press: an Undo, the banner's `?head=` effect, or the model-gate effect.
      props.card = pair('t2', ['Sicario', 'Prisoners']);
      flushSync();
      vi.advanceTimersByTime(500);
      expect(duels, 'the press answered the pair that replaced the pressed one').toEqual([]);
    } finally {
      unmount(app);
    }
  });

  it('unarms the timer when the card goes away under it', () => {
    const { app } = open('t1');
    pressLeft();
    unmount(app);
    vi.advanceTimersByTime(500);
    expect(duels, 'an unmounted card still answered').toEqual([]);
  });

  it('still answers an ordinary tap, and only once', () => {
    // The click that follows a fired press must not answer twice.
    const { app } = open('t1');
    try {
      const el = target.querySelector(LEFT);
      el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
      el.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
      el.click();
      expect(duels).toEqual([{ outcome: 'A' }]);

      duels = [];
      el.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
      vi.advanceTimersByTime(500);
      el.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
      el.click();
      expect(duels).toEqual([{ outcome: 'A', decisive: true, token: 't1' }]);
    } finally {
      unmount(app);
    }
  });
});

describe('the pair card (decisions 520 and 527)', () => {
  it('makes the posters the answers and names each title in the corrections zone', () => {
    const { app } = open('t1');
    try {
      const left = target.querySelector(LEFT);
      expect(left.getAttribute('aria-label')).toBe('Pick Heat');
      expect(left.querySelector('button, a')).toBeNull();
      expect(target.querySelector('[data-testid="rate-strip-tie"]').textContent).toBe(
        'About the same'
      );
      const corrections = [...target.querySelectorAll('[data-testid^="rate-correction-"]')];
      expect(corrections.map((el) => el.textContent)).toEqual(['Heat', 'Drive', 'Neither']);
      expect(corrections.map((el) => el.getAttribute('data-testid'))).toEqual([
        'rate-correction-left',
        'rate-correction-right',
        'rate-correction-both'
      ]);
      expect(target.querySelector('[data-testid="rate-corrections"]').textContent).toContain(
        "Haven't seen one?"
      );
    } finally {
      unmount(app);
    }
  });

  it('shows the clear-favourite switch as a switch, with its line that it resets', () => {
    const { props, app } = open('t1');
    try {
      const toggle = target.querySelector('[data-testid="rate-decisive"]');
      expect(toggle.getAttribute('role')).toBe('switch');
      expect(toggle.getAttribute('aria-checked')).toBe('false');
      expect(document.getElementById(toggle.getAttribute('aria-labelledby')).textContent).toBe(
        'Clear favourite'
      );
      expect(target.querySelector('[data-testid="rate-decisive-why"]').textContent).toBe(
        'Turn this on when one is clearly better - that answer counts for more. It resets for ' +
          'the next pair.'
      );
      props.decisive = true;
      flushSync();
      expect(toggle.getAttribute('aria-checked')).toBe('true');
    } finally {
      unmount(app);
    }
  });
});
