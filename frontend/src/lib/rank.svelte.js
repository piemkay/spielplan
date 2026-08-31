/**
 * The Rank surface's client. Spec v2.1 §6.3, §6.7, §6.8; proposals 71–83, 157.
 *
 * Three rules this module encodes, each of which is a way the surface could quietly stop
 * obeying §6.3:
 *
 *   * **The tier a title renders in comes from the server.** §6.3 forbids snapping back, and
 *     the way a client breaks that is by re-sorting optimistically and then reconciling — the
 *     title lands where you dropped it, the response arrives, and it slides somewhere else.
 *     So a drop shows a pending state and the board is replaced wholesale by the response.
 *   * **The lift is cancellable, and cancelling writes nothing.** Proposal 74: "a modeless
 *     lift with an undiscoverable exit is the classic tap-to-move failure". `putDown()` is not
 *     a request; it is the absence of one.
 *   * **The queue's pair is opaque.** §13's guard needs the held-out tenth to be identifiable
 *     end to end, so the arm lives inside a server-sealed token and this module never reads,
 *     reconstructs or sends one. `answer()` posts the token back and nothing else.
 */

import { ApiError, get, post, qs } from '$lib/api.js';

/** §4.1 rule 5's partition, in the words the surface uses. */
export const KIND_LABELS = { movie: 'Films', series: 'Series' };

/**
 * Proposal 75's standing footnote, amended to be true.
 *
 * The proposal's own wording is "each move writes a tier_edit plus two duels", and §6.3 does
 * say a drop *between two titles* writes two. A drop at the end of a tier has one neighbour and
 * writes one, and the tap path has no position inside the row at all — so the proposal's
 * sentence is a claim this surface cannot always honour. Copy that overstates a write is worse
 * than copy that is vaguer: §6.8's register is "quiet reasons", not confident ones.
 */
export const TAP_FOOTNOTE =
  'tap a poster to pick it up, tap a tier to drop · each move writes a tier_edit plus a duel ' +
  'against each new neighbour';

/** §6.3's control, by the name §6.3 gives it. */
export const SHARPEN_LABEL = 'sharpen my ranking';

/**
 * Proposal 80's second state. §6.3's queue estimate — "~10–20 comparisons place a new title" —
 * is the nearest measured number to "enough to tier", and §6.1's own target is 50–100 verdicts
 * in the first sitting or two; 30 is the spec's own handoff figure in proposal 80's copy.
 */
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
  /** @type {any} the comparison queue's current pair, or null */
  pair: null,
  queueReason: '',
  queueOpen: false
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
  rank.queueEligible = payload.queue_eligible ?? 0;
  rank.why = payload.why ?? '';
  rank.filters = payload.filters ?? {};
  rank.dnaTiers = payload.dna_tiers ?? null;
  // Absent when decision 117's toggle is off — `rail.redact` deletes the key rather than
  // emptying it, so `?? null` is reading an absence and not a value.
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

/**
 * Filter and kind changes fire overlapping requests; without a sequence number a slow earlier
 * response lands after a fast later one and the board shows the wrong kind under the other
 * tab. `routes/+page.svelte` carries the same guard with the same comment — this surface has
 * three triggers (the tabs, four filter `onchange`s, and the trailing read after a queue
 * answer), so it needs it more, not less.
 */
let requestSeq = 0;

