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
  rank.fitting = payload.fitting ?? false;
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
 * §4.1 rule 5's switch, as one event rather than two.
 *
 * The genre and decade vocabularies are scoped to the kind (`loadFacets` asks for one kind), so a
 * value carried over from the kind you just left stays in the query, renders blank in a `<select>`
 * that no longer offers it, and the board comes back empty with a filter the person cannot see.
 * Home's `toggleKind` (`routes/+page.svelte`) clears exactly these two for exactly this reason.
 * `q`, `dna`, `runtime_max` and `seen` are not kind-scoped, and a kind switch is no reason to
 * throw away what somebody typed.
 *
 * It lives here rather than in the page because a kind change and a filter change are different
 * events and only one of them resets anything — `load()` must stay the filter change, which is
 * why this is not folded into it — and because this is the layer a test can reach. [finding 27]
 */
export async function chooseKind(kind) {
  draft.genre = '';
  draft.decade = '';
  await loadFacets(kind);
  await load(kind);
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
 * new title goes above it and below whatever was above it. Absent — a tap, or a drop on the row
 * rather than on a poster — the gesture is §6.3's other case, "dropping a title into a tier
 * emits a `tier_edit`", and it names no neighbour at all.
 *
 * It used to name the tier's current last entry there, and `rank/drop.py` then wrote
 * `duel(title_a=<last>, title_b=<dropped>, outcome='A')` — a comparison the person never made,
 * in the same direction every time, on every promotion into a non-empty tier. §6.3 makes
 * tap-to-tier the whole of the phone's input path, so on the primary form factor that was every
 * move; §4.2 is append-only, so every one of them was permanent; and §5.2 weighs it like a duel
 * somebody answered. A position this tier does not hold is the same fabrication by another route
 * — a stale board from a second tab or a read that predates a refit — so it names nobody either.
 * The route still accepts one neighbour, because a pointer drop onto the last poster of a tier is
 * a genuine end-of-tier insert and honestly names exactly one. [§6.3, §4.2, §5.2; finding 17]
 */
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
  // `rank.busy` is the guard `drop()` above and `rate.svelte.js`'s `send()` both take, and it is
  // explicitly NOT the fix for the double answer: two tabs, two devices, or a tap that outruns
  // this module still reach the route together, and only the advisory lock the route takes inside
  // its transaction can make the loser the 409 `fail()` renders as a notice rather than a second
  // `duel` row that §4.2 keeps forever. What this line buys is that a double tap on one surface
  // stops being the easy way to get there. [§6.3, §4.2; finding 1]
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
    const line = payload.log ?? [];
    // §6.3: "The model refits (incremental immediately)". The board behind the queue has moved,
    // so it is re-read rather than left showing the ranking from before the answer — and the
    // line is restored afterwards, because the board GET carries no `log` and `apply()` would
    // otherwise blank the one §6.7 line this write produced.
    await load(rank.kind);
    rank.log = line;
  } catch (err) {
    fail(err);
    if (err instanceof ApiError && err.status === 409) {
      // The refusal is permanent for this token, not transient. The seal the route checks is a
      // count of this user's `tier_queue` duels and `duel` is append-only (§4.2), so once the
      // winner's row has landed the count never returns to the sealed value — every further tap on
      // these two posters is refused the same way. Rendered as a notice and nothing else, the
      // losing device sat on a dead pair with no control that said so: `closeQueue` and a reopen
      // were the only way out. So the pair comes off the table and the queue is re-read, which is
      // what `rate.svelte.js`'s stale branch does at the same seam ("the card moved on without us.
      // Say so and re-read the table rather than guessing"). Here rather than in `fail()` because
      // `drop()` shares that helper and carries no seal — decision 202 leaves it without a 409 —
      // so clearing the pair there would be a recovery from a refusal that cannot happen.
      // [§6.3, §4.2; cycle 1 m410-rev-03]
      rank.pair = null;
      await nextPair();
    }
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

/** Why Sharpen is disabled, decided here rather than in the template so it can be tested.
 *
 * Decision 35's rule, generalised by the template this replaces: a control that disables has to
 * say why, or the person reads a dead button as a broken one. It said "the queue draws from titles
 * you have rated" for every `ratedTotal < 2`, which during decision 209's window is said to
 * somebody who has rated ten — the same sentence for both states, which is the reading
 * `emptyState` above was changed to stop making. The cause is the owed fit, so name it.
 */
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
      text: `${rank.ratedTotal} rated - nothing straddles a boundary right now, so the queue is exploring rather than settling one.`
    };
  }
  return null;
}

/** Proposal 80's two states and decision 209's, decided from one payload so two cannot render. */
export function emptyState() {
  // Nothing is claimed before the board has been read. "You're at 0" is a statement about the
  // person's ledger, and asserting it while the request is still in flight — or after it
  // failed, with the error banner right above — is §6.8's register saying something untrue.
  if (rank.loading || !rank.booted || rank.error) return null;
  // Decision 209, and before the two counting states because it is the reason the count is 0.
  // §6.3's board reads the fit, §5.3 puts the first fit on the 60 s sweep rather than in the
  // request, and in between "you're at 0" is the ledger statement above made about somebody who
  // has just rated ten films. `fitting` is the server's stamp, so the claim here is only that a
  // fit is owed — no duration, because the sweep's period is not a promise this copy can keep.
  if (rank.ratedTotal === 0 && rank.fitting) {
    return {
      kind: 'fitting',
      text: 'Nothing is missing — your first tiers are still being fitted. They appear here shortly.',
      cta: 'Rate some titles'
    };
  }
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
