/**
 * @vitest-environment jsdom
 *
 * The shelf's own one-line why for §8 stage 10's badge. Spec v2.1 §6.8, §6.0; M4.15 finding 10,
 * decision 278.
 *
 * The "new" chip on a poster explained itself through `title="placed by the Cold Tower — no crowd
 * data yet"`, and a `title=` is a hover tooltip: it does not exist on the form factor §6's
 * preamble makes primary. So on a phone — where §6.0 says a shelf that cannot say why it exists
 * does not ship — the one thing on the row that IS a model statement said nothing at all.
 *
 * The sentence is the shelf's rather than the card's, which is the whole content of these cases:
 * one line for a row of twelve, present when a cold card is on the row and absent when none is.
 * Twelve copies of it would be the noise the quiet-reason register exists to avoid.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT because the condition is a property of the payload: a shelf
 * with a cold card on it and a shelf without one are two responses, and the e2e stack has
 * whichever the imported bundle happens to produce.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), qs: vi.fn(() => '') }));

import ShelfRow from './ShelfRow.svelte';

const COLD_NOTE = '[data-testid="shelf-cold-note"]';

/** One shelf card in the shape `GET /api/home` sends. Warm by default: a title with a Backbone
 *  row and 480 crowd ratings is the ordinary case, and the badge is the exception. */
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
  heading: 'FILMS',
  why: 'warm comedies, gentle pacing',
  caption: null,
  shared_terms: [],
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
  // `onSelect` is passed because it is declared without a default and is therefore a REQUIRED
  // prop: `npm --prefix frontend run check` is a CI job step from this milestone on (decision
  // 273), and a test that omits it costs the frontend gate an error of its own.
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
    // Decision 486's register: the fact, without the model's nouns.
    expect(notes[0].textContent).toContain('no outside ratings yet');
    expect(notes[0].textContent).not.toContain('Cold Tower');
    // The register, not the data voice: §6.8 gives the mono face to model numbers and ids, and
    // this is a sentence about them. The class is what carries that, so it is asserted here.
    expect(notes[0].className).toContain('why');
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
    // The bundle's evaluation holdout serves crowd-rated rows from the Cold Tower: Raiders of
    // the Lost Ark wore "new" over 192,061 ratings on the first household (§8 stage 10 names
    // the badge by the absence of crowd data).
    const app = render([card({ e_source: 'cold_tower', placement: 'cold_tower', item_n: 192061 })]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    expect(target.querySelector('.badge')).toBeNull();
    unmount(app);
  });

  it('is not said a second time on the shelf whose why-line already says it', () => {
    // "New in the library"'s why-line is §8 stage 10's sentence, and the row printed it twice
    // (second household test, U10). The chips stay on the cards.
    const cold = [card({ title_id: 1, e_source: 'cold_tower', item_n: 0 })];
    const app = mount(ShelfRow, {
      target,
      props: { section: section(cold), shelfId: 'new_in_library', onSelect: () => {} }
    });
    flushSync();
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(0);
    expect(target.querySelector('.badge').textContent).toBe('new');
    unmount(app);
  });

  it('reads the same fields the badge does, so the row and the card cannot disagree', () => {
    // `e_source` decides where the payload has it. A title with 55 crowd ratings and a Backbone
    // row is still stamped `placement: 'cold_tower'` — 15% of its coordinate comes from there —
    // and neither the chip nor this line may claim it has no crowd data. [M4.9 finding 18]
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
  it('states how many titles it holds, since a phone shows fewer than three', () => {
    const items = Array.from({ length: 12 }, (_, i) => card({ title_id: i + 1 }));
    const app = renderSection({ items });
    expect(target.querySelector('[data-testid="shelf-count"]').textContent.trim()).toBe(
      '12 titles'
    );
    unmount(app);
  });

  it('names a shared term by its label and never by its vocabulary id', () => {
    const app = renderSection({
      shared_terms: [
        { term: 'era.wwii', facet: 'era', tier: 'extracted', role: 'member', label: 'World War II' },
        { term: 'pacing.slow_burn', facet: 'pacing', tier: 'projected', role: 'member' }
      ]
    });
    const chips = [...target.querySelectorAll('[data-testid="shelf-term"]')].map((el) =>
      el.firstChild.textContent.trim()
    );
    expect(chips).toEqual(['World War II', 'slow burn']);
    expect(target.textContent).not.toContain('era.wwii');
    unmount(app);
  });

  it('says a tier letter on an unseen title is a guess, and one on the Rank board is not', () => {
    const app = render([
      card({ title_id: 1, tier: 'S', seen: false }),
      card({ title_id: 2, tier: 'B', seen: true, on_board: true, board_tier: 'B' })
    ]);
    const [guess, placed] = target.querySelectorAll('[data-testid="shelf-tier"]');
    expect(guess.getAttribute('aria-label')).toBe(
      "our guess: tier S if you rated it — you haven't seen it"
    );
    expect(guess.dataset.guess).toBe('true');
    expect(placed.getAttribute('aria-label')).toBe('tier B, as on your Rank board');
    expect(placed.dataset.guess).toBe('false');
    unmount(app);
  });

  // Decision 476's "as on your Rank board" is a quotation of Rank, so it is said only where Rank
  // shows this letter (decision 486 clause 7). A title marked watched and never rated is on no
  // board; a title moved on Rank shows the person's tier there, not the fit's.
  it('never quotes the Rank board for a title that is not on it, or at another letter there', () => {
    const app = render([
      card({ title_id: 1, tier: 'S', seen: true, on_board: false, board_tier: null }),
      card({ title_id: 2, tier: 'C', seen: true, on_board: true, board_tier: 'A' })
    ]);
    const [unrated, moved] = target.querySelectorAll('[data-testid="shelf-tier"]');
    expect(unrated.getAttribute('aria-label')).toBe('our guess: tier S if you rated it');
    expect(unrated.dataset.guess).toBe('true');
    expect(moved.getAttribute('aria-label')).toBe(
      'tier C, where your other answers point — you put it in A on your Rank board'
    );
    expect(moved.dataset.guess).toBe('false');
    expect(target.textContent).not.toContain('as on your Rank board');
    unmount(app);
  });

  it('draws the rank and the tier under the art, never over it', () => {
    // Pinned 30 px down the poster they covered Kiki's and Schindler's List's lettering (second
    // household test, U5).
    const app = render([card({ title_id: 1, tier: 'A', rank: 1 })]);
    const rank = target.querySelector('[data-testid="shelf-rank"]');
    const tier = target.querySelector('[data-testid="shelf-tier"]');
    for (const badge of [rank, tier]) {
      expect(badge.closest('.poster'), 'a badge is on the art').toBeNull();
      expect(badge.closest('.chrome'), 'a badge left the row under the art').not.toBeNull();
    }
    const cardWrap = target.querySelector('.card-wrap');
    const order = [...cardWrap.children].map((el) => el.className.split(' ')[0]);
    expect(order).toEqual(['poster', 'chrome', 'meta']);
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