export async function loadFacets(kind = rank.kind) {
  facets.genres = [];
  facets.decades = [];
  const found = await get(`/facets${qs({ kind: [kind] })}`).catch(() => null);
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

/**
 * Drop everything this module holds. The house convention (`rate.svelte.js`) and the reason
 * Rank needs it more: a lift is a *pending write naming a bare title id*, so a lift carried
 * across a sign-out into the next person's session would post a `tier_edit` into their
 * append-only ledger. `+layout.svelte`'s logout calls this; so does the page on destroy.
 */
export function reset() {
  rank.lifted = null;
  rank.pair = null;
  rank.queueOpen = false;
  rank.log = [];
  rank.error = '';
  rank.notice = '';
  rank.booted = false;
  rank.loading = true;
  requestSeq += 1;                        // and no in-flight response may land after this
}

/**
 * §6.3's drop, from either input path. `above` and `below` are the titles it landed between —
 * absent at the ends of a tier, which is one duel rather than a refusal.
 */
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

/** Proposal 74: the lift is a mode, so it needs a visible way out. Re-tapping is one. */
export function lift(entry) {
  rank.lifted = rank.lifted?.title_id === entry.title_id ? null : entry;
}

/** The other way out — the banner's Cancel. Writes nothing, by construction. */
export function putDown() {
  rank.lifted = null;
}

/** Where a tap on a tier row lands: the lifted title, into that tier, between its neighbours. */
export function dropLifted(tierIndex) {
  if (!rank.lifted) return Promise.resolve();
  const title = rank.lifted;
  return drop({ title_id: title.title_id, tier: tierIndex, ...neighboursIn(tierIndex, title) });
}

/**
 * The two titles a drop lands between, in the tier it lands in.
 *
 * `beforeTitleId` is the poster the drop landed *on*, which is §6.3's "between two titles": the
 * new title goes above it and below whatever was above it. Absent — a tap, or a drop on the
 * row rather than on a poster — it lands at the bottom, where the one neighbour that exists is
 * the current last entry and §6.3's two duels become one. Inventing a second would put a
 * comparison in the Ledger nobody made.
 */
export function neighboursIn(tierIndex, title, beforeTitleId = null) {
  const tier = rank.tiers.find((t) => t.index === tierIndex);
  const entries = (tier?.entries ?? []).filter((e) => e.title_id !== title.title_id);
  if (!entries.length) return { above: null, below: null };
  if (beforeTitleId === null || beforeTitleId === title.title_id) {
    return { above: entries[entries.length - 1].title_id, below: null };
  }
  const at = entries.findIndex((e) => e.title_id === beforeTitleId);
  if (at < 0) return { above: entries[entries.length - 1].title_id, below: null };
  return {
    above: at > 0 ? entries[at - 1].title_id : null,
    below: entries[at].title_id
  };
}

export async function openQueue() {
  rank.queueOpen = true;
  await nextPair();
}

export function closeQueue() {
  rank.queueOpen = false;
  rank.pair = null;
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

/**
 * One comparison. The token carries the pair *and the arm the server drew it under* — this
 * module never names an arm, because a client that could would decide which §13 stream a
 * comparison belonged to.
 */
export async function answer(outcome, decisive = false) {
  if (!rank.pair) return;
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
    const line = payload.log ?? [];
    // §6.3: "The model refits (incremental immediately)". The board behind the queue has moved,
    // so it is re-read rather than left showing the ranking from before the answer — and the
    // line is restored afterwards, because the board GET carries no `log` and `apply()` would
    // otherwise blank the one §6.7 line this write produced.
    await load(rank.kind);
    rank.log = line;
  } catch (err) {
    fail(err);
  } finally {
    rank.busy = false;
  }
}

/**
 * Proposal 80's "no match" state has to name *what* matched nothing, values and all: "the
 * filter matched nothing — say so, with the active filters listed". A message naming only the
 * fields ("nothing matches dna") tells the person which control to look at and not what it
 * currently says, which on a surface with six of them is most of the answer missing.
 */
const FILTER_LABELS = {
  q: 'search',
  genre: 'genre',
  decade: 'decade',
  runtime_max: 'under',
  runtime_min: 'over',
  seen: 'seen state',
  dna: 'DNA term'
};

export function activeFilterText() {
  const parts = Object.entries(rank.filters).map(([key, value]) => {
    const label = FILTER_LABELS[key] ?? key;
    if (key === 'runtime_max' || key === 'runtime_min') return `${label} ${value} min`;
    return `${label} ${value}`;
  });
  return parts.join(', ') || 'these filters';
}

/** Proposal 80's two states, decided from one payload so they cannot both render. */
export function emptyState() {
  // Nothing is claimed before the board has been read. "You're at 0" is a statement about the
  // person's ledger, and asserting it while the request is still in flight — or after it
  // failed, with the error banner right above — is §6.8's register saying something untrue.
  if (rank.loading || !rank.booted || rank.error) return null;
  if (rank.ratedTotal === 0) {
    return {
      kind: 'unrated',
      text: `Tiers appear once you've rated about ${TIER_THRESHOLD} titles — you're at 0.`,
      cta: 'Rate some titles'
    };
  }
  if (rank.ratedTotal < TIER_THRESHOLD) {
    return {
      kind: 'thin',
      text: `Tiers appear once you've rated about ${TIER_THRESHOLD} titles — you're at ${rank.ratedTotal}.`,
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

/**
 * §6.3's badge, as one line. The straddle chip and the tension chip compete for the same corner
 * and proposal 71 gives tension precedence; the server has already decided which one exists, so
 * this only picks the string.
 */
export function chipFor(entry) {
  if (entry.tension) return { kind: 'tension', text: entry.tension };
  if (entry.straddle_badge) return { kind: 'straddle', text: entry.straddle_badge };
  return null;
}
