// The board always comes from the server and is replaced whole (§6.3 forbids snapping back); the
// queue's pair is a sealed token this module never opens (§13).

import { ApiError, get, post, qs } from '$lib/api.js';
import { haptic } from '$lib/motion.js';
import { runtimeLabel } from '$lib/rate.svelte.js';
import { showToast } from '$lib/toast.svelte.js';

export const KIND_LABELS = { movie: 'Films', series: 'Series' };

const NOUNS = { movie: ['film', 'films'], series: ['series', 'series'] };

// Counted here from taps: a server count would stand still on a held-out answer and reveal it (§13).
export const ROUND_SIZE = 15;

export const ROUND_END_TITLE = `That's ${ROUND_SIZE}.`;
export const ROUND_END_TEXT = `Stop here, or keep going for another ${ROUND_SIZE}.`;

// The two DNA tiers stay distinguishable (§4.1 rule 1), named by what each is to a member.
export const DNA_TIER_LABELS = { extracted: 'quoted', projected: 'our read' };

export function dnaTierText(tiers) {
  return (tiers ?? []).map((t) => DNA_TIER_LABELS[t] ?? t).join(' + ');
}

/** §6.3's genre and decade vocabularies, scoped to the kind on screen (as Home scopes them). */
export const facets = $state({ genres: [], decades: [] });

export const rank = $state({
  loading: true,
  booted: false,
  busy: false,
  /** Board reads in flight: the board on screen is stale while this is above 0. */
  reading: 0,
  /** @type {string | null} the queue answer in flight, in the pair card's words */
  pending: null,
  error: '',
  notice: '',
  kind: 'movie',
  /** @type {string[]} */
  tierSet: [],
  /** @type {any[]} one entry per tier, best-first, empty tiers kept (proposal 82) */
  tiers: [],
  /** Decision 528: how many of each tier's entries the board asks for; null is all of them. */
  perTier: null,
  /** @type {number[]} the tiers opened in place, which come whole */
  expanded: [],
  rated: 0,
  ratedTotal: 0,
  /** Decision 550: before the person's set-up the board is read-only. */
  setUp: true,
  /** Too few comparisons since the set-up for the order inside a step to be theirs (decision 550). */
  guessing: false,
  /** What Sharpen would ask about: the titles that sit between two tiers. */
  straddling: 0,
  /** Decision 209: the server says a full fit is owed, so the two counts above are not yet final. */
  fitting: false,
  /** @type {Record<string, any>} what the person has switched on */
  filters: {},
  /** @type {Record<string, string[]> | null} §4.1 rule 1: which tier matched each survivor */
  dnaTiers: null,
  /** @type {any} §6.7, present only when the model-log toggle is on */
  model: null,
  /** @type {string[]} §6.7's lines for the last write */
  log: [],
  /** @type {null | number} decision 496: the title whose card a tap opened */
  opened: null,
  /** @type {any} the comparison queue's current pair, or null */
  pair: null,
  queueReason: '',
  queueOpen: false,
  roundAnswered: 0,
  roundDone: false,
  /** @type {any[]} where the last answered pair's two titles sit now, from the answer route */
  placed: []
});

/** The filter state the person is editing, kept out of `rank` so a redraw cannot clobber typing. */
export const draft = $state({
  q: '',
  genre: '',
  decade: '',
  runtime_max: '',
  seen: 'any',
  dna: ''
});

function query() {
  return {
    kind: rank.kind,
    per_tier: rank.perTier ?? undefined,
    q: draft.q || undefined,
    genre: draft.genre || undefined,
    decade: draft.decade || undefined,
    runtime_max: draft.runtime_max || undefined,
    seen: draft.seen !== 'any' ? draft.seen : undefined,
    dna: draft.dna || undefined
  };
}

/** Fold one board response into the store. The server's tiers replace ours; nothing merges. */
export function apply(payload) {
  rank.tierSet = payload.tier_set ?? [];
  rank.tiers = payload.tiers ?? [];
  rank.rated = payload.rated ?? 0;
  rank.ratedTotal = payload.rated_total ?? 0;
  rank.setUp = payload.set_up ?? true;
  rank.guessing = payload.guessing ?? false;
  rank.straddling = payload.straddling ?? 0;
  rank.fitting = payload.fitting ?? false;
  rank.filters = payload.filters ?? {};
  rank.dnaTiers = payload.dna_tiers ?? null;
  // The server deletes the gated key rather than emptying it, so this reads an absence.
  rank.model = payload.model ?? null;
  rank.log = payload.log ?? [];
  rank.booted = true;
  rank.loading = false;
  return payload;
}

