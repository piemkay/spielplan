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
    onSkip: () => {},
    onDecisive: () => {}
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
