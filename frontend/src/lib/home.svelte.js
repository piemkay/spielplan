/**
 * Home's own wire module. Spec v2.1 §6.0 (M2 shelves), §6.7 (the model log), §6.8;
 * decisions 18 and 117; proposals 20–33 and 150.
 *
 * `lib/api.js` is shared and stays untouched — this module imports its helpers and owns every
 * Home-shaped request and every rule that has to be the same on both sides of the wire.
 *
 * THE ONE RULE WORTH RESTATING. §6.0: "Search or an active person-filter switches Home into the
 * catalog grid; clearing it returns the shelves." The server computes that same mode for
 * `/api/home` (q or person_id ⇒ grid, and then the payload carries no shelves at all), so
 * `gridReason` below is a mirror, not a second opinion — which is why it is a pure function
 * with tests rather than a condition spread through the markup.
 */

import { get, qs } from '$lib/api.js';

/** Vocabulary v1's eleven facets (§6.8: "a fixed colour per vocabulary facet (11)"). */
const FACETS = new Set([
  'mood', 'themes', 'pacing', 'structure', 'visual', 'sound',
  'character', 'place', 'era', 'sensibility', 'register'
]);

/** An unknown facet gets a neutral. §6.8 spends the ember on selection and primary actions
 *  only, so a stray term must not borrow it. */
export function facetColour(facet) {
  return FACETS.has(facet) ? `var(--facet-${facet})` : 'var(--ink-4)';
}

// --- requests ---------------------------------------------------------------------------------

/**
 * The shelves half: greeting, banner, six shelves, the degraded state, and — only when the
 * §6.7 toggle is on — `rail` and `suppressed`.
 *
 * Called WITHOUT `q`/`person_id` on purpose. The catalog grid is `/api/titles`, which is the
 * only route carrying M0's genre/decade/seen filters; asking `/api/home` for the grid would
 * quietly drop three of the five filter dimensions §6.0 names.
 */
export function loadHome(kinds) {
  return get(`/home${qs({ kind: kinds })}`);
}

/** §6.7's rail. With the toggle off the response has no `events` key at all — never an empty
 *  list the client is trusted to hide. */
export function loadModelLog(limit = 15) {
  return get(`/model-log${qs({ limit })}`);
}

/** The banner alone, for a refresh after a verdict lands elsewhere. */
export function loadPendingVerdicts() {
  return get('/home/pending-verdicts');
}

// --- the two-mode state machine ---------------------------------------------------------------

/**
 * Why Home is showing the catalog grid, or `null` when it is showing shelves.
 *
 * `search` and `person` are §6.0's two named triggers. `filter` is the third: proposal 152
 * puts decade and seen-state (and M0's genre) on the catalog, and a filter whose effect is
 * invisible because the shelves are still on screen is the dead control that proposal exists
 * to fix. All three clear the same way, and clearing all of them returns the shelves — which
 * is the property `library-rate-home-grid-switch` actually asserts.
 */
export function gridReason({ q = '', personId = null, genre = '', decade = '', seen = 'any' } = {}) {
  if (q && q.trim()) return 'search';
  if (personId !== null && personId !== undefined && personId !== '') return 'person';
  if (genre || decade || (seen && seen !== 'any')) return 'filter';
  return null;
}

export function homeMode(state) {
  return gridReason(state) ? 'grid' : 'shelves';
}

// --- the count line ----------------------------------------------------------------------------

const KIND_NOUN = { movie: 'film', series: 'series' };

/** "6 films" / "1 film" / "2 series" — `series` has no plural, which a naive `+ 's'` gets wrong. */
export function plural(kind, n) {
  return kind === 'movie' ? `film${n === 1 ? '' : 's'}` : 'series';
}

/**
 * §6.0 / decision 18: "with one active the count line says how many the other holds
 * ('6 films · 2 series hidden')". A toggle that hides things without saying how many is the
 * silent truncation the two-toggle control was introduced to fix, so the hidden clause is part
 * of the count rather than a tooltip.
 */
export function countLabel({ total = 0, hidden = {}, kinds = [], filters = [] } = {}) {
  const head =
    kinds.length === 1
      ? `${total.toLocaleString()} ${plural(kinds[0], total)}`
      : `${total.toLocaleString()} titles`;
  const parts = [head];
  for (const [kind, n] of Object.entries(hidden ?? {})) {
    parts.push(`${n.toLocaleString()} ${plural(kind, n)} hidden`);
  }
  // Proposal 152: "Active filters render as removable chips beside the person chip, and the
  // result-count line states them."
  for (const f of filters) if (f) parts.push(f);
  return parts.join(' · ');
}

export { KIND_NOUN };

// --- shelves ------------------------------------------------------------------------------------

