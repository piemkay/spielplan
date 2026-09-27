import { describe, expect, it } from 'vitest';

import {
  activeFilterCount,
  bannerCountLine,
  bannerLabel,
  bannerText,
  countLabel,
  elsewhereLine,
  eventTime,
  facetColour,
  gridLine,
  gridReason,
  homeMode,
  kindChoice,
  kindHeading,
  kindRegions,
  kindsFor,
  libraryLabel,
  matchStrength,
  otherKinds,
  partitionLine,
  plural,
  shelfRows,
  sortOffered,
  sortWaitingLine,
  strongEnd,
  toPosterTitle,
  whyNumbersLine
} from './home.svelte.js';

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
    expect(homeMode({ q: '', personId: null })).toBe('shelves');
  });

  it('counts a catalog filter as a grid reason, so no control is dead', () => {
    expect(gridReason({ genre: 'Drama' })).toBe('filter');
    expect(gridReason({ decade: '1990' })).toBe('filter');
    expect(gridReason({ seen: 'unseen' })).toBe('filter');
    expect(gridReason({ owned: true })).toBe('filter');
    expect(gridReason({ seen: 'any', owned: false })).toBeNull();
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

  it('says "1 title", not "1 titles", with both kinds on', () => {
    expect(countLabel({ total: 1, hidden: {}, kinds: ['movie', 'series'] })).toBe('1 title');
  });

  it('counts the household library over the shelves, not the whole catalog', () => {
    const library = { movie: 612, series: 262 };
    expect(libraryLabel({ library, kinds: ['movie'] })).toBe(
      '612 films in your library · 262 series hidden'
    );
    expect(libraryLabel({ library, kinds: ['movie', 'series'] })).toBe(
      '874 titles in your library'
    );
    expect(libraryLabel({ library: { series: 1 }, kinds: ['series'] })).toBe(
      '1 series in your library'
    );
  });

  it('states the active filters (proposal 152)', () => {
    expect(countLabel({ total: 3, kinds: ['movie'], filters: ['genre Drama', '1990s'] })).toBe(
      '3 films · genre Drama · 1990s'
    );
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
    // Both sections share one array: a merging `shelfRows` would grow it or return one row.
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

    const split = shelfRows(payload);
    expect(split.map((r) => r.section.items.map((i) => i.title_id))).toEqual([[1], [2]]);
  });

  it('survives a payload with no shelves key', () => {
    expect(shelfRows(null)).toEqual([]);
    expect(shelfRows({})).toEqual([]);
  });
});

describe('the pending-verdicts banner (proposal 21)', () => {
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
      route: '/rate?head=1123&head=1023'
    }
  };

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
    // The badge reads `e_source`/`item_n`, which live outside the gated `model` block.
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
    // The shipped vocabulary's prefix is plural: `characters`.
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

  it('names every shelf number it prints, and prints nothing without them (decision 486)', () => {
    expect(whyNumbersLine({ beta: 0.62, beta_optimum: 0.2, gate_k: 10 })).toBe(
      'β 0.62 · β optimum 0.20 · gate k 10'
    );
    expect(whyNumbersLine({ min_cdf: 0.7, partner_user_id: 3 })).toBe('cdf floor 0.70');
    expect(whyNumbersLine(undefined)).toBe('');
  });
});

describe('the kind switch (decision 474)', () => {
  it('switches rather than adds: Series alone is series alone', () => {
    expect(kindsFor('series')).toEqual(['series']);
    expect(kindsFor('movie')).toEqual(['movie']);
    expect(kindsFor('both')).toEqual(['movie', 'series']);
  });

  it('reads the position back from the selection, and no position is empty', () => {
    expect(kindChoice(['movie'])).toBe('movie');
    expect(kindChoice(['series'])).toBe('series');
    expect(kindChoice(['series', 'movie'])).toBe('both');
    // Decision 18's "never neither": an empty selection is read as Films, never as nothing.
    expect(kindChoice([])).toBe('movie');
    expect(kindsFor('nonsense')).toEqual(['movie']);
  });
});

describe('two kind regions under Both (decision 474)', () => {
  const card = (id, kind) => ({ title_id: id, name: `T${id}`, kind, rank: 1, seen: false });
  const heading = { movie: 'Films', series: 'Series' };
  const section = (kind, id) => ({
    kind, heading: heading[kind], why: 'for a school night', items: [card(id, kind)]
  });
  const payload = {
    kinds: ['movie', 'series'],
    shelves: [
      { id: 'top_of_ledger', ranking: true, sections: [section('series', 8)] },
      { id: 'school_night', ranking: true, sections: [section('movie', 2), section('series', 9)] }
    ]
  };

  it('keeps the table order inside each region, Films first', () => {
    const regions = kindRegions(payload);
    expect(regions.map((r) => [r.kind, r.heading])).toEqual([['movie', 'Films'], ['series', 'Series']]);
    expect(regions[0].rows.map((r) => r.shelf)).toEqual(['school_night']);
    expect(regions[1].rows.map((r) => r.shelf)).toEqual(['top_of_ledger', 'school_night']);
  });

  it('never puts the other kind into a region', () => {
    for (const region of kindRegions(payload)) {
      for (const row of region.rows) {
        expect(row.section.kind).toBe(region.kind);
        for (const item of row.section.items) expect(item.kind).toBe(region.kind);
      }
    }
  });

  it('drops a region with nothing to show and survives a payload without shelves', () => {
    const seriesOnly = { ...payload, shelves: [payload.shelves[0]] };
    expect(kindRegions(seriesOnly).map((r) => r.kind)).toEqual(['series']);
    expect(kindRegions(null)).toEqual([]);
  });
});

const REFERENCE = /§\s?\d|decision \d|proposal \d|\bM[0-7](\.\d+)?\b/i;

