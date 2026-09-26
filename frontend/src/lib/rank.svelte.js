// The board always comes from the server and is replaced whole (§6.3 forbids snapping back); a
// cancelled lift writes nothing; the queue's pair is a sealed token this module never opens (§13).

import { ApiError, get, post, qs } from '$lib/api.js';

export const KIND_LABELS = { movie: 'Films', series: 'Series' };

// How to use the board, true on every path; what a move writes is the rail's to say.
export const TAP_FOOTNOTE =
  'tap a title to open it · tap Move to pick it up, then tap a tier to drop it';

export const SHARPEN_LABEL = 'sharpen my ranking';

// Counted here from taps: a server count would stand still on a held-out answer and reveal it (§13).
export const ROUND_SIZE = 15;

export const ROUND_END_TEXT =
  `That's ${ROUND_SIZE} comparisons for this round. You can stop here — or keep going for another ${ROUND_SIZE}.`;

// The two DNA tiers stay distinguishable (§4.1 rule 1), named by what each is to a member.
export const DNA_TIER_LABELS = { extracted: 'quoted', projected: 'inferred' };

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
  queueEligible: 0,
  why: '',
  /** @type {Record<string, any>} what the person has switched on */
  filters: {},
  /** @type {Record<string, string[]> | null} §4.1 rule 1: which tier matched each survivor */
  dnaTiers: null,
  /** @type {any} §6.7, present only when the model-log toggle is on */
  model: null,
  /** @type {string[]} §6.7's lines for the last write */
  log: [],
  /** @type {null | {title_id:number, name:string}} the lifted title, on phones */
  lifted: null,
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
  rank.queueEligible = payload.queue_eligible ?? 0;
  rank.why = payload.why ?? '';
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

// A lift is a pending write naming a bare title id, so it must not survive a sign-out.
export function reset() {
  rank.lifted = null;
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

// `above`/`below` are the titles it landed between; absent at a tier's ends.
export async function drop({ title_id, tier, above = null, below = null }) {
  if (rank.busy) return;                  // two drops in flight would race their two boards
  rank.busy = true;
  rank.error = '';
  rank.notice = '';
  try {
    apply(await post(`/rank/drop${qs(query())}`, { title_id, tier, above, below }));
    rank.lifted = null;
  } catch (err) {
    fail(err);
  } finally {
    rank.busy = false;
  }
}

export function lift(entry) {
  rank.lifted = rank.lifted?.title_id === entry.title_id ? null : entry;
}

/** The other way out — the banner's Cancel. Writes nothing, by construction. */
export function putDown() {
  rank.lifted = null;
}

// Opening puts a lifted title down first, so a tap behind the card cannot drop it.
export function openTitle(entry) {
  rank.lifted = null;
  rank.opened = entry.title_id;
}

export function closeTitle() {
  rank.opened = null;
}

// With a title lifted, a tap on another title drops it into that tier, naming no neighbour.
export function tapTile(entry, tierIndex) {
  if (!rank.lifted) {
    openTitle(entry);
    return Promise.resolve();
  }
  if (rank.lifted.title_id === entry.title_id) {
    putDown();
    return Promise.resolve();
  }
  return dropLifted(tierIndex);
}

export function dropLifted(tierIndex) {
  if (!rank.lifted) return Promise.resolve();
  const title = rank.lifted;
  return drop({ title_id: title.title_id, tier: tierIndex, ...neighboursIn(tierIndex, title) });
}

// A drop on a poster lands above it and names both neighbours; a tap or a drop on the row names
// none, since a named neighbour writes a duel nobody answered (§4.2 keeps it). A stale position
// names nobody either.
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
  rank.lifted = null;
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

// Name the active filters with their values, not just the fields.
const FILTER_LABELS = {
  q: 'search',
  genre: 'genre',
  decade: 'decade',
  runtime_max: 'under',
  runtime_min: 'over',
  seen: 'seen state',
  // "tag", not "DNA term": the member register's word for a vocabulary term (decision 486).
  dna: 'tag'
};

export function activeFilterText() {
  const parts = Object.entries(rank.filters).map(([key, value]) => {
    const label = FILTER_LABELS[key] ?? key;
    if (key === 'runtime_max' || key === 'runtime_min') return `${label} ${value} min`;
    return `${label} ${value}`;
  });
  return parts.join(', ') || 'these filters';
}

// A disabled control says why; during the first fit the cause is the owed fit, not the count.
export function sharpenWhy() {
  if (!rank.booted) return null;
  if (rank.ratedTotal === 0 && rank.fitting) {
    return { kind: 'fitting', text: 'Nothing to compare until your first tiers are fitted.' };
  }
  if (rank.ratedTotal < 2) {
    return { kind: 'thin', text: 'Nothing to compare yet - the queue draws from titles you have rated.' };
  }
  if (rank.queueEligible === 0) {
    return {
      kind: 'exploring',
      text: `${rank.ratedTotal} rated - no title is a close call between two tiers right now, so the pairs explore your board instead.`
    };
  }
  return null;
}

/** Proposal 80's two states and decision 209's, decided from one payload so two cannot render. */
export function emptyState() {
  // Nothing is claimed before the board has been read, or after it failed.
  if (rank.loading || !rank.booted || rank.error) return null;
  // Before the counting states: an owed fit is why the count reads 0, and no duration is promised.
  if (rank.ratedTotal === 0 && rank.fitting) {
    return {
      kind: 'fitting',
      text: 'Nothing is missing — your first tiers are still being fitted. They appear here shortly.',
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
    return {
      kind: 'no-match',
      text: `Nothing matches ${activeFilterText()}.`,
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
