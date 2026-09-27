// The board always comes from the server and is replaced whole (§6.3 forbids snapping back); the
// queue's pair is a sealed token this module never opens (§13).

import { ApiError, get, post, qs } from '$lib/api.js';
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

// Proposal 80's handoff figure for a board worth trusting.
export const TIER_THRESHOLD = 30;

/** §6.3's genre and decade vocabularies, scoped to the kind on screen (as Home scopes them). */
export const facets = $state({ genres: [], decades: [] });

export const rank = $state({
  loading: true,
  booted: false,
  busy: false,
  error: '',
  notice: '',
  kind: 'movie',
  /** @type {string[]} */
  tierSet: [],
  /** @type {any[]} one entry per tier, best-first, empty tiers kept (proposal 82) */
  tiers: [],
  rated: 0,
  ratedTotal: 0,
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

export async function load(kind = rank.kind) {
  const mine = ++requestSeq;
  rank.kind = kind;
  rank.error = '';
  try {
    const payload = await get(`/rank${qs(query())}`);
    if (mine !== requestSeq) return;      // a newer request has already answered
    apply(payload);
  } catch (err) {
    if (mine !== requestSeq) return;
    rank.loading = false;
    fail(err);
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
  await loadFacets(kind);
  await load(kind);
}

// One person's board, card and round must not carry into the next person's session.
export function reset() {
  rank.opened = null;
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

// `above`/`below` are the titles it landed between; absent at a tier's ends. True once written.
export async function drop({ title_id, tier, above = null, below = null }) {
  if (rank.busy) return false;            // two drops in flight would race their two boards
  rank.busy = true;
  rank.error = '';
  rank.notice = '';
  try {
    apply(await post(`/rank/drop${qs(query())}`, { title_id, tier, above, below }));
    return true;
  } catch (err) {
    fail(err);
    return false;
  } finally {
    rank.busy = false;
  }
}

/** Move's action sheet (decision 527): the tier it names, naming no neighbour. */
export async function moveTo(entry, tier) {
  if (tier === entry.tier) return;        // the checked row: it is there already
  const label = rank.tiers.find((t) => t.index === tier)?.label ?? '';
  if (await drop({ title_id: entry.title_id, tier })) showToast(`${entry.name} — moved to ${label}`);
}

export function openTitle(entry) {
  rank.opened = entry.title_id;
}

export function closeTitle() {
  rank.opened = null;
}

// A drop on a title lands above it and names both neighbours; a drop into the tier names none,
// since a named neighbour writes a duel nobody answered (§4.2 keeps it). A stale position names
// nobody either.
export function neighboursIn(tierIndex, title, beforeTitleId = null) {
  const tier = rank.tiers.find((t) => t.index === tierIndex);
  const entries = (tier?.entries ?? []).filter((e) => e.title_id !== title.title_id);
  const at =
    beforeTitleId === null || beforeTitleId === title.title_id
      ? -1
      : entries.findIndex((e) => e.title_id === beforeTitleId);
  if (at < 0) return { above: null, below: null };
  return {
    above: at > 0 ? entries[at - 1].title_id : null,
    below: entries[at].title_id
  };
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

// The token carries the pair and its sealed arm; this module never names an arm (§13).
export async function answer(outcome, decisive = false) {
  // Not the fix for a double answer (the route's lock is); this only stops a double tap here.
  if (!rank.pair || rank.busy) return;
  rank.busy = true;
  rank.notice = '';
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
    const line = payload.log ?? [];
    // Re-read the refitted board, then restore the log line that `apply()` blanks.
    await load(rank.kind);
    rank.log = line;
  } catch (err) {
    fail(err);
    if (err instanceof ApiError && err.status === 409) {
      // A refused seal is permanent for this token, so drop the pair and re-read the queue.
      rank.pair = null;
      await nextPair();
    }
  } finally {
    rank.busy = false;
  }
}

/** The count under the title: "70 films · best first", or "12 of 70 films" while filtered. */
export function countLine() {
  if (!rank.booted || rank.ratedTotal === 0) return '';
  const [one, many] = NOUNS[rank.kind] ?? NOUNS.movie;
  const shown = rank.rated === rank.ratedTotal ? rank.rated : `${rank.rated} of ${rank.ratedTotal}`;
  return `${shown} ${rank.ratedTotal === 1 ? one : many} · best first`;
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
  // Before the counting states: an owed fit is why the count reads 0, and no duration is promised.
  if (rank.ratedTotal === 0 && rank.fitting) {
    return {
      kind: 'fitting',
      text: 'Nothing is missing — your first tiers are still being worked out. They appear here shortly.',
      cta: 'Rate some titles'
    };
  }
  // The board shows tiers from the first rated title; what 30 buys is a board worth trusting.
  if (rank.ratedTotal === 0) {
    return {
      kind: 'unrated',
      text: `Your tiers fill in as you rate titles — about ${TIER_THRESHOLD} makes a good start, and you're at 0.`,
      cta: 'Rate some titles'
    };
  }
  if (rank.ratedTotal < TIER_THRESHOLD) {
    return {
      kind: 'thin',
      text: `These tiers are a first guess until you've rated about ${TIER_THRESHOLD} titles — you're at ${rank.ratedTotal}.`,
      cta: 'Rate some titles'
    };
  }
  if (rank.rated === 0) {
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
  return null;
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

// Tension takes precedence over the straddle badge (proposal 71).
export function chipFor(entry) {
  if (entry.tension) return { kind: 'tension', text: entry.tension };
  if (entry.straddle_badge) return { kind: 'straddle', text: entry.straddle_badge };
  return null;
}
