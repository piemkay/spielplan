import { get, qs } from '$lib/api.js';

// The shipped facet ids, the prefix of every v1 term: `characters`, not `character`.
const FACETS = new Set([
  'mood', 'themes', 'pacing', 'structure', 'visual', 'sound',
  'characters', 'place', 'era', 'sensibility', 'register'
]);

// An unknown facet gets a neutral, never the accent (§6.8).
export function facetColour(facet) {
  return FACETS.has(facet) ? `var(--facet-${facet})` : 'var(--ink-4)';
}

export function loadHome(kinds) {
  return get(`/home${qs({ kind: kinds })}`);
}

// Home's place while another tab is open (decision 530): its member, kinds, shelves and scroll.
export const homeKept = { user: null, epoch: 0, kinds: null, payload: null, scrollY: 0 };

// With the toggle off the response has no `events` key at all, never an empty list to hide.
export function loadModelLog(limit = 15) {
  return get(`/model-log${qs({ limit })}`);
}

// Any filter away from its default switches to the grid, Only in library off included (decision
// 558): a filter hidden under the shelves is a dead control. One person alone is a filmography.
export function gridReason({
  q = '',
  terms = [],
  people = [],
  like = [],
  less = [],
  genre = '',
  decade = '',
  seen = 'any',
  owned = true
} = {}) {
  if (like.length + less.length > 0) return 'recipe';
  if (q && q.trim()) return 'search';
  const narrowed = Boolean(genre || decade || (seen && seen !== 'any') || !owned || terms.length);
  if (people.length === 1 && !narrowed) return 'person';
  if (people.length || narrowed) return 'filter';
  return null;
}

export function homeMode(state) {
  return gridReason(state) ? 'grid' : 'shelves';
}

export function activeFilterCount({
  genre = '',
  decade = '',
  seen = 'any',
  owned = true,
  terms = [],
  people = [],
  like = [],
  less = []
} = {}) {
  const set = [genre, decade, seen && seen !== 'any', !owned].filter(Boolean).length;
  return set + terms.length + people.length + like.length + less.length;
}

// A filtered grid names its order with the order control, and its filters with their chips.
/** @param {string | null} reason `gridReason`'s answer */
export function gridLine(reason) {
  if (reason === 'search') return 'Best match first';
  if (reason === 'person') return 'Their work in your library';
  return '';
}

const KIND_NOUNS = { movie: 'films', series: 'series' };

/** The search field's words (decision 558): the library's count while Only in library is on. */
export function searchPlaceholder({ library = null, kinds = [], owned = true } = {}) {
  if (!owned) return `Search all ${kinds.length > 1 ? 'titles' : (KIND_NOUNS[kinds[0]] ?? 'films')}`;
  return library ? `Search your ${libraryLabel({ library, kinds })}` : 'Search';
}