function fail(err) {
  if (err instanceof ApiError && err.status === 409) {
    rank.notice = err.detail?.message ?? 'that pair is no longer on the table';
    return;
  }
  rank.error = err instanceof Error ? err.message : String(err);
}

// Overlapping requests: a slow earlier answer must not land after a newer one.
let requestSeq = 0;

// Its own counter: `load()` alone must not discard the newest facets answer.
let facetSeq = 0;

export async function loadFacets(kind = rank.kind) {
  const mine = ++facetSeq;
  facets.genres = [];
  facets.decades = [];
  const found = await get(`/facets${qs({ kind: [kind] })}`).catch(() => null);
  if (mine !== facetSeq) return;          // a newer request has already answered
  if (found) {
    facets.genres = found.genres ?? [];
    facets.decades = found.decades ?? [];
  }
}

// An opened tier comes whole: the board sends its first `per_tier`, the tier route the rest.
async function withOpened(payload) {
  const tiers = await Promise.all(
    (payload.tiers ?? []).map(async (tier) => {
      if (!rank.expanded.includes(tier.index)) return tier;
      const entries = [...tier.entries];
      while (entries.length < tier.count) {
        const page = await get(
          `/rank/tier${qs({ ...query(), per_tier: undefined, index: tier.index, offset: entries.length, limit: 200 })}`
        );
        if (!page.entries?.length) break;
        entries.push(...page.entries);
      }
      return { ...tier, entries };
    })
  );
  return { ...payload, tiers };
}

export async function load(kind = rank.kind) {
  const mine = ++requestSeq;
  rank.kind = kind;
  rank.error = '';
  rank.reading += 1;
  try {
    const payload = await withOpened(await get(`/rank${qs(query())}`));
    if (mine !== requestSeq) return;      // a newer request has already answered
    apply(payload);
  } catch (err) {
    if (mine !== requestSeq) return;
    rank.loading = false;
    fail(err);
  } finally {
    rank.reading -= 1;
  }
}

// Live, as Home's search is: a phone fires `change` only on Enter or blur.
export const TYPING_PAUSE_MS = 220;
let typingTimer;

export function typed() {
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => load(rank.kind), TYPING_PAUSE_MS);
}

// Genre and decade are kind-scoped, so they clear on a kind switch; nothing else the person typed does.
export async function chooseKind(kind) {
  draft.genre = '';
  draft.decade = '';
  rank.expanded = [];
  rank.kind = kind;                       // the control flips on the tap; the board dims until it lands
  await Promise.all([loadFacets(kind), load(kind)]);
}

// One person's board, card and round must not carry into the next person's session; leaving the tab
// keeps the board, so Back lands where it was.
export function reset({ board = true } = {}) {
  if (board) {
    rank.tiers = [];
    rank.ratedTotal = 0;
    rank.setUp = true;
    rank.guessing = false;
    rank.straddling = 0;
  }
  rank.model = null;
  rank.opened = null;
  rank.expanded = [];
  rank.pair = null;
  rank.queueOpen = false;
  rank.roundAnswered = 0;
  rank.roundDone = false;
  rank.placed = [];
  rank.log = [];
  rank.error = '';
  rank.notice = '';
  rank.booted = false;
  rank.loading = true;
  clearTimeout(typingTimer);              // nor a read the last keystroke had scheduled
  requestSeq += 1;                        // and no in-flight response may land after this
}

const undoing = (edit) => (Number.isInteger(edit) ? { undoes: edit } : {});

// `above`/`below` are the titles it landed between; absent at a tier's ends. `via` and `undoes` are
// recorded as how the tier was chosen and which edit an Undo takes back (decision 534). Once written,
// the edit's id (true from a server that names none); false otherwise.
export async function drop({ title_id, tier, above = null, below = null, via = undefined, undoes = undefined }) {
  if (rank.busy) return false;            // two drops in flight would race their two boards
  rank.busy = true;
  rank.error = '';
  rank.notice = '';
  requestSeq += 1;                        // a read started before the drop must not paint over it
  try {
    const body = { title_id, tier, above, below, ...(via ? { via } : {}), ...undoing(undoes) };
    const res = await post(`/rank/drop${qs(query())}`, body);
    apply(await withOpened(res));
    return res?.tier_edit_id ?? true;
  } catch (err) {
    fail(err);
    return false;
  } finally {
    rank.busy = false;
  }
}

