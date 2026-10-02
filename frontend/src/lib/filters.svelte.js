// The term and people pickers' reads (decision 557): the vocabulary, its ranking and browsing,
// and the people typeahead.
import { get, qs } from '$lib/api.js';
import { facetColour } from '$lib/home.svelte.js';
import { FACETS } from '$lib/taste.svelte.js';

const FACET_NAMES = Object.fromEntries(FACETS);
export const facetName = (facet) => FACET_NAMES[facet] ?? facet.charAt(0).toUpperCase() + facet.slice(1);

/** @type {Map<string, Promise<{version: string | null, facets: any[], terms: any[]}>>} */
const vocabularies = new Map();

/** One read per kinds for the session; a failed read is not kept, so the next open asks again. */
export function loadVocabulary(kinds) {
  const key = [...kinds].sort().join(',');
  if (!vocabularies.has(key)) {
    const read = get(`/vocabulary${qs({ kind: kinds })}`);
    vocabularies.set(key, read);
    read.catch(() => vocabularies.delete(key));
  }
  return vocabularies.get(key);
}

const fold = (s) =>
  String(s ?? '')
    .normalize('NFD')
    .replace(/\p{M}/gu, '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();

// 0 exact, 1 starts with, 2 a word starts with, 3 contains; null for no match.
function quality(text, q) {
  const t = fold(text);
  if (t === q) return 0;
  if (t.startsWith(q)) return 1;
  const at = t.indexOf(q);
  if (at === -1) return null;
  for (let i = at; i !== -1; i = t.indexOf(q, i + 1)) {
    if (!/[\p{L}\p{N}]/u.test(t[i - 1])) return 2;
  }
  return 3;
}

/**
 * The terms a typed word finds, best first: exact, starts with, a word start, contains; a label
 * before an alias of the same quality; then the more of the library carries it. `via` names the
 * alias that found a term its label did not.
 */
export function rankTerms(vocab, q) {
  const query = fold(q);
  if (!query) return [];
  const hits = [];
  for (const t of vocab?.terms ?? []) {
    const byLabel = quality(t.label, query);
    let rank = byLabel === null ? null : byLabel * 2;
    let via = null;
    for (const alias of t.aliases ?? []) {
      const qual = quality(alias, query);
      if (qual === null || (rank !== null && qual * 2 + 1 >= rank)) continue;
      rank = qual * 2 + 1;
      via = alias;
    }
    if (rank === null) continue;
    hits.push({
      term: t.term, label: t.label, facet: t.facet, via: byLabel === null ? via : null, owned: t.owned ?? 0, rank
    });
  }
  hits.sort((a, b) => a.rank - b.rank || b.owned - a.owned || a.label.localeCompare(b.label));
  return hits.map(({ rank, ...hit }) => hit);
}

const ORDER = FACETS.map(([facet]) => facet);
const place = (facet) => (ORDER.includes(facet) ? ORDER.indexOf(facet) : ORDER.length);

/**
 * The facets in Taste's order, as the boards draw them, a facet it lacks after them in the
 * vocabulary's; each with its terms most carried first; `top` is the first eight.
 */
export function browseFacets(vocab) {
  return [...(vocab?.facets ?? [])]
    .sort((a, b) => place(a.facet) - place(b.facet))
    .map(({ facet }) => {
      const terms = (vocab.terms ?? [])
        .filter((t) => t.facet === facet)
        .map((t) => ({ term: t.term, label: t.label, facet, via: null, owned: t.owned ?? 0 }))
        .sort((a, b) => b.owned - a.owned || a.label.localeCompare(b.label));
      return {
        facet, name: facetName(facet), colour: facetColour(facet), top: terms.slice(0, 8), terms, total: terms.length
      };
    })
    .filter((f) => f.total);
}

/** The people typeahead: word-start, from two characters (§6.0). */
export async function searchPeople(q, kinds) {
  const query = String(q ?? '').trim();
  if (query.length < 2) return [];
  const res = await get(`/people${qs({ q: query, kind: kinds, limit: 8 })}`);
  return res?.people ?? [];
}
