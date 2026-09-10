/**
 * @vitest-environment jsdom
 *
 * The long press, against a card that moves under it. Spec v2.1 §6.1, §5.2, §4.2; proposal 51;
 * M4.10 finding 28.
 *
 * §6.1 keeps the long press as "an optional accelerator only", and proposal 51 defines it as
 * "equivalent to toggle-on plus tap". Both sentences assume the tap and the write are the same
 * event. They are not: the write happens 500 ms after the finger lands, and `load()` replaces the
 * card inside that window on three routine triggers — the `?head=` effect, the model-gate effect
 * and Undo. The timer closed over the outcome alone and `rate.svelte.js` read the live token at
 * fire time, so the gesture answered whichever pair had arrived.
 *
 * HERE RATHER THAN IN PLAYWRIGHT. The window is 500 ms wide and the swap has to land inside it
 * with the finger still down; a browser test would have to race a real timer while holding a
 * pointer, which is how a suite gets a flake instead of a proof. Mounted, the timer is vitest's
 * and the swap is a prop assignment, so the frame is exact and the assertion cannot flake.
 * `rate.svelte.test.js` keeps the other half — that `duel()` refuses a pressed token the table has
 * moved past, which is the same defect arriving after this check has already passed.
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
  substituted_for: null
});

/** Every `onDuel` the card made, in order — the writes, as the page would have posted them. */
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

/** Mount one card with reactive props, the way `rate/+page.svelte` mounts it. */
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

/** `pointerdown` as a MouseEvent: jsdom has no PointerEvent, and the listener is by name. */
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
      // §4.2 is append-only and §5.2 weighs this row ~1.6 against ~1.0, so a duel written here
      // is both the heaviest observation in the Ledger and one the person never made.
      props.card = pair('t2', ['Sicario', 'Prisoners']);
      flushSync();
      vi.advanceTimersByTime(500);
      expect(duels, 'the press answered the pair that replaced the pressed one').toEqual([]);
    } finally {
      unmount(app);
    }
  });

  it('unarms the timer when the card goes away under it', () => {
    // No `onDestroy` at all before this: a mode switch or a navigation left the timer armed and
    // it fired into a component nobody was looking at.
    const { app } = open('t1');
    pressLeft();
    unmount(app);
    vi.advanceTimersByTime(500);
    expect(duels, 'an unmounted card still answered').toEqual([]);
  });

  it('still answers an ordinary tap, and only once', () => {
    // The accelerator must not become less reachable, and the click that follows a fired press
    // must not answer twice — proposal 51's "equivalent to toggle-on plus tap", not both.
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