/** Its own tier, and no spot or the one it holds: nothing moved, so nothing is written. */
export function stays(entry, tier, above = null, below = null) {
  const from = rank.tiers.find((t) => t.index === entry.tier)?.entries ?? [];
  const at = from.findIndex((e) => e.title_id === entry.title_id);
  const held = [from[at - 1]?.title_id ?? null, at < 0 ? null : (from[at + 1]?.title_id ?? null)];
  return tier === entry.tier && ((!above && !below) || (above === held[0] && below === held[1]));
}

/** A drag or the tier sheet (decision 528): the drop, then a toast whose Undo takes the tier back.
 *  The sheet passes `via: 'explicit'`. */
export async function move(entry, tier, above = null, below = null, via = undefined) {
  if (stays(entry, tier, above, below)) return false;
  haptic();
  const edit = await drop({ title_id: entry.title_id, tier, above, below, via });
  if (!edit) return false;
  const label = rank.tiers.find((t) => t.index === tier)?.label ?? '';
  // Undo names no neighbours: comparisons the person never made are never written.
  const undo = tier === entry.tier ? null : { label: 'Undo', run: () => drop({ title_id: entry.title_id, tier: entry.tier, undoes: edit }) };
  showToast(`${entry.name} moved to ${label}`, undo);
  return true;
}

/** The tier sheet on a title card off Rank (decision 531): the same drop, replacing no board. A
 *  first placement has no tier to go back to, so it offers no Undo. */
export async function cardMove(entry, tier) {
  if (tier.index === entry.tier) return false;
  haptic();
  const to = async (index, extra) => {
    try {
      const res = await post(`/rank/drop?kind=${entry.kind}&per_tier=1`, { title_id: entry.title_id, tier: index, ...extra });
      return res?.tier_edit_id ?? true;
    } catch (err) {
      showToast(`Could not move ${entry.name} — ${err.message}`);
      return false;
    }
  };
  const edit = await to(tier.index, { via: 'explicit' });
  if (!edit) return false;
  const first = entry.tier == null;
  showToast(
    first ? `${entry.name} placed in ${tier.label}` : `${entry.name} moved to ${tier.label}`,
    first ? null : { label: 'Undo', run: () => to(entry.tier, undoing(edit)) }
  );
  return true;
}

/** "+N" opens a tier in place; the read fetches the rest of it. */
export function showAll(index) {
  if (!rank.expanded.includes(index)) rank.expanded = [...rank.expanded, index];
  return load(rank.kind);
}

export function showLess(index) {
  rank.expanded = rank.expanded.filter((i) => i !== index);
}

export function openTitle(entry) {
  rank.opened = entry.title_id;
}

export function closeTitle() {
  rank.opened = null;
}

/** The entries a drop at `at` lands between, counted without the title itself; no `at` is the tier
 *  alone, which names nobody, since a named neighbour writes a duel nobody answered (§4.2). */
export function neighboursAt(tierIndex, title, at = null) {
  if (at === null) return { above: null, below: null };
  const entries = (rank.tiers.find((t) => t.index === tierIndex)?.entries ?? []).filter(
    (e) => e.title_id !== title.title_id
  );
  return { above: entries[at - 1] ?? null, below: entries[at] ?? null };
}

/** The drag's label and the live region's sentence for a spot (decision 528). */
export function spot(label, { above, below }) {
  if (above && below) {
    const between = `between ${above.name} and ${below.name}`;
    return { chip: `${label} · ${between}`, said: `In ${label}, ${between}.` };
  }
  const edge = below ? 'top' : above ? 'bottom' : null;
  if (!edge) return { chip: label, said: `In ${label}.` };
  return { chip: `${edge} of ${label}`, said: `At the ${edge} of ${label}.` };
}

// Every chip opens the queue, so a second call while it is open is not a new round.
export async function openQueue() {
  if (rank.queueOpen) return;
  rank.queueOpen = true;
  startRound();
  await nextPair();
}

export function closeQueue() {
  rank.queueOpen = false;
  rank.pair = null;
  startRound();
}

function startRound() {
  rank.roundAnswered = 0;
  rank.roundDone = false;
  rank.placed = [];
}

export function keepGoing() {
  startRound();
}

export function roundLine() {
  const slot = rank.roundDone ? ROUND_SIZE : Math.min(rank.roundAnswered + 1, ROUND_SIZE);
  return `${slot} of ${ROUND_SIZE} this round`;
}

export async function nextPair() {
  rank.busy = true;
  try {
    const payload = await get(`/rank/queue${qs({ kind: rank.kind })}`);
    rank.pair = payload.pair ?? null;
    rank.queueReason = payload.reason ?? '';
  } catch (err) {
    fail(err);
  } finally {
    rank.busy = false;
  }
}

