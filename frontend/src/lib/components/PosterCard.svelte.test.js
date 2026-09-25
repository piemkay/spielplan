/**
 * @vitest-environment jsdom
 *
 * The two statuses a catalog card carries about the title itself: "new" (§8 stage 10) and "in
 * library". Spec v2.1 §6.0, §6.8; owner instruction of 2026-09-25 after the first household test.
 *
 * Mounted rather than in Playwright because both are properties of the payload, and the e2e
 * bundle has whichever mix it happens to ship. Unregistered by decision 274.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import PosterCard, { isColdPlaced } from './PosterCard.svelte';

const title = (overrides = {}) => ({
  id: 1,
  kind: 'movie',
  name: 'Heat',
  year: 1995,
  runtime_min: 170,
  placement: 'warm',
  item_n: 35000,
  e_source: 'backbone',
  seen_state: 'unseen',
  ...overrides
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  target.remove();
});

function render(t) {
  const app = mount(PosterCard, { target, props: { title: t, onSelect: () => {} } });
  flushSync();
  return app;
}

describe('the "new" badge', () => {
  it('is never worn by a title the crowd has rated, whatever placed it', () => {
    // Raiders of the Lost Ark: a cold-masked holdout row, served from the Cold Tower, with
    // 192,061 crowd ratings behind it. It wore "new" on the first household.
    expect(isColdPlaced(title({ e_source: 'cold_tower', placement: 'cold_tower', item_n: 192061 })))
      .toBe(false);
    expect(isColdPlaced(title({ e_source: 'cold_tower', item_n: 0 }))).toBe(true);
    expect(isColdPlaced(title({ e_source: null, placement: 'cold_tower', item_n: null }))).toBe(true);
  });

  it('explains itself in words a member can check', () => {
    const app = render(title({ e_source: 'cold_tower', item_n: 0 }));
    const badge = target.querySelector('.badge');
    expect(badge.textContent).toBe('new');
    expect(badge.getAttribute('title')).not.toContain('Cold Tower');
    unmount(app);
  });
});

describe('the "in library" chip', () => {
  it('marks a catalog card the household owns', () => {
    const app = render(title({ is_owned: true }));
    expect(target.querySelector('[data-testid="owned-chip"]').textContent).toBe('in library');
    unmount(app);
  });

  it('is absent from an unowned card and from a card that does not say', () => {
    // A shelf card never says: every shelf is owned-only, and a chip on each of its cards would
    // carry no information (`toPosterTitle` does not pass `is_owned`).
    for (const t of [title({ is_owned: false }), title()]) {
      const app = render(t);
      expect(target.querySelector('[data-testid="owned-chip"]')).toBeNull();
      unmount(app);
    }
  });
});
