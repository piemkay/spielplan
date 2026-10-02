import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', async (importOriginal) => ({
  .../** @type {object} */ (await importOriginal()),
  get: vi.fn()
}));

import { get } from '$lib/api.js';
import { browseFacets, loadVocabulary, rankTerms, searchPeople } from './filters.svelte.js';

const term = (id, label, owned, aliases = []) => ({ term: id, facet: id.split('.')[0], label, aliases, owned });

beforeEach(() => {
  vi.mocked(get).mockReset();
});

describe('the vocabulary', () => {
  it('is read once per kinds, in either order, and kept', async () => {
    vi.mocked(get).mockResolvedValue({ version: 'v1', facets: [], terms: [] });
    await loadVocabulary(['movie']);
    await loadVocabulary(['movie']);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenLastCalledWith('/vocabulary?kind=movie');
    await loadVocabulary(['series', 'movie']);
    await loadVocabulary(['movie', 'series']);
    expect(get).toHaveBeenCalledTimes(2);
    expect(get).toHaveBeenLastCalledWith('/vocabulary?kind=series&kind=movie');
  });

  it('is asked again after a read that failed', async () => {
    vi.mocked(get).mockRejectedValueOnce(new Error('down')).mockResolvedValue({ version: 'v1', facets: [], terms: [] });
    await expect(loadVocabulary(['series'])).rejects.toThrow('down');
    await expect(loadVocabulary(['series'])).resolves.toEqual({ version: 'v1', facets: [], terms: [] });
    expect(get).toHaveBeenCalledTimes(2);
  });
});

describe('a typed term', () => {
  const VOCAB = {
    terms: [
      term('mood.nostalgic', 'nostalgic', 158, ['cosy nostalgia']),
      term('mood.cozy', 'cozy & mellow', 51, ['cosy', 'cozy']),
      term('sensibility.comforting', 'comforting register', 49, ['cosy adventure']),
      term('mood.calm', 'calm', 500, ['mellow']),
      term('mood.mellow', 'mellow', 1),
      term('themes.theists', 'theists', 400),
      term('themes.caper', 'caper heist', 3),
      term('structure.heist_thriller', 'heist thriller', 2),
      term('themes.heist', 'heist', 1)
    ]
  };
  const ids = (q) => rankTerms(VOCAB, q).map((t) => t.term);

  it('ranks exact, then starts with, then a word start, then contains, whatever the counts', () => {
    expect(ids('heist')).toEqual(['themes.heist', 'structure.heist_thriller', 'themes.caper', 'themes.theists']);
  });

  it('puts a label before an alias of the same quality, then the more of the library first', () => {
    expect(ids('mellow')).toEqual(['mood.mellow', 'mood.calm', 'mood.cozy']);
    // "cosy" is exact on cozy's alias; the other two start with it, by alias alone.
    expect(ids('cosy')).toEqual(['mood.cozy', 'mood.nostalgic', 'sensibility.comforting']);
  });

  it('says which alias found a term its label did not', () => {
    const hits = rankTerms(VOCAB, 'Cosy');
    expect(hits[0]).toEqual({ term: 'mood.cozy', label: 'cozy & mellow', facet: 'mood', via: 'cosy', owned: 51 });
    expect(hits[1].via).toBe('cosy nostalgia');
    expect(rankTerms(VOCAB, 'cozy')[0].via).toBeNull();
  });

  it('finds nothing for a word the vocabulary lacks, or for an empty field', () => {
    expect(rankTerms(VOCAB, 'noir')).toEqual([]);
    expect(rankTerms(VOCAB, '  ')).toEqual([]);
  });
});

describe('browsing', () => {
  it('groups by facet in the vocabulary\'s order, the most carried first, eight on top', () => {
    const many = Array.from({ length: 10 }, (_, i) => term(`themes.t${i}`, `t${i}`, i));
    const vocab = {
      facets: [{ facet: 'themes' }, { facet: 'mood' }, { facet: 'era' }],
      terms: [term('mood.calm', 'calm', 5), term('mood.bleak', 'bleak', 9), term('mood.airy', 'airy', 5), ...many]
    };
    const facets = browseFacets(vocab);
    expect(facets.map((f) => [f.facet, f.name, f.total])).toEqual([['themes', 'Themes', 10], ['mood', 'Mood', 3]]);
    expect(facets[0].top.map((t) => t.label)).toEqual(['t9', 't8', 't7', 't6', 't5', 't4', 't3', 't2']);
    expect(facets[0].terms).toHaveLength(10);
    expect(facets[1].top.map((t) => t.label)).toEqual(['bleak', 'airy', 'calm']);
    expect(facets[1].colour).toBe('var(--facet-mood)');
  });
});

describe('the people typeahead', () => {
  it('asks from two characters, for the kinds shown', async () => {
    vi.mocked(get).mockResolvedValue({ people: [{ person_id: 77, name: 'Denis Villeneuve' }] });
    expect(await searchPeople('v', ['movie'])).toEqual([]);
    expect(get).not.toHaveBeenCalled();
    expect(await searchPeople(' vill ', ['movie', 'series'])).toEqual([{ person_id: 77, name: 'Denis Villeneuve' }]);
    expect(get).toHaveBeenCalledWith('/people?q=vill&kind=movie&kind=series&limit=8');
  });
});