// Only the newest answer's §6.7 line survives its board re-read.
let answerSeq = 0;

// The token carries the pair and its sealed arm; this module never names an arm (§13).
export async function answer(outcome, decisive = false) {
  // Not the fix for a double answer (the route's lock is); this only stops a double tap here.
  if (!rank.pair || rank.busy) return;
  const mine = ++answerSeq;
  rank.busy = true;
  rank.pending = `duel-${outcome}`;
  rank.notice = '';
  let line;
  try {
    const payload = await post('/rank/queue/answer', {
      pair: rank.pair.token,
      outcome,
      decisive
    });
    rank.pair = payload.pair ?? null;
    rank.queueReason = payload.reason ?? '';
    // Every accepted answer counts, whichever sealed arm drew it.
    rank.roundAnswered += 1;
    if (rank.roundAnswered >= ROUND_SIZE) rank.roundDone = true;
    rank.placed = payload.placed ?? [];
    line = payload.log ?? [];
  } catch (err) {
    fail(err);
    if (err instanceof ApiError && err.status === 409) {
      // A refused seal is permanent for this token, so drop the pair and re-read the queue.
      rank.pair = null;
      await nextPair();
    }
    return;
  } finally {
    // The next pair answers while the board re-reads.
    rank.busy = false;
    rank.pending = null;
  }
  // Re-read the refitted board, then restore the log line that `apply()` blanks.
  await load(rank.kind);
  if (mine === answerSeq) rank.log = line;
}

/** The kind's own word for `n` titles: "film", "films", "series". */
export function nounFor(n) {
  const [one, many] = NOUNS[rank.kind] ?? NOUNS.movie;
  return n === 1 ? one : many;
}

/** The search field carries the count (decision 528): "Search 612 rated films". */
export function searchHint() {
  if (!rank.booted || rank.ratedTotal === 0) return 'Search your ranking';
  return `Search ${rank.ratedTotal} rated ${nounFor(rank.ratedTotal)}`;
}

/** What the Filters control holds, as removable chips; the title search keeps its own box. */
export function filterChips() {
  const chips = [];
  if (draft.genre) chips.push({ key: 'genre', text: draft.genre });
  if (draft.decade) chips.push({ key: 'decade', text: `${draft.decade}s` });
  if (draft.runtime_max) {
    const limit = runtimeLabel({ runtime_min: Number(draft.runtime_max), kind: rank.kind });
    chips.push({ key: 'runtime_max', text: `Up to ${limit}` });
  }
  if (draft.seen !== 'any') chips.push({ key: 'seen', text: draft.seen === 'seen' ? 'Seen' : 'Not seen' });
  if (draft.dna) chips.push({ key: 'dna', text: draft.dna });
  return chips;
}

export function clearFilter(key) {
  draft[key] = key === 'seen' ? 'any' : '';
  return load(rank.kind);
}

/** Proposal 80's two states and decision 209's, decided from one payload so two cannot render. */
export function emptyState() {
  // Nothing is claimed before the board has been read, or after it failed.
  if (rank.loading || !rank.booted || rank.error) return null;
  // Before the set-up its own card is the way on (Rate is closed); only a filter can still miss.
  if (!rank.setUp) return rank.rated === 0 && rank.ratedTotal > 0 ? noMatch() : null;
  // Before the counting states: an owed fit is why the count reads 0, and no duration is promised.
  if (rank.ratedTotal === 0 && rank.fitting) {
    return {
      kind: 'fitting',
      text: 'Nothing is missing — your first tiers are still being worked out. They appear here shortly.',
      cta: 'Rate some titles'
    };
  }
  // A young board says nothing here: Needs a look's guess line speaks for it (decision 550).
  if (rank.ratedTotal === 0) {
    return {
      kind: 'unrated',
      text: `Your tiers fill in as you place ${nounFor(2)} on Rate.`,
      cta: 'Rate some titles'
    };
  }
  return rank.rated === 0 ? noMatch() : null;
}

function noMatch() {
  const onlySearch = Object.keys(rank.filters).every((key) => key === 'q');
  return {
    kind: 'no-match',
    text:
      onlySearch && rank.filters.q
        ? `Nothing on your list matches “${rank.filters.q}”.`
        : 'Nothing on your list matches these filters.',
    cta: 'Clear filters'
  };
}

export function clearFilters() {
  draft.q = '';
  draft.genre = '';
  draft.decade = '';
  draft.runtime_max = '';
  draft.seen = 'any';
  draft.dna = '';
  return load(rank.kind);
}
