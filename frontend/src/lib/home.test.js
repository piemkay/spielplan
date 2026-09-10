import { describe, expect, it } from 'vitest';

import {
  bannerHref,
  bannerLabel,
  bannerText,
  countLabel,
  eventTime,
  facetColour,
  gridReason,
  homeMode,
  kindsOnShelf,
  plural,
  sectionShips,
  shelfRows,
  toPosterTitle
} from './home.svelte.js';

/**
 * The falsifiers, not the happy paths. Each block below breaks one sentence of §6.0 and
 * checks the module refuses it.
 */

describe('the two-mode state machine (§6.0)', () => {
  it('shows shelves when nothing is filtering', () => {
    expect(homeMode({})).toBe('shelves');
    expect(gridReason({})).toBeNull();
  });

  it('switches to the grid on a search', () => {
    expect(gridReason({ q: 'dune' })).toBe('search');
    expect(homeMode({ q: 'dune' })).toBe('grid');
  });

  it('does not switch on whitespace, which is not a query', () => {
    expect(gridReason({ q: '   ' })).toBeNull();
  });

  it('switches to the grid on a person filter', () => {
    expect(gridReason({ personId: 5 })).toBe('person');
  });

  it('treats person id 0 as a real id', () => {
    // A falsy-but-valid id is the classic way a filmography filter silently stops working.
    expect(gridReason({ personId: 0 })).toBe('person');
  });

  it('returns to the shelves once the query and the chip are both gone', () => {
    // The half of §6.0's sentence nothing else in the spec supplies: how a user gets back out.
    expect(homeMode({ q: '', personId: null })).toBe('shelves');
  });

  it('counts a catalog filter as a grid reason, so no control is dead', () => {
    expect(gridReason({ genre: 'Drama' })).toBe('filter');
    expect(gridReason({ decade: '1990' })).toBe('filter');
    expect(gridReason({ seen: 'unseen' })).toBe('filter');
    expect(gridReason({ seen: 'any' })).toBeNull();
  });

  it('names search before person when both are set, so the copy is stable', () => {
    expect(gridReason({ q: 'x', personId: 5 })).toBe('search');
  });
});

describe('the count line (decision 18)', () => {
  it('names the kind when exactly one toggle is on', () => {
    expect(countLabel({ total: 6, kinds: ['movie'] })).toBe('6 films');
    expect(countLabel({ total: 1, kinds: ['movie'] })).toBe('1 film');
  });

  it('does not pluralise series, which has no plural', () => {
    expect(plural('series', 2)).toBe('series');
    expect(countLabel({ total: 2, kinds: ['series'] })).toBe('2 series');
  });

  it('says how many the other toggle holds — the whole point of the control', () => {
    expect(countLabel({ total: 6, hidden: { series: 2 }, kinds: ['movie'] })).toBe(
      '6 films · 2 series hidden'
    );
  });

  it('says "titles" when both kinds are on and reports nothing hidden', () => {
    expect(countLabel({ total: 8, hidden: {}, kinds: ['movie', 'series'] })).toBe('8 titles');
  });

  it('states the active filters (proposal 152)', () => {
    expect(countLabel({ total: 3, kinds: ['movie'], filters: ['genre Drama', '1990s'] })).toBe(
      '3 films · genre Drama · 1990s'
    );
  });
});

describe('a shelf that cannot say why it exists (§6.0 M2)', () => {
  const card = (id) => ({ title_id: id, name: `T${id}`, kind: 'movie', rank: 1, seen: false });

  it('drops a section whose why-line is empty', () => {
    expect(sectionShips({ why: '', items: [card(1), card(2)] })).toBe(false);
    expect(sectionShips({ why: '   ', items: [card(1)] })).toBe(false);
  });

  it('drops a section with no cards rather than rendering a bare heading', () => {
    expect(sectionShips({ why: 'for a school night', items: [] })).toBe(false);
  });

  it('keeps a section that has both', () => {
    expect(sectionShips({ why: 'for a school night', items: [card(1)] })).toBe(true);
  });

  it('yields nothing at all for a shelf whose every section fails', () => {
    const payload = {
      shelves: [{ id: 'school_night', ranking: true, sections: [{ kind: 'movie', why: '', items: [card(1)] }] }]
    };
    expect(shelfRows(payload)).toEqual([]);
  });

  it('survives a payload with no shelves key', () => {
    expect(shelfRows(null)).toEqual([]);
    expect(shelfRows({})).toEqual([]);
  });
});

