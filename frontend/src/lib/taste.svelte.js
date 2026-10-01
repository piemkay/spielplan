// Your taste and Compare (§6.5): where a marker sits on the track, the words read out for it, a row's
// poster strip, Compare's orders and its seats. The server decides what is read and who sees which films.
import { get, qs } from '$lib/api.js';

export const KINDS = { movie: 'Films', series: 'Series' };
export const NOUNS = { movie: 'films', series: 'series' };
export const PICKABLE_AT = 20;
export const TRACK = 152;
export const SLOTS = 4;
const MARK = 20;
// A marker at either end of the track sits this far in.
const INSET = 12;
const OFF_MIDDLE = 0.2;

// The vocabulary's facets, in its own order.
export const FACETS = [
  ['mood', 'Mood'],
  ['themes', 'Themes'],
  ['pacing', 'Pacing'],
  ['structure', 'Structure'],
  ['visual', 'Visual'],
  ['sound', 'Sound'],
  ['characters', 'Characters'],
  ['place', 'Place'],
  ['era', 'Era'],
  ['sensibility', 'Sensibility'],
  ['register', 'Register']
];
const FACET_NAMES = Object.fromEntries(FACETS);

// The kind both pages read, kept as the person moves between them.
export const taste = $state({ kind: 'movie' });

export const loadTaste = (kind) => get(`/taste${qs({ kind })}`);
export const loadMembers = (kind) => get(`/taste/members${qs({ kind })}`);
export const loadCompare = (kind, a, b) => get(`/taste/compare${qs({ kind, a, b })}`);

/** A marker's centre: the person's middle at half the track, their strongest term near an end. */
export function markX(pos, width = TRACK) {
  return width / 2 + pos * (width / 2 - INSET);
}

/** Two markers closer than one marker part to one apart, keeping their order (a on the left on a tie). */
export function markPair(pa, pb, width = TRACK) {
  const xa = markX(pa, width);
  const xb = markX(pb, width);
  if (Math.abs(xa - xb) >= MARK) return [xa, xb];
  const left = Math.min(Math.max((xa + xb) / 2 - MARK / 2, INSET), width - INSET - MARK);
  return xa <= xb ? [left, left + MARK] : [left + MARK, left];
}

/** Plain words for a position, read out and never drawn. Lower never reads as a dislike. */
export function placeWord(pos) {
  if (pos >= 0.5) return 'sits high';
  if (pos >= 0.2) return 'sits a little high';
  if (pos > -0.2) return 'near the middle';
  if (pos > -0.5) return 'lands a little lower';
  return 'lands lower';
}

/** A row's poster slots: every film when they fit, else one slot fewer and a "+N" tile for the rest. */
export function strip(films, more = 0, slots = SLOTS) {
  if (films.length + more <= slots) return { shown: films, plus: 0 };
  return { shown: films.slice(0, slots - 1), plus: films.length + more - (slots - 1) };
}

const gap = (row) => Math.abs(row.pa - row.pb);
const byLabel = (x, y) => (x.label < y.label ? -1 : x.label > y.label ? 1 : 0);

/** Both markers clearly to one side: high for both, or lower for both. */
export function offMiddle(row) {
  return row.pa * row.pb > 0 && Math.min(Math.abs(row.pa), Math.abs(row.pb)) >= OFF_MIDDLE;
}

export function mostDifferent(rows) {
  return [...rows].sort((x, y) => gap(y) - gap(x) || byLabel(x, y));
}

export function mostAlike(rows) {
  return [...rows].sort(
    (x, y) => Number(offMiddle(y)) - Number(offMiddle(x)) || gap(x) - gap(y) || byLabel(x, y)
  );
}

/** Compare's Show all, filtered to one facet or none: one ordered list, or a section per facet. */
export function showAll(rows, { sort = 'diff', facet = null } = {}) {
  const pool = facet ? rows.filter((r) => r.facet === facet) : rows;
  if (sort === 'facet') {
    return FACETS.map(([key, head]) => ({
      key,
      head,
      facet: key,
      rows: pool.filter((r) => r.facet === key).sort(byLabel)
    })).filter((section) => section.rows.length);
  }
  const head = facet ? FACET_NAMES[facet] ?? facet : `All ${rows.length} terms`;
  return [{ key: 'all', head, facet, rows: sort === 'alike' ? mostAlike(pool) : mostDifferent(pool) }];
}

/** The facets the rows carry, in the vocabulary's order. */
export function facetsOf(rows) {
  return FACETS.filter(([key]) => rows.some((r) => r.facet === key));
}

/** A pick fills the seat being changed; picking whoever sits in the other seat swaps the two. */
export function takeSeat(seats, edit, id) {
  if (seats[edit] === id) return seats;
  const next = [...seats];
  if (next[1 - edit] === id) next[1 - edit] = next[edit];
  next[edit] = id;
  return next;
}

/** Both seats filled, by two different people who can be picked. */
export function seatsReady(seats, members) {
  const [a, b] = seats;
  const ok = (id) => members.some((m) => m.id === id && m.pickable);
  return a != null && b != null && a !== b && ok(a) && ok(b);
}

export function gateLine(kind) {
  return `Comparing ${NOUNS[kind]} opens once two of you have each placed ${PICKABLE_AT} ${NOUNS[kind]}.`;
}
