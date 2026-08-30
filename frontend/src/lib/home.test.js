import { describe, expect, it } from 'vitest';

import {
  bannerHref,
  bannerLabel,
  bannerText,
  countLabel,
  eventTime,
  facetColour,
  gridReason,
  hasModelAnnotations,
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

  it('never merges the two arrays', () => {
    // The falsifier for an interleaved ranking: each row must still hold exactly its own
    // section's items, and the two item arrays must not be the same object or a concatenation.
    const rows = shelfRows(payload);
    expect(rows[0].section.items).toHaveLength(1);
    expect(rows[1].section.items).toHaveLength(1);
    expect(rows[0].section.items[0].title_id).toBe(1);
    expect(rows[1].section.items[0].title_id).toBe(2);
    expect(rows[0].section.items).not.toBe(rows[1].section.items);
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
      route: '/rate?mode=sweep&head=1123&head=1023'
    }
  };

  it('follows the server link verbatim when it carries every named title', () => {
    expect(bannerHref(banner)).toBe('/rate?mode=sweep&head=1123&head=1023');
  });

  it('refuses a bare /rate — naming titles then serving another card is the failure', () => {
    expect(bannerHref({ ...banner, cta: { route: '/rate' } })).toBeNull();
  });

  it('refuses a link that drops one of the named titles', () => {
    expect(bannerHref({ ...banner, cta: { route: '/rate?mode=sweep&head=1123' } })).toBeNull();
  });

  it('refuses a comma-joined head, which GET /api/rate answers with a 422', () => {
    expect(bannerHref({ ...banner, cta: { route: '/rate?mode=sweep&head=1123,1023' } })).toBeNull();
  });

  it('refuses a link whose head is in a different order than the copy named', () => {
    expect(
      bannerHref({ ...banner, cta: { route: '/rate?mode=sweep&head=1023&head=1123' } })
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
      seen_state: 'seen'
    });
  });

  it('maps an unseen card to the string the catalog card expects, not to false', () => {
    expect(toPosterTitle({ title_id: 1, seen: false }).seen_state).toBe('unseen');
  });
});

describe('decision 117: the gate is the payload, not a local boolean', () => {
  it('reads the annotations as present only when the server sent the rail', () => {
    expect(hasModelAnnotations({ rail: [] })).toBe(true);
    expect(hasModelAnnotations({ shelves: [] })).toBe(false);
    expect(hasModelAnnotations(null)).toBe(false);
  });
});

describe('the data voice (§6.8)', () => {
  it('gives every vocabulary-v1 facet its own colour token', () => {
    for (const facet of [
      'mood', 'themes', 'pacing', 'structure', 'visual', 'sound',
      'character', 'place', 'era', 'sensibility', 'register'
    ]) {
      expect(facetColour(facet)).toBe(`var(--facet-${facet})`);
    }
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