/**
 * Does this section ship?
 *
 * §6.0 M2: "a shelf that cannot say why it exists doesn't ship". The server already suppresses
 * such a section, and this is the client saying the same thing rather than trusting it: a
 * section that arrives with an empty why-line renders as nothing at all, never as a bare row
 * of posters under a blank heading.
 */
export function sectionShips(section) {
  return Boolean(
    section &&
      typeof section.why === 'string' &&
      section.why.trim().length > 0 &&
      Array.isArray(section.items) &&
      section.items.length > 0
  );
}

/**
 * The payload's shelves, flattened into the rows the page renders — one row per (shelf, kind).
 *
 * §4.1 rule 5 as decision 18 reads it: "a surface that ranks … renders two headed sections and
 * never one interleaved ranking". The two arrays are never concatenated here, and there is no
 * shelf-level `items` to concatenate even if someone tried: each row keeps its own section
 * object, with its own title, why-line and ordering.
 */
export function shelfRows(payload) {
  return (payload?.shelves ?? []).flatMap((shelf) =>
    (shelf.sections ?? [])
      .filter(sectionShips)
      .map((section) => ({ shelf: shelf.id, ranking: !!shelf.ranking, section }))
  );
}

/** How many kinds this shelf actually shipped — what makes the partition visible in the header. */
export function kindsOnShelf(shelf) {
  return (shelf?.sections ?? []).filter(sectionShips).map((s) => s.kind);
}

/**
 * A shelf card, in the shape `PosterCard` reads.
 *
 * The shelf payload speaks `title_id` and a boolean `seen`; the catalog card speaks `id` and a
 * `seen_state` string. One rename, in one place — a second spelling of "seen" in markup is how
 * the seen pill ends up on every card or on none.
 */
export function toPosterTitle(item) {
  return {
    id: item.title_id,
    kind: item.kind,
    name: item.name,
    year: item.year,
    runtime_min: item.runtime_min,
    poster_path: item.poster_path,
    placement: item.placement,
    seen_state: item.seen ? 'seen' : 'unseen'
  };
}

// --- the pending-verdicts banner -----------------------------------------------------------------

/**
 * The banner's CTA target, or `null` when it would lie.
 *
 * Proposal 150: "The CTA enters the §6.1 queue with the named titles at its head — a prompt
 * that names titles and then presents a different one is worse than no prompt." The server
 * builds the link (with REPEATED `head` parameters, because `GET /api/rate` declares
 * `head: list[int]` and a comma-joined value is a 422). This function's whole job is to refuse
 * to render a CTA that does not carry every named title: no synthesised `/rate`, ever.
 */
export function bannerHref(banner) {
  const route = banner?.cta?.route;
  if (typeof route !== 'string' || !route) return null;
  const head = banner?.head_title_ids ?? [];
  if (!head.length) return null;
  const query = route.includes('?') ? route.slice(route.indexOf('?') + 1) : '';
  const carried = new URLSearchParams(query).getAll('head');
  const wanted = head.map(String);
  if (wanted.length !== carried.length) return null;
  return wanted.every((id, i) => id === carried[i]) ? route : null;
}

/** Proposal 21's two registers, chosen by viewport rather than by rewriting the sentence here. */
export function bannerText(banner, { compact = false } = {}) {
  const copy = banner?.copy;
  if (!copy) return '';
  return (compact ? copy.compact : copy.wide) ?? '';
}

export function bannerLabel(banner, { compact = false } = {}) {
  const cta = banner?.cta;
  if (!cta) return '';
  return (compact ? cta.label_compact : cta.label_wide) ?? 'Rate now';
}

// --- §6.7's rail ----------------------------------------------------------------------------------

/** `13:41:07` — the data voice wants a timestamp, not "3 minutes ago". */
export function eventTime(at) {
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString(undefined, { hour12: false });
}

/**
 * Decision 117's gate, asked of a payload rather than of a preference.
 *
 * The server removes `model`, `rail` and `suppressed` when the toggle is off, so their absence
 * — not a local boolean — is what the UI branches on. A client that re-derived them from
 * `session.user.show_model` would render an empty rail for one frame after the toggle flips
 * and before the refetch lands, which is exactly the promise §6.7 makes about numbers not
 * being there.
 */
export function hasModelAnnotations(payload) {
  return Array.isArray(payload?.rail);
}

/**
 * The signal that says "the show-the-model preference has actually LANDED on the server".
 *
 * Not the preference itself. `setShowModel` updates `session.user` optimistically and only
 * then awaits the POST, so a surface that refetched on the optimistic change raced its own
 * write and got the old payload back — the toggle flipped, the rail stayed away, and nothing
 * looked broken enough to notice. The account chip bumps this AFTER the await; every surface
 * that has to re-read a gated payload watches this instead.
 */
export const modelGate = $state({ epoch: 0 });

export function modelGateSettled() {
  modelGate.epoch += 1;
}
