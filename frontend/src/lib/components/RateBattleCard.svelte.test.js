/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import RateBattleCard from './RateBattleCard.svelte';

/** One battle card, in the shape `rate/session.payload` sends. */
const pair = (token = 't1') => ({
  type: 'battle',
  token,
  kind: 'movie',
  left: { id: 1, name: 'Heat', year: 1995, runtime_min: 170, outcome: 'A' },
  right: { id: 2, name: 'Drive', year: 2011, runtime_min: 100, outcome: 'B' },
  reason: 'You rated both liked',
  substituted_for: null,
  corrections: { label: 'not seen', sides: ['left', 'both', 'right'] }
});

let calls = [];
let target;
let app;

beforeEach(() => {
  calls = [];
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  target.remove();
});

function open(over = {}) {
  const props = $state({
    card: pair(),
    busy: false,
    pending: null,
    onDuel: (outcome, much) => calls.push(['duel', outcome, much]),
    onCorrect: (side) => calls.push(['correct', side]),
    onPeek: (side) => calls.push(['peek', side]),
    onWhy: () => calls.push(['why']),
    ...over
  });
  app = mount(RateBattleCard, { target, props });
  flushSync();
  return props;
}

const q = (sel) => target.querySelector(sel);

describe('the pair card (decision 528)', () => {
  it('opens "About this film" from a poster, and never answers with it', () => {
    open();
    const left = q('[data-testid="rate-battle-left"]');
    expect(left.getAttribute('aria-label')).toBe('About Heat');
    left.click();
    q('[data-testid="rate-battle-right"]').click();
    expect(calls).toEqual([
      ['peek', 'left'],
      ['peek', 'right']
    ]);
  });

  it('answers with five steps, the outer two counting for more, and says which film each means', () => {
    open();
    const steps = [...target.querySelectorAll('.scale button')];
    expect(steps.map((b) => b.textContent.trim())).toEqual([
      'Much more',
      'More',
      'Same',
      'More',
      'Much more'
    ]);
    expect(steps.map((b) => b.getAttribute('aria-label'))).toEqual([
      'Heat: much more',
      'Heat: more',
      'About the same',
      'Drive: more',
      'Drive: much more'
    ]);
    for (const step of steps) step.click();
    expect(calls).toEqual([
      ['duel', 'A', true],
      ['duel', 'A', false],
      ['duel', 'TIE', false],
      ['duel', 'B', false],
      ['duel', 'B', true]
    ]);
  });

  it('puts Not seen under each film, named for it, and offers no both', () => {
    open();
    const pills = [...target.querySelectorAll('[data-testid^="rate-correction-"]')];
    expect(pills.map((el) => el.getAttribute('aria-label'))).toEqual([
      'Not seen: Heat',
      'Not seen: Drive'
    ]);
    pills[1].click();
    expect(calls).toEqual([['correct', 'right']]);
    expect(target.textContent).not.toMatch(/Haven't seen one|Neither|Clear favourite/);
  });

  it('lights the step in flight, rings its film and dims the other', () => {
    open({ busy: true, pending: 'duel-B-much' });
    expect(q('[data-testid="rate-duel-B-much"]').classList.contains('picked')).toBe(true);
    expect(q('[data-testid="rate-duel-B"]').classList.contains('picked')).toBe(false);
    expect(q('[data-testid="rate-battle-right"]').classList.contains('ringed')).toBe(true);
    expect(q('[data-testid="rate-battle-left"]').classList.contains('dimmed')).toBe(true);
  });

  it('says why these two on one line, "Why these?" beside it', () => {
    open();
    expect(q('.sub').textContent.replace(/\s+/g, ' ').trim()).toBe('You rated both liked · Why these?');
    q('[data-testid="rate-why"]').click();
    expect(calls).toEqual([['why']]);
  });
});
