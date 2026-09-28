/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), qs: vi.fn(() => '') }));

import ShelfRow from './ShelfRow.svelte';

const COLD_NOTE = '[data-testid="shelf-cold-note"]';

/** One shelf card in `GET /api/home`'s shape; warm by default, since the badge is the exception. */
const card = (overrides = {}) => ({
  title_id: 1,
  kind: 'movie',
  name: 'Paddington',
  year: 2014,
  runtime_min: 95,
  poster_path: null,
  placement: 'warm',
  item_n: 480,
  e_source: 'backbone',
  seen: false,
  rank: 1,
  tier: null,
  model: null,
  ...overrides
});

const section = (items) => ({
  kind: 'movie',
  title: 'Because you liked Paddington',
  heading: 'Films',
  why: 'warm comedies, gentle pacing',
  caption: null,
  items
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  target.remove();
});

function render(items) {
  // `onSelect` is a required prop, and omitting it fails `npm run check`.
  const app = mount(ShelfRow, {
    target,
    props: {
      section: section(items),
      shelfId: 'because-you',
      onSelect: () => {}
    }
  });
  flushSync();
  return app;
}

describe('the cold-placement note', () => {
  it('is absent from a row every card of which has crowd data behind it', () => {
    const app = render([card(), card({ title_id: 2, name: 'Arrival' })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    unmount(app);
  });

  it('appears once when a card on the row was placed with no crowd ratings behind it', () => {
    const app = render([
      card(),
      card({ title_id: 2, name: 'Tampopo', e_source: 'cold_tower', item_n: 0 })
    ]);
    const notes = target.querySelectorAll(COLD_NOTE);
    expect(notes).toHaveLength(1);
    expect(notes[0].textContent).toContain('no outside ratings yet');
    expect(notes[0].textContent).not.toContain('Cold Tower');
    expect(notes[0].className).toContain('footnote');
    unmount(app);
  });

  it('says it once for the row and not once per card', () => {
    const app = render([
      card({ title_id: 1, e_source: 'cold_tower', item_n: 0 }),
      card({ title_id: 2, e_source: 'cold_tower', item_n: 0 }),
      card({ title_id: 3, e_source: 'cold_tower', item_n: 0 })
    ]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(1);
    unmount(app);
  });

  it('is absent when the Cold Tower placed a title the crowd has rated', () => {
    // The evaluation holdout serves crowd-rated rows from the Cold Tower.
    const app = render([card({ e_source: 'cold_tower', placement: 'cold_tower', item_n: 192061 })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    expect(target.querySelector('[data-testid="new-badge"]')).toBeNull();
    unmount(app);
  });

  it('is not said a second time on the shelf whose why-line already says it', () => {
    const cold = [card({ title_id: 1, e_source: 'cold_tower', item_n: 0 })];
    const app = mount(ShelfRow, {
      target,
      props: { section: section(cold), shelfId: 'new_in_library', onSelect: () => {} }
    });
    flushSync();
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    expect(target.querySelector('[data-testid="new-badge"]').textContent).toBe('New');
    unmount(app);
  });

  it('reads the same fields the badge does, so the row and the card cannot disagree', () => {
    // `e_source` decides where present: a crowd-rated title can still be stamped `cold_tower`.
    const app = render([card({ placement: 'cold_tower', item_n: 55, e_source: 'backbone' })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    unmount(app);

    // And without `e_source` the placement stamp is the fallback, as it is on the card.
    const fallback = render([card({ placement: 'cold_tower', item_n: null, e_source: null })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(1);
    unmount(fallback);
  });
});

function renderSection(overrides) {
  const app = mount(ShelfRow, {
    target,
    props: { section: { ...section([card()]), ...overrides }, shelfId: 'x', onSelect: () => {} }
  });
  flushSync();
  return app;
}

describe('what a row says about itself', () => {
  it('is a heading and one reason line, with no chips of terms, kinds or counts', () => {
    const app = renderSection({
      why_terms: [{ term: 'era.wwii', facet: 'era', tier: 'extracted', label: 'World War II' }]
    });
    const header = target.querySelector('header');
    expect([...header.children].map((el) => el.dataset.testid)).toEqual(['shelf-title', 'shelf-why']);
    expect(target.textContent).not.toContain('World War II');
    expect(target.textContent).not.toContain('FILMS');
    unmount(app);
  });

  // Tier letters live on Rank (decision 527).
  it('prints no rank number and no tier letter on a card, whatever the payload carries', () => {
    const app = render([
      card({ title_id: 1, name: 'Heat', tier: 'S', rank: 1 }),
      card({ title_id: 2, name: 'Up', tier: 'B', rank: 2, seen: true })
    ]);
    const cards = [...target.querySelectorAll('[data-testid="shelf-card"]')];
    expect(cards.map((c) => c.querySelector('.meta').textContent.replace(/\s+/g, ' ').trim())).toEqual([
      'Heat 2014 · 1h 35m',
      'Up 2014 · 1h 35m'
    ]);
    for (const c of cards) expect(c.textContent).not.toMatch(/\b(S|B)\b/);
    expect(cards[1].querySelector('[data-testid="seen-badge"]')).not.toBeNull();
    unmount(app);
  });

  it('prints the model numbers only when the payload carries them', () => {
    const off = renderSection({});
    expect(target.querySelector('[data-testid="shelf-numbers"]')).toBeNull();
    unmount(off);
    const on = renderSection({ why_numbers: { beta: 0.62, gate_k: 10, beta_fitted: true } });
    expect(target.querySelector('[data-testid="shelf-numbers"]').textContent).toBe(
      'β 0.62 · gate k 10'
    );
    unmount(on);
  });
});
