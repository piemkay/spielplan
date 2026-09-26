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

/** Vocabulary v1's eleven facets (§6.8: "a fixed colour per vocabulary facet (11)").
 *
 *  These are the SHIPPED ids — the prefix of every term in `dna_vocab/v1`, and since M4.9 what
 *  `dna_tag.facet` holds too. `characters`, not `character`: the vocabulary file is
 *  `vocab_characters_v1.tsv`. [M4.9 finding 4] */
const FACETS = new Set([
  'mood', 'themes', 'pacing', 'structure', 'visual', 'sound',
  'characters', 'place', 'era', 'sensibility', 'register'
]);

/** An unknown facet gets a neutral. §6.8 spends the ember on selection and primary actions
 *  only, so a stray term must not borrow it. */
export function facetColour(facet) {
  return FACETS.has(facet) ? `var(--facet-${facet})` : 'var(--ink-4)';
}

// --- requests ---------------------------------------------------------------------------------

/**
 * The shelves half: greeting, banner, six shelves, the degraded state, and — only when the
 * §6.7 toggle is on — every card's `model` block and proposal 28's `suppressed` list.
 *
 * No `rail`, whatever the toggle says. §6.7's events ride `/api/model-log` and nothing else:
 * the drawer is mounted once in `+layout.svelte` and refetches on open, so a copy here would
 * refresh on Home's cadence for a reader that no longer exists — one drawer, not one per
 * route, which is the rule `home/shelves.py` now keeps. [M4.9 review cycle 1: M49-HOME-04]
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
export function gridReason({
  q = '',
  personId = null,
  genre = '',
  decade = '',
  seen = 'any',
  owned = false
} = {}) {
  if (q && q.trim()) return 'search';
  if (personId !== null && personId !== undefined && personId !== '') return 'person';
  if (genre || decade || (seen && seen !== 'any') || owned) return 'filter';
  return null;
}

export function homeMode(state) {
  return gridReason(state) ? 'grid' : 'shelves';
}

/**
 * How many of the four catalog filters are set - the number the one "Filters" control wears while
 * its panel is shut, so a narrowed grid never hides why it is narrow (decision 516). The person
 * filter is not one of them: its chip is always on screen.
 */
export function activeFilterCount({ genre = '', decade = '', seen = 'any', owned = false } = {}) {
  return [Boolean(genre), Boolean(decade), Boolean(seen && seen !== 'any'), Boolean(owned)].filter(
    Boolean
  ).length;
}

/**
 * The line over the grid: what it is, and how to get the shelves back, in words. It was
 * `filtered · clear it to get your shelves back` in the data voice.
 *
 * @param {string | null} reason `gridReason`'s answer: 'search', 'person' or 'filter'
 */
export function gridLine(reason) {
  if (reason === 'search') {
    return 'Search results, best match first. Clear the search to get your shelves back.';
  }
  if (reason === 'person') {
    return 'Everything they worked on. Remove their name to get your shelves back.';
  }
  return 'Filtered. Remove the filters to get your shelves back.';
}

/**
 * The order the catalog grid can be read in (decision 516's control over the server's `sort`):
 * "For you" is the member's own score, "Newest" the year. A search is always best match first
 * (decision 472), so the control is offered for a filtered or a person's grid only, and only when
 * the server says which order it used - a build that does not echo `sort` gets no control rather
 * than one that claims an order it cannot vouch for.
 */
export const SORT_CHOICES = [
  { id: 'for_you', label: 'For you' },
  { id: 'newest', label: 'Newest' }
];

export function sortOffered(reason, echoed) {
  return (reason === 'filter' || reason === 'person') && SORT_CHOICES.some((c) => c.id === echoed);
}

/** The kinds a switch position leaves out: where a search that found nothing may have matches. */
export function otherKinds(kinds = []) {
  return ['movie', 'series'].filter((k) => !kinds.includes(k));
}

/**
 * "Found in Series: Broadchurch" - what an empty search says when the other kind holds matches,
 * instead of "Nothing matches" about a series the Films position was hiding (U7 of the second
 * household test). Names at most two and counts the rest, as §6.0's banner does.
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

/**
 * Does this search hit match the way a person means it - the name, or a word in it, starting with
 * what they typed - or only contain the letters somewhere? "Up" matched 361 films, "Up" and "Up in
 * the Air" among them and "Superman" and "Cupid" too (U14 of the second household test).
 *
 * The server's own reading wins where it sends one (`match: 'strong' | 'weak'`, decision 472's
 * six qualities cut after "a word start anywhere"); without it, a word of the name starting with
 * the query is strong. A hit through an alias reads weak here, which the server's field corrects.
 */
export function matchStrength(item, q) {
  if (item?.match === 'strong' || item?.match === 'weak') return item.match;
  const needle = String(q ?? '').trim().toLowerCase();
  if (!needle) return 'strong';
  const name = String(item?.name ?? '').toLowerCase();
  const at = [...name.matchAll(/[\p{L}\p{N}]+/gu)].some((m) => name.startsWith(needle, m.index));
  return at || name.startsWith(needle) ? 'strong' : 'weak';
}

/**
 * Where the weak tail of a best-match-first list starts: after the last strong hit, so an alias
 * hit the server ranked above a name hit is never cut off from the matches it sits among. Zero
 * when nothing is strong - a list of only weak hits is shown as it is, with nothing to set apart.
 */