describe('the kind partition (§4.1 rule 5, decision 18)', () => {
  const payload = {
    shelves: [
      {
        id: 'top_of_ledger',
        ranking: true,
        sections: [
          { kind: 'movie', heading: 'Films', why: 'β 0.62', items: [{ title_id: 1, rank: 1 }] },
          { kind: 'series', heading: 'Series', why: 'β 0.62', items: [{ title_id: 2, rank: 1 }] }
        ]
      }
    ]
  };

  it('renders one row per (shelf, kind) — two headed sections, never one list', () => {
    const rows = shelfRows(payload);
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.section.kind)).toEqual(['movie', 'series']);
    expect(rows.every((r) => r.shelf === 'top_of_ledger')).toBe(true);
  });

  it('never merges the two arrays, on a payload that hands both sections one array', () => {
    // The falsifier for an interleaved ranking, rebuilt. The last assertion used to be
    // `rows[0].section.items).not.toBe(rows[1].section.items)` against two *distinct* fixture
    // arrays, which no implementation could ever fail: the objects differ in the fixture, before
    // `shelfRows` is called. The case that discriminates is the one where the payload hands both
    // sections the same array — a merging `shelfRows` concatenates or pushes, and the length is
    // then the tell; one that flattened the shelf would return a single row instead of two.
    // [§4.1 rule 5, decision 18; M4.10 finding 33]
    const shared = [{ title_id: 1 }, { title_id: 2 }];
    const rows = shelfRows({
      shelves: [
        {
          id: 'top_of_ledger',
          ranking: true,
          sections: [
            { kind: 'movie', heading: 'Films', why: 'β 0.62', items: shared },
            { kind: 'series', heading: 'Series', why: 'β 0.62', items: shared }
          ]
        }
      ]
    });
    expect(rows, 'one row per (shelf, kind), never one interleaved list').toHaveLength(2);
    expect(rows.map((r) => r.section.kind)).toEqual(['movie', 'series']);
    expect(rows[0].section, 'each row keeps its own section').not.toBe(rows[1].section);
    expect(rows.map((r) => r.section.items.length), 'the rows grew').toEqual([2, 2]);
    expect(shared, "the payload's own array was mutated").toHaveLength(2);

    // And the ordinary payload, where each section brings its own: still exactly its own items.
    const split = shelfRows(payload);
    expect(split.map((r) => r.section.items.map((i) => i.title_id))).toEqual([[1], [2]]);
  });

  it('reports which kinds a shelf actually shipped', () => {
    expect(kindsOnShelf(payload.shelves[0])).toEqual(['movie', 'series']);
    expect(kindsOnShelf({ sections: [{ kind: 'movie', why: 'x', items: [] }] })).toEqual([]);
    expect(kindsOnShelf(undefined)).toEqual([]);
  });
});

describe('the pending-verdicts banner (proposals 21 and 150)', () => {
  const banner = {
    count: 6,
    named: [{ title_id: 1123, name: 'Patriot' }, { title_id: 1023, name: 'Hereditary' }],
    head_title_ids: [1123, 1023],
    copy: {
      wide: 'You watched Patriot, Hereditary and 4 more — a quick verdict keeps your profile sharp.',
      compact: 'Watched, not rated: Patriot, Hereditary and 4 more'
    },
    cta: {
      label_wide: 'Rate now',
      label_compact: 'Rate',
      // The link the server actually emits. `mode=sweep` led this query until decision 203
      // removed a parameter `GET /api/rate` never declared and this page never read, and a
      // fixture is where a stale spelling survives longest: the parser only reads `head`, so
      // nothing here would have failed.
      route: '/rate?head=1123&head=1023'
    }
  };

  it('follows the server link verbatim when it carries every named title', () => {
    expect(bannerHref(banner)).toBe('/rate?head=1123&head=1023');
  });

  it('refuses a bare /rate — naming titles then serving another card is the failure', () => {
    expect(bannerHref({ ...banner, cta: { route: '/rate' } })).toBeNull();
  });

  it('refuses a link that drops one of the named titles', () => {
    expect(bannerHref({ ...banner, cta: { route: '/rate?head=1123' } })).toBeNull();
  });

  it('refuses a comma-joined head, which GET /api/rate answers with a 422', () => {
    expect(bannerHref({ ...banner, cta: { route: '/rate?head=1123,1023' } })).toBeNull();
  });

  it('refuses a link whose head is in a different order than the copy named', () => {
    expect(
      bannerHref({ ...banner, cta: { route: '/rate?head=1023&head=1123' } })
    ).toBeNull();
  });

  it('has no link at all when there is no head', () => {
    expect(bannerHref({ ...banner, head_title_ids: [] })).toBeNull();
    expect(bannerHref(null)).toBeNull();
  });

  it('uses the two registers proposal 21 specifies rather than one sentence', () => {
    expect(bannerText(banner)).toMatch(/^You watched /);
    expect(bannerText(banner, { compact: true })).toMatch(/^Watched, not rated: /);
    expect(bannerLabel(banner)).toBe('Rate now');
    expect(bannerLabel(banner, { compact: true })).toBe('Rate');
    expect(bannerText(null)).toBe('');
  });
});

