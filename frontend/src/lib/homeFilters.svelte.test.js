import { afterEach, describe, expect, it } from 'vitest';

import {
  catalogParams,
  chipOrder,
  homeFilters,
  homeHref,
  readHomeUrl,
  resetHomeFilters,
  writeHomeUrl
} from './homeFilters.svelte.js';

const COZY = { id: 'mood.cozy', label: 'cozy & mellow', facet: 'mood', mode: 'in' };
const HEIST = { id: 'themes.heist', label: 'heist', facet: 'themes', mode: 'out' };
const CAINE = { person_ids: [12, 13], person_id: 12, name: 'Michael Caine', photo: true };

const url = (search) => new URL(`http://localhost${search}`).searchParams;

afterEach(resetHomeFilters);

describe("Home's filters", () => {
  it('start with Only in library on and nothing else set', () => {
    expect(homeFilters).toEqual({
      q: '', genre: '', decade: '', seen: 'any', owned: true,
      terms: [], people: [], like: [], less: [], panelOpen: false
    });
  });

  it('reset every field to its default', () => {
    Object.assign(homeFilters, { q: 'heat', genre: 'Crime', owned: false, terms: [COZY], people: [CAINE], like: ['245'], panelOpen: true });
    resetHomeFilters();
    expect(homeFilters.owned).toBe(true);
    expect(homeFilters.terms).toEqual([]);
    expect(homeFilters.like).toEqual([]);
    expect(homeFilters.panelOpen).toBe(false);
  });
});

describe('the catalogue query', () => {
  it('splits terms into includes and leave-outs and joins each person group', () => {
    Object.assign(homeFilters, { q: ' heat ', genre: 'Crime', decade: '1990', seen: 'unseen', terms: [COZY, HEIST], people: [CAINE] });
    expect(catalogParams()).toEqual({
      q: 'heat', genre: 'Crime', decade: '1990', seen: 'unseen',
      term: ['mood.cozy'], not_term: ['themes.heist'], person: ['12,13'], owned: 'only'
    });
  });

  it('reads the library while Only in library is on, the catalogue while it is off', () => {
    expect(catalogParams().owned).toBe('only');
    homeFilters.owned = false;
    expect(catalogParams().owned).toBe('any');
  });

  it('takes the caller\'s owned scope over the toggle', () => {
    expect(catalogParams(homeFilters, { owned: 'not' }).owned).toBe('not');
    homeFilters.owned = false;
    expect(catalogParams(homeFilters, { owned: 'only' }).owned).toBe('only');
  });
});

describe("Home's URL", () => {
  it('reads as nothing without a filter param, kind alone included', () => {
    expect(readHomeUrl(url('/'))).toBeNull();
    expect(readHomeUrl(url('/?kind=series&q=heat'))).toBeNull();
  });

  it('carries every chip, the recipe, the open panel and the library switch both ways', () => {
    Object.assign(homeFilters, {
      terms: [COZY, HEIST], people: [CAINE, { person_ids: [40], person_id: 40, name: 'Denis Villeneuve', photo: null }],
      like: ['245', '6087:mood,sound'], less: ['1891'], panelOpen: true, owned: false
    });
    const written = writeHomeUrl(homeFilters, { kinds: ['movie', 'series'] });
    expect(written).toBe(
      '/?term=mood.cozy&not_term=themes.heist&person=12,13&person=40&like=245&like=6087:mood,sound' +
        '&less=1891&kind=movie&kind=series&filters=open&owned=off'
    );
    const read = readHomeUrl(url(written));
    expect(read).toEqual({
      kinds: ['movie', 'series'],
      terms: [
        { id: 'mood.cozy', label: 'cozy', facet: 'mood', mode: 'in' },
        { id: 'themes.heist', label: 'heist', facet: 'themes', mode: 'out' }
      ],
      people: [
        { person_ids: [12, 13], person_id: 12, name: '', photo: null },
        { person_ids: [40], person_id: 40, name: '', photo: null }
      ],
      like: ['245', '6087:mood,sound'],
      less: ['1891'],
      panelOpen: true,
      owned: false
    });
  });

  it('leaves Only in library at its default and the kinds unsaid when the URL does not name them', () => {
    const read = readHomeUrl(url('/?term=mood.cozy'));
    expect(read).not.toHaveProperty('owned');
    expect(read).not.toHaveProperty('kinds');
  });

  it('drops a person group that is not ids, and a repeated chip', () => {
    const read = readHomeUrl(url('/?person=12,x&person=40&person=40&term=mood.cozy&not_term=mood.cozy'));
    expect(read.people.map((p) => p.person_ids)).toEqual([[40]]);
    expect(read.terms).toEqual([{ id: 'mood.cozy', label: 'cozy', facet: 'mood', mode: 'in' }]);
  });

  it('is the bare page when nothing is set, whatever the kinds', () => {
    expect(writeHomeUrl(homeFilters, { kinds: ['series'] })).toBe('/');
  });

  it('reads an encoded comma and colon as the plain ones', () => {
    expect(readHomeUrl(url('/?person=12%2C13&like=6087%3Amood')).people[0].person_ids).toEqual([12, 13]);
    expect(readHomeUrl(url('/?like=6087%3Amood')).like).toEqual(['6087:mood']);
  });
});

describe('a jump to Home', () => {
  it('opens a credit on Both with that person alone', () => {
    expect(homeHref({ person: [12, 13], kinds: ['movie', 'series'] })).toBe('/?person=12,13&kind=movie&kind=series');
  });

  it('opens a term on the card\'s kind', () => {
    expect(homeHref({ term: 'mood.cozy', kinds: ['movie'] })).toBe('/?term=mood.cozy&kind=movie');
    expect(homeHref({ term: { term: 'mood.cozy', label: 'cozy & mellow' }, kinds: ['movie'] })).toBe(
      '/?term=mood.cozy&kind=movie'
    );
  });

  it('opens More like this with the Filters open', () => {
    expect(homeHref({ like: 245, kinds: ['movie'], open: true })).toBe('/?like=245&kind=movie&filters=open');
  });

  it('ignores the filters already set on Home', () => {
    Object.assign(homeFilters, { terms: [HEIST], owned: false });
    expect(homeHref({ person: [40], kinds: ['movie'] })).toBe('/?person=40&kind=movie');
  });
});

describe('the chips', () => {
  it('come people first, then includes, leave-outs and the rest', () => {
    Object.assign(homeFilters, {
      terms: [HEIST, COZY], people: [CAINE], genre: 'Crime', decade: '1990', seen: 'seen', owned: false
    });
    expect(chipOrder().map((c) => [c.variant, c.label, c.testid])).toEqual([
      ['person', 'Michael Caine', 'person-chip'],
      ['term', 'cozy & mellow', 'term-chip'],
      ['term', 'heist', 'term-chip'],
      ['plain', 'Crime', 'genre-chip'],
      ['plain', '1990s', 'decade-chip'],
      ['plain', 'Seen', 'seen-chip'],
      ['plain', 'Beyond your library', 'owned-filter-chip']
    ]);
    expect(chipOrder()[2]).toMatchObject({ mode: 'out', facet: 'themes', key: 'term:themes.heist' });
  });

  it('show none at the defaults: Only in library on is no chip', () => {
    expect(chipOrder()).toEqual([]);
  });
});