export function strongEnd(items = [], q = '') {
  let end = 0;
  items.forEach((item, i) => {
    if (matchStrength(item, q) === 'strong') end = i + 1;
  });
  return end;
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
      : `${total.toLocaleString()} ${total === 1 ? 'title' : 'titles'}`;
  const parts = [head];
  for (const [kind, n] of Object.entries(hidden ?? {})) {
    parts.push(`${n.toLocaleString()} ${plural(kind, n)} hidden`);
  }
  // Proposal 152: "Active filters render as removable chips beside the person chip, and the
  // result-count line states them."
  for (const f of filters) if (f) parts.push(f);
  return parts.join(' · ');
}

/**
 * The count line over the SHELVES: what they draw on, which is the household's own library.
 *
 * It used to be the catalog's line - "13,330 films · 5,747 series hidden" - printed above shelves
 * that hold only owned titles, so it counted something the screen was not showing. The grid
 * keeps `countLabel`; the shelves count `payload.library`, the owned titles per kind, and name
 * what the unselected kind holds in the same "hidden" clause decision 18 gave the catalog.
 */
export function libraryLabel({ library = {}, kinds = [] } = {}) {
  const shown = kinds.reduce((sum, kind) => sum + (library?.[kind] ?? 0), 0);
  const head =
    kinds.length === 1
      ? `${shown.toLocaleString()} ${plural(kinds[0], shown)} in your library`
      : `${shown.toLocaleString()} ${shown === 1 ? 'title' : 'titles'} in your library`;
  const parts = [head];
  for (const [kind, n] of Object.entries(library ?? {})) {
    if (!kinds.includes(kind) && n) parts.push(`${n.toLocaleString()} ${plural(kind, n)} hidden`);
  }
  return parts.join(' · ');
}

export { KIND_NOUN };

// --- the kind switch (decision 474) ------------------------------------------------------------

/**
 * Home's kind control is one switch with three positions - Films, Series, Both - and not decision
 * 18's two toggles. Tapping "Series" with Films on used to ADD series under the film shelves, and
 * a member who wanted to switch to series had to find out that a second tap on Films was the way.
 * Decision 18's invariants all hold: never neither (no position is empty), and Both is a
 * selection, not a merge - the shelves still arrive one kind-headed section per kind.
 */
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
 * The shelves grouped by kind: one region per selected kind, Films then Series, each holding
 * that kind's shelves in the table's order. Decision 474.
 *
 * Read from the payload's `sections`, which the server has always built (`sections_by_kind`) and
 * no client read. With Both on, `shelfRows` alternated a Films row and a Series row per shelf,
 * which is what made the Series tap look like series stacked under the films. A region is still
 * a list of kind-scoped sections: nothing here can put two kinds' items in one row.
 */
export function kindRegions(payload) {
  return (payload?.sections ?? [])
    .map((region) => ({
      kind: region.kind,
      heading: region.heading,
      rows: (region.shelves ?? []).flatMap((shelf) =>
        (shelf.sections ?? [])
          .filter((section) => section.kind === region.kind && sectionShips(section))
          .map((section) => ({ shelf: shelf.id, ranking: !!shelf.ranking, section }))
      )
    }))
    .filter((region) => region.rows.length);
}

/** The data-voice names of the numbers a shelf's ordering used (decision 486: shown only with
 *  Show the model on, and the server sends `why_numbers` only then). */
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

/** `β 0.62 · gate k 10` — §6.8's "model numbers in the data voice next to their name". */
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

/**
 * A shelf card, in the shape `PosterCard` reads.
 *
 * The shelf payload speaks `title_id` and a boolean `seen`; the catalog card speaks `id` and a
 * `seen_state` string. One rename, in one place — a second spelling of "seen" in markup is how
 * the seen pill ends up on every card or on none.
 *
 * `e_source` and `item_n` travel with `placement` because §8 stage 10's badge is decided on
 * them, not on it: `PosterCard`'s own comment is the specification ("Off `e_source`/`item_n`,
 * NOT off `title.placement`"), and dropping them here silently returned the card to the
 * fallback branch — 111 of the 130 badges Home drew were false. The server moved both out of
 * the decision-117 `model` block for this; a rename that forgets them undoes that.
 * [M4.9 finding 18]
 */
export function toPosterTitle(item) {
  return {
    id: item.title_id,
    kind: item.kind,
    name: item.name,
    // Decision 516: the card leads with the original title where it is the viewer's language.
    original_name: item.original_name,
    original_language: item.original_language,
    year: item.year,
    runtime_min: item.runtime_min,
    poster_path: item.poster_path,
    placement: item.placement,
    item_n: item.item_n,
    e_source: item.e_source,
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

/**
 * The banner's second line, in words. It read "16 seen, no verdict · 2 named" - the model's word
 * for a rating and a count of how the sentence above it was built (U6 of the second household
 * test). What it has to say is how many are waiting in all, since the sentence names at most two.
 */
export function bannerCountLine(banner) {
  const n = Number(banner?.count) || 0;
  if (!n) return '';
  return n === 1
    ? '1 title you watched is waiting for your rating.'
    : `${n.toLocaleString()} titles you watched are waiting for your rating.`;
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
