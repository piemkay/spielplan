/**
 * @vitest-environment jsdom
 *
 * §6.6 Data's review of DNA rejects and low-evidence tags: the server's order, every row, no cut.
 * Spec v2.1 §6.6 Data, §4.1 rule 2, §8 stage 7; decisions 341 and 446.
 *
 * Named BESIDE `test_dna_review.py`, `test_data_surface_guards.py` and `20-admin-data.spec.js` on
 * the review's coverage row, never instead of them (decision 226). The fixtures tempt a cut on
 * purpose - a NULL confidence and a 0.01 beside the rest - because a review that hid either would
 * look exactly as tidy as one that did not.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { get } from '$lib/api.js';
import DnaRejects from './components/DnaRejects.svelte';
import {
  evidencePath,
  evidenceRows,
  figure,
  ledgerPrefill,
  parseTitleId,
  rejectRows,
  titleOf
} from './dnaReview.svelte.js';
import { verdictDraft } from './ledgerEditors.svelte.js';

// Server order: weakest first, a NULL where the server put it (last), nothing removed.
const TAGS = [
  {
    term: 'pacing.slow_burn',
    facet: 'pacing',
    salience: 1,
    confidence: 0.01,
    n_sources: 1,
    provider: 'gemini'
  },
  { term: 'mood.dread', facet: 'mood', salience: 3, confidence: 0.6, n_sources: 2, provider: 'gemini' },
  { term: 'themes.obsession', facet: 'themes', salience: 3, confidence: 0.9, n_sources: 4, provider: '' },
  {
    term: 'era.period',
    facet: 'era',
    salience: 2,
    confidence: null,
    n_sources: null,
    provider: 'anthropic'
  }
];

// Server order: newest first.
const REJECTS = [
  {
    id: 8, title_id: 2, name: 'Prisoners', year: 2013, term: 'mood.cosy', facet: 'mood', salience: 9,
    quote: 'a warm hug', rule_violated: 'quote_not_found', provider: 'gemini', at: '2026-09-24T10:00:00Z'
  },
  {
    id: 3, title_id: null, name: null, year: null, term: 'themes.robots', facet: 'themes', salience: 0,
    quote: null, rule_violated: 'unknown_title', provider: 'anthropic', at: '2026-09-20T10:00:00Z'
  }
];

describe('the two orderings', () => {
  it('keeps the rejects in the order the server sent them, every one', () => {
    const rows = rejectRows({ rejects: REJECTS, limit: 200 });
    expect(rows).toBe(REJECTS);
    expect(rows.map((r) => r.id)).toEqual([8, 3]);
    expect(rejectRows(null)).toEqual([]);
  });

  it('keeps the low-evidence tags in server order with the unmeasured ones where they fell', () => {
    const rows = evidenceRows({ title_id: 2, version: 'v1', tags: TAGS });
    expect(rows).toBe(TAGS);
    expect(rows.map((r) => r.term)).toEqual(TAGS.map((r) => r.term));
    expect(rows).toHaveLength(4);
    expect(evidenceRows({ tags: undefined })).toEqual([]);
  });
});

describe('reading a row', () => {
  it('prints a figure in the data voice and a dash for one nobody measured', () => {
    expect(figure(0.6000000238418579)).toBe('0.60');
    expect(figure(3)).toBe('3');
    expect(figure(null)).toBe('-');
    expect(figure(undefined)).toBe('-');
  });

  it('takes a whole positive title id and nothing else', () => {
    expect(parseTitleId(' 12 ')).toBe(12);
    expect(parseTitleId('0')).toBe(null);
    expect(parseTitleId('1.5')).toBe(null);
    expect(parseTitleId('twelve')).toBe(null);
    expect(evidencePath(12)).toBe('/admin/dna/evidence/12');
  });

  it('opens a title verdict with the term and the title, and leaves the action open', () => {
    expect(ledgerPrefill(REJECTS[0])).toEqual({ scope: 'title', term: 'mood.cosy', title_id: '2' });
    expect(ledgerPrefill(TAGS[0], 5)).toEqual({ scope: 'title', term: 'pacing.slow_burn', title_id: '5' });
    expect(ledgerPrefill(REJECTS[1])).toEqual({ scope: 'title', term: 'themes.robots', title_id: '' });
    expect(titleOf(REJECTS[0])).toBe('Prisoners (2013)');
    expect(titleOf(REJECTS[1])).toBe('no title');
  });
});

describe('the mounted review', () => {
  let target;

  beforeEach(() => {
    target = document.createElement('div');
    document.body.appendChild(target);
    vi.mocked(get).mockReset();
  });

  afterEach(() => {
    target.remove();
  });

  async function settle() {
    for (let i = 0; i < 40; i++) await Promise.resolve();
    flushSync();
  }

  it('draws every tag in server order, offers no weight control, and hands a row on', async () => {
    vi.mocked(get).mockImplementation(async (path) =>
      path === '/admin/dna/rejects'
        ? { rejects: REJECTS, limit: 200 }
        : { title_id: 2, version: 'v1', tags: TAGS }
    );
    const app = mount(DnaRejects, { target, props: {} });
    await settle();
    try {
      expect([...target.querySelectorAll('[data-testid="dna-reject"]')]).toHaveLength(2);
      const input = target.querySelector('[data-testid="evidence-title"]');
      input.value = '2';
      input.dispatchEvent(new Event('input'));
      target.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }));
      await settle();
      expect(vi.mocked(get)).toHaveBeenLastCalledWith('/admin/dna/evidence/2');
      const drawn = [...target.querySelectorAll('[data-testid="evidence-tag"]')].map((e) =>
        e.getAttribute('data-term')
      );
      expect(drawn).toEqual(TAGS.map((t) => t.term));

      // One input on the whole card, the title id; no slider, no select, no toggle.
      expect([...target.querySelectorAll('input')]).toHaveLength(1);
      const controls = 'select, input[type="range"], input[type="checkbox"]';
      expect(target.querySelectorAll(controls)).toHaveLength(0);
      const names = [...target.querySelectorAll('button')].map((b) => b.textContent.trim());
      expect(names.some((n) => /accept|confidence|salience|n_sources|threshold/i.test(n))).toBe(false);

      const before = verdictDraft.seq;
      [...target.querySelectorAll('[data-testid="evidence-tag"] button')][0].click();
      expect(verdictDraft.seq).toBe(before + 1);
      expect(verdictDraft.prefill).toEqual({ scope: 'title', term: 'pacing.slow_burn', title_id: '2' });
    } finally {
      unmount(app);
    }
  });
});
