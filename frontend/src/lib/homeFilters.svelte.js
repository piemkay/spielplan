// Home's filters (decisions 557-559): one state for the grid, the chips and Home's URL. The recipe
// travels as the raw `like`/`less` strings its own module writes.
import { termLabel } from '$lib/terms.js';

const defaults = () => ({
  q: '',
  genre: '',
  decade: '',
  seen: 'any',
  // Only in library, on by default (decision 558).
  owned: true,
  /** @type {{id: string, label: string, facet: string, mode: 'in' | 'out'}[]} */
  terms: [],
  /** @type {{person_ids: number[], person_id: number, name: string, photo: any}[]} */
  people: [],
  /** @type {string[]} */
  like: [],
  /** @type {string[]} */
  less: [],
  panelOpen: false
});

export const homeFilters = $state(defaults());

export function resetHomeFilters() {
  Object.assign(homeFilters, defaults());
}

const ids = (f, mode) => f.terms.filter((t) => t.mode === mode).map((t) => t.id);
const groupKey = (p) => p.person_ids.join(',');

/**
 * The query for `/api/titles` and the mix routes; `owned` overrides Only in library's own reading.
 *
 * @param {typeof homeFilters} [f]
 * @param {{owned?: 'only' | 'not' | 'any'}} [opts]
 */
export function catalogParams(f = homeFilters, { owned } = {}) {
  return {
    q: f.q.trim(),
    genre: f.genre,
    decade: f.decade,
    seen: f.seen,
    term: ids(f, 'in'),
    not_term: ids(f, 'out'),
    person: f.people.map(groupKey),
    owned: owned ?? (f.owned ? 'only' : 'any')
  };
}

function termOf(id, mode) {
  return { id, label: termLabel(id), facet: id.split('.')[0], mode };
}

// One human's comma-joined ids; a group with anything but positive ids is no group.
function personOf(value) {
  const personIds = value.split(',').map((v) => Number(v.trim()));
  if (!personIds.every((id) => Number.isInteger(id) && id > 0)) return null;
  return { person_ids: personIds, person_id: personIds[0], name: '', photo: null };
}

/**
 * The filters a URL carries, or null when it carries none. Labels and names are placeholders until
 * `/api/titles` echoes them in `applied`.
 *
 * @param {URLSearchParams} searchParams
 */
export function readHomeUrl(searchParams) {
  const all = (key) => searchParams.getAll(key).map((v) => v.trim()).filter(Boolean);
  const terms = [];
  for (const [key, mode] of [['term', 'in'], ['not_term', 'out']]) {
    for (const id of all(key)) {
      if (!terms.some((t) => t.id === id)) terms.push(termOf(id, mode));
    }
  }
  const people = [];
  for (const group of all('person').map(personOf)) {
    if (group && !people.some((p) => groupKey(p) === groupKey(group))) people.push(group);
  }
  const like = all('like');
  const less = all('less');
  const panelOpen = searchParams.get('filters') === 'open';
  const ownedOff = searchParams.get('owned') === 'off';
  if (!terms.length && !people.length && !like.length && !less.length && !panelOpen && !ownedOff) {
    return null;
  }
  const state = { terms, people, like, less, panelOpen };
  const kinds = ['movie', 'series'].filter((k) => all('kind').includes(k));
  if (kinds.length) state.kinds = kinds;
  if (ownedOff) state.owned = false;
  return state;
}

// Commas and colons stay readable: `person=12,13`, `like=6087:mood,sound`.
const pair = ([key, value]) =>
  `${key}=${encodeURIComponent(value).replace(/%2C/gi, ',').replace(/%3A/gi, ':')}`;

/** Home's URL for these filters, '/' when nothing is set; kinds ride along only with a filter. */
export function writeHomeUrl(f = homeFilters, { kinds = [] } = {}) {
  const pairs = [
    ...ids(f, 'in').map((id) => ['term', id]),
    ...ids(f, 'out').map((id) => ['not_term', id]),
    ...f.people.map((p) => ['person', groupKey(p)]),
    ...f.like.map((v) => ['like', v]),
    ...f.less.map((v) => ['less', v])
  ];
  const tail = [];
  if (f.panelOpen) tail.push(['filters', 'open']);
  if (!f.owned) tail.push(['owned', 'off']);
  if (!pairs.length && !tail.length) return '/';
  return `/?${[...pairs, ...kinds.map((k) => ['kind', k]), ...tail].map(pair).join('&')}`;
}

/** A jump to Home's grid with that one chip, every other filter at its default (decision 557 item 6). */
export function homeHref({ person = null, term = null, like = null, kinds = [], open = false } = {}) {
  const f = defaults();
  if (person != null) f.people = [personOf([].concat(person).join(','))].filter(Boolean);
  if (term) f.terms = [termOf(term.term ?? term, 'in')];
  if (like != null) f.like = [String(like)];
  f.panelOpen = open;
  return writeHomeUrl(f, { kinds });
}

const SEEN_CHIP = { seen: 'Seen', unseen: 'Not seen' };

/** The chips as FilterChip draws them: people, includes, leave-outs, then the rest. */
export function chipOrder(f = homeFilters) {
  const terms = (mode) =>
    f.terms
      .filter((t) => t.mode === mode)
      .map((t) => ({
        key: `term:${t.id}`, variant: 'term', mode, label: t.label || termLabel(t.id), facet: t.facet,
        term: t, testid: 'term-chip'
      }));
  const plain = (key, label, testid) => ({ key, variant: 'plain', label, testid });
  return [
    ...f.people.map((p) => ({
      key: `person:${groupKey(p)}`, variant: 'person', label: p.name, person: p, testid: 'person-chip'
    })),
    ...terms('in'),
    ...terms('out'),
    ...(f.genre ? [plain('genre', f.genre, 'genre-chip')] : []),
    ...(f.decade ? [plain('decade', `${f.decade}s`, 'decade-chip')] : []),
    ...(SEEN_CHIP[f.seen] ? [plain('seen', SEEN_CHIP[f.seen], 'seen-chip')] : []),
    ...(f.owned ? [] : [plain('owned', 'Beyond your library', 'owned-filter-chip')])
  ];
}
