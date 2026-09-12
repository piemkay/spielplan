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
 * whichever the imported bundle happens to produce. Decision 274 leaves this layer unregistered
 * in `spec_coverage.toml` — `npm --prefix e2e run fresh` does not run vitest.
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

  it('appears once when a card on the row was placed by the Cold Tower', () => {
    const app = render([card(), card({ title_id: 2, name: 'Tampopo', e_source: 'cold_tower' })]);
    const notes = target.querySelectorAll(COLD_NOTE);
    expect(notes).toHaveLength(1);
    expect(notes[0].textContent).toContain('Cold Tower');
    // The register, not the data voice: §6.8 gives the mono face to model numbers and ids, and
    // this is a sentence about them. The class is what carries that, so it is asserted here.
    expect(notes[0].className).toContain('why');
    unmount(app);
  });

  it('says it once for the row and not once per card', () => {
    const app = render([
      card({ title_id: 1, e_source: 'cold_tower' }),
      card({ title_id: 2, e_source: 'cold_tower' }),
      card({ title_id: 3, e_source: 'cold_tower' })
    ]);
    expect(target.querySelectorAll(COLD_NOTE)).toHaveLength(1);
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