describe('the Filters control and the grid line', () => {
  it('counts the four catalog filters that are set, and not the person', () => {
    expect(activeFilterCount({})).toBe(0);
    expect(activeFilterCount({ genre: 'Drama', seen: 'unseen', owned: true })).toBe(3);
    expect(activeFilterCount({ seen: 'any', decade: '' })).toBe(0);
  });

  it('says in words what the grid is and how to get the shelves back', () => {
    for (const reason of ['search', 'person', 'filter']) {
      const line = gridLine(reason);
      expect(line).toMatch(/shelves back\.$/);
      expect(line).not.toMatch(REFERENCE);
      expect(line).not.toContain('·');
    }
  });

  it('offers the order control for a filtered or a person grid the server named an order for', () => {
    expect(sortOffered('filter', 'for_you')).toBe(true);
    expect(sortOffered('person', 'newest')).toBe(true);
    expect(sortOffered('search', 'for_you'), 'a search is best match first').toBe(false);
    expect(sortOffered('filter', undefined), 'no echo, no claim').toBe(false);
    expect(sortOffered('filter', 'year'), 'an order this control cannot name').toBe(false);
  });

  it('offers no For you where the server says the member has no order of their own yet', () => {
    // The server answers `newest` whatever is asked there.
    expect(sortOffered('filter', 'newest', false)).toBe(false);
    expect(sortOffered('filter', 'newest', true)).toBe(true);
    expect(sortOffered('filter', 'newest', null), 'a build that does not say').toBe(true);
    expect(sortWaitingLine('filter', 'newest', false)).toBe(
      'Newest first. Your own order arrives once your ratings rank these.'
    );
    expect(sortWaitingLine('person', 'newest', false)).not.toBe('');
    expect(sortWaitingLine('search', 'match', false), 'a search has no order to wait for').toBe('');
    expect(sortWaitingLine('filter', 'newest', true)).toBe('');
    expect(sortWaitingLine('filter', 'newest', null)).toBe('');
  });

  it("heads a grid in the member's order at each kind, and only that grid", () => {
    // The member's order is every film, then every series.
    const items = [
      { id: 1, kind: 'movie' },
      { id: 2, kind: 'movie' },
      { id: 3, kind: 'series' }
    ];
    expect(items.map((_, i) => kindHeading(items, i))).toEqual(['Films', '', 'Series']);
    expect(partitionLine(['movie', 'series'], 'for_you')).toBe('Films first, then series.');
    expect(partitionLine(['movie', 'series'], 'newest'), 'the year order interleaves').toBe('');
    expect(partitionLine(['movie', 'series'], 'match'), 'so does a search').toBe('');
    expect(partitionLine(['series'], 'for_you'), 'one kind, nothing to divide').toBe('');
  });
});

describe('an empty search and the other kind', () => {
  it('asks only the kinds the switch leaves out', () => {
    expect(otherKinds(['movie'])).toEqual(['series']);
    expect(otherKinds(['series'])).toEqual(['movie']);
    expect(otherKinds(['movie', 'series'])).toEqual([]);
  });

  it('names at most two and counts the rest', () => {
    expect(elsewhereLine('series', ['Broadchurch'], 1)).toBe('Found in Series: Broadchurch');
    expect(elsewhereLine('movie', ['Heat', 'Heatwave'], 5)).toBe(
      'Found in Films: Heat, Heatwave and 3 more'
    );
    expect(elsewhereLine('series', [], 0)).toBe('');
  });
});

describe('the looser matches of a search', () => {
  it('calls a name, or a word in it, that starts with the query strong', () => {
    for (const name of ['Up', 'Up in the Air', "What's Up, Doc?", 'Cheech and Chong’s Up in Smoke']) {
      expect(matchStrength({ name }, 'up'), name).toBe('strong');
    }
    for (const name of ['Superman', 'Cupid', 'Hiccups']) {
      expect(matchStrength({ name }, 'up'), name).toBe('weak');
    }
    expect(matchStrength({ name: 'Up in the Air' }, 'up in')).toBe('strong');
  });

  it("takes the server's reading where it sends one", () => {
    expect(matchStrength({ name: '重慶森林', match: 'strong' }, 'chungking')).toBe('strong');
    expect(matchStrength({ name: 'Up', match: 'weak' }, 'up')).toBe('weak');
  });

  it('cuts after the last strong hit, and nowhere when nothing is strong', () => {
    const items = [{ name: 'Up' }, { name: 'Superman' }, { name: 'Up in Smoke' }, { name: 'Cupid' }];
    expect(strongEnd(items, 'up'), 'a weak hit ranked among strong ones stays with them').toBe(3);
    expect(strongEnd([{ name: 'Superman' }, { name: 'Cupid' }], 'up')).toBe(0);
    expect(strongEnd([], 'up')).toBe(0);
  });
});

describe("the banner's count, in words", () => {
  it('says how many are waiting and nothing about how the sentence was built', () => {
    expect(bannerCountLine({ count: 16, named: [{}, {}] })).toBe(
      '16 titles you watched are waiting for your rating.'
    );
    expect(bannerCountLine({ count: 1, named: [{}] })).toBe(
      '1 title you watched is waiting for your rating.'
    );
    expect(bannerCountLine(null)).toBe('');
    expect(bannerCountLine({ count: 16 })).not.toMatch(/verdict|named/);
  });
});

describe('a shelf card carries the names a German viewer needs', () => {
  it('passes the original title and its language to the poster', () => {
    const card = toPosterTitle({
      title_id: 1, kind: 'movie', name: 'Wonderfully Beautiful', original_name: 'Wunderschön',
      original_language: 'de', seen: false
    });
    expect(card.original_name).toBe('Wunderschön');
    expect(card.original_language).toBe('de');
  });
});