describe('the shelf card (proposal 29)', () => {
  it('renames the shelf payload into the shape the poster card reads', () => {
    const item = {
      title_id: 1012,
      kind: 'movie',
      name: 'Paddington',
      year: 2014,
      runtime_min: 95,
      poster_path: null,
      placement: 'warm',
      item_n: 480,
      e_source: 'backbone',
      seen: true,
      rank: 3,
      tier: 'A+'
    };
    expect(toPosterTitle(item)).toEqual({
      id: 1012,
      kind: 'movie',
      name: 'Paddington',
      year: 2014,
      runtime_min: 95,
      poster_path: null,
      placement: 'warm',
      item_n: 480,
      e_source: 'backbone',
      seen_state: 'seen'
    });
  });

  it('maps an unseen card to the string the catalog card expects, not to false', () => {
    expect(toPosterTitle({ title_id: 1, seen: false }).seen_state).toBe('unseen');
  });

  it('carries the two fields the no-crowd-data badge is decided on', () => {
    // §8 stage 10's badge is off `e_source`/`item_n`, not off `placement` — PosterCard's own
    // comment says so. The server moved both out of the decision-117 `model` block for exactly
    // this reason; a rename that drops them here puts the card straight back on the fallback,
    // where 111 of the 130 badges Home drew were false. [M4.9 finding 18]
    const warm = toPosterTitle({ title_id: 7, placement: 'cold_tower', item_n: 480, e_source: 'backbone' });
    expect(warm.e_source).toBe('backbone');
    expect(warm.item_n).toBe(480);
    const cold = toPosterTitle({ title_id: 8, placement: 'warm', item_n: 0, e_source: 'cold_tower' });
    expect(cold.e_source).toBe('cold_tower');
    expect(cold.item_n).toBe(0);
  });
});

describe('the data voice (§6.8)', () => {
  it('gives every vocabulary-v1 facet its own colour token', () => {
    for (const facet of [
      'mood', 'themes', 'pacing', 'structure', 'visual', 'sound',
      'characters', 'place', 'era', 'sensibility', 'register'
    ]) {
      expect(facetColour(facet)).toBe(`var(--facet-${facet})`);
    }
  });

  it('spells the fifth facet the way the shipped vocabulary does', () => {
    // The shipped file is `vocab_characters_v1.tsv` and every shipped term prefix is
    // `characters`, so the singular named a twelfth facet no row can carry and left the real one
    // at the neutral. The negative half is the point: seven sites agreed on `character` and the
    // static guard pinned it, so the palette was consistent and wrong. [M4.9 finding 4]
    expect(facetColour('characters')).toBe('var(--facet-characters)');
    expect(facetColour('character')).toBe('var(--ink-4)');
  });

  it('never lends the ember to an unknown facet', () => {
    expect(facetColour('vibes')).toBe('var(--ink-4)');
    expect(facetColour(undefined)).toBe('var(--ink-4)');
  });

  it('renders a timestamp, not "3 minutes ago"', () => {
    expect(eventTime('2026-08-30T13:13:39.432117+00:00')).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(eventTime('not a date')).toBe('');
  });
});