const listed = (parts) =>
  parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts.at(-1)}` : (parts[0] ?? '');

/** An empty grid names its chips (decision 557 item 7). */
export function emptyLine({ terms = [], people = [], genre = '', decade = '', seen = 'any', owned = true } = {}) {
  const parts = [
    ...terms.filter((t) => t.mode === 'in').map((t) => t.label),
    ...terms.filter((t) => t.mode === 'out').map((t) => `not ${t.label}`),
    ...people.map((p) => `by ${p.name}`),
    ...(genre ? [genre] : []),
    ...(decade ? [`from the ${decade}s`] : []),
    ...(seen === 'seen' ? ['seen'] : seen === 'unseen' ? ['not seen'] : [])
  ];
  if (!parts.length) return '';
  return `Nothing ${owned ? 'in your library ' : ''}is ${listed(parts)}.`;
}

/** The fields a chip's removal changes, by `chipOrder`'s key; removing Beyond your library turns
 *  Only in library back on. */
export function withoutChip(f, key) {
  if (key.startsWith('term:')) return { terms: f.terms.filter((t) => `term:${t.id}` !== key) };
  if (key.startsWith('person:')) {
    return { people: f.people.filter((p) => `person:${p.person_ids.join(',')}` !== key) };
  }
  if (key === 'seen') return { seen: 'any' };
  if (key === 'owned') return { owned: true };
  return { [key]: '' };
}

/** An empty grid's drop: what removing one chip leaves ("Without heist: 51 films"). */
export function dropLabel(chip, n, kinds) {
  const left = countLabel({ total: n, kinds });
  return chip.variant === 'term' && chip.mode === 'out'
    ? `With ${chip.label} too: ${left}`
    : `Without ${chip.label}: ${left}`;
}

// Offered for a filtered or person grid only (a search is best match first), only when the
// server echoes `sort`, and not while `for_you_available` is false: then it answers `newest`.
export const SORT_CHOICES = [
  { id: 'for_you', label: 'For you' },
  { id: 'newest', label: 'Newest' }
];

export function sortOffered(reason, echoed, available) {
  return (
    (reason === 'filter' || reason === 'person') &&
    available !== false &&
    SORT_CHOICES.some((c) => c.id === echoed)
  );
}

// Stands where the order control would be while the member's own order does not exist yet.
export function sortWaitingLine(reason, echoed, available) {
  if (reason !== 'filter' && reason !== 'person') return '';
  if (available !== false || echoed !== 'newest') return '';
  return 'Newest first. Your own order arrives once your ratings rank these.';
}

// The member's own order ranks every film, then every series; a search or the year interleaves.
export function partitionedByKind(kinds = [], echoed) {
  return echoed === 'for_you' && kinds.includes('movie') && kinds.includes('series');
}

export function partitionLine(kinds, echoed) {
  return partitionedByKind(kinds, echoed) ? 'Films first, then series.' : '';
}

export function kindHeading(items = [], index = 0) {
  const kind = items[index]?.kind;
  if (!kind || (index > 0 && items[index - 1]?.kind === kind)) return '';
  return kind === 'series' ? 'Series' : 'Films';
}

export function otherKinds(kinds = []) {
  return ['movie', 'series'].filter((k) => !kinds.includes(k));
}

/**
 * Names at most two and counts the rest, as the banner does.
 *
 * @param {string} kind
 * @param {string[]} names
 * @param {number} total
 */
export function elsewhereLine(kind, names = [], total = 0) {
  const label = kind === 'series' ? 'Series' : 'Films';
  const shown = names.slice(0, 2);
  if (!shown.length) return '';
  const rest = Math.max(0, total - shown.length);
  return `Found in ${label}: ${shown.join(', ')}${rest ? ` and ${rest.toLocaleString()} more` : ''}`;
}

// The server's `match` wins; this name reading is the fallback, where an alias-only hit reads weak.
export function matchStrength(item, q) {
  if (item?.match === 'strong' || item?.match === 'weak') return item.match;
  const needle = String(q ?? '').trim().toLowerCase();
  if (!needle) return 'strong';
  const name = String(item?.name ?? '').toLowerCase();
  const at = [...name.matchAll(/[\p{L}\p{N}]+/gu)].some((m) => name.startsWith(needle, m.index));
  return at || name.startsWith(needle) ? 'strong' : 'weak';
}

// After the last strong hit, so an alias hit ranked among name hits stays with them.
export function strongEnd(items = [], q = '') {
  let end = 0;
  items.forEach((item, i) => {
    if (matchStrength(item, q) === 'strong') end = i + 1;
  });
  return end;
}

// `series` has no plural.
export function plural(kind, n) {
  return kind === 'movie' ? `film${n === 1 ? '' : 's'}` : 'series';
}

function counted(n, kinds) {
  const noun = kinds.length === 1 ? plural(kinds[0], n) : n === 1 ? 'title' : 'titles';
  return `${n.toLocaleString()} ${noun}`;
}

// The grid counts what it lists; the set filters are named by their chips (decision 527).
export function countLabel({ total = 0, kinds = [], owned = false } = {}) {
  return `${counted(total, kinds)}${owned ? ' in your library' : ''}`;
}

// The shelves count the household's own library of the shown kind (§6.0).
export function libraryLabel({ library = {}, kinds = [] } = {}) {
  const shown = kinds.reduce((sum, kind) => sum + (library?.[kind] ?? 0), 0);
  return counted(shown, kinds);
}

// One switch, three positions; Both is a selection, never a merge (decisions 18, 474).
export const KIND_CHOICES = [
  { id: 'movie', label: 'Films', kinds: ['movie'] },
  { id: 'series', label: 'Series', kinds: ['series'] },
  { id: 'both', label: 'Both', kinds: ['movie', 'series'] }
];

export function kindChoice(kinds = []) {
  if (kinds.includes('movie') && kinds.includes('series')) return 'both';
  return kinds.includes('series') ? 'series' : 'movie';
}

export function kindsFor(choice) {
  return [...(KIND_CHOICES.find((c) => c.id === choice)?.kinds ?? ['movie'])];
}

// One row per (shelf, kind), never concatenated (§4.1 rule 5).
export function shelfRows(payload) {
  return (payload?.shelves ?? []).flatMap((shelf) =>
    (shelf.sections ?? []).map((section) => ({ shelf: shelf.id, section }))
  );
}

// One region per selected kind, in the payload's order, each row kind-scoped (decision 474).
export function kindRegions(payload) {
  const rows = shelfRows(payload);
  return (payload?.kinds ?? []).flatMap((kind) => {
    const own = rows.filter((row) => row.section.kind === kind);
    return own.length ? [{ kind, heading: own[0].section.heading, rows: own }] : [];
  });
}

// Shown only with Show the model on: the server sends `why_numbers` only then.
const WHY_NUMBER_NAMES = {
  beta: 'β',
  beta_optimum: 'β optimum',
  gate_k: 'gate k',
  label_count: 'labels',
  cos: 'cos',
  affinity: 'affinity',
  min_seen: 'min seen',
  min_cdf: 'cdf floor',
  co_seen: 'co-seen',
  max_minutes: 'max min'
};

export function whyNumbersLine(numbers) {
  if (!numbers || typeof numbers !== 'object') return '';
  return Object.entries(WHY_NUMBER_NAMES)
    .filter(([key]) => typeof numbers[key] === 'number')
    .map(([key, name]) => {
      const v = numbers[key];
      return `${name} ${Number.isInteger(v) ? v : v.toFixed(2)}`;
    })
    .join(' · ');
}

// The one rename from the shelf payload to the catalog card's shape. `e_source` and `item_n`
// must travel: the "new" badge is decided on them; so must `wanted` and `like` (decision 544).
export function toPosterTitle(item) {
  return {
    id: item.title_id,
    kind: item.kind,
    name: item.name,
    original_name: item.original_name,
    original_language: item.original_language,
    year: item.year,
    runtime_min: item.runtime_min,
    poster_path: item.poster_path,
    placement: item.placement,
    item_n: item.item_n,
    e_source: item.e_source,
    seen_state: item.seen ? 'seen' : 'unseen',
    wanted: Boolean(item.wanted),
    like: item.like ?? null
  };
}

export function eventTime(at) {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString(undefined, { hour12: false });
}

// Bumped by the account chip once the preference has landed; surfaces re-read gated payloads on it.
export const modelGate = $state({ epoch: 0 });

export function modelGateSettled() {
  modelGate.epoch += 1;
}
