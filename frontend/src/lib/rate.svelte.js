// Writes name the server's `card_token`, never a title, and nothing here caches a model belief
// between cards (§6.1). The reveal hold is the one timing that is ours: the verdict response
// already carries the next card, so the swap costs no request.

import { ApiError, get, post, qs } from '$lib/api.js';
import { preloadPoster } from '$lib/art.js';

/** Proposal 42: "~1.2 s or until the next card". */
export const HOLD_MS = 1200;

export const KIND_LABELS = { movie: 'film', series: 'series' };

// [key, name, what it does]: the keys are the wire's and the tests', the names a member's.
export const MODES = [
  ['mix', 'Mixed', 'Single titles and pairs in turn — the pairs start at 15 ratings'],
  ['sweep', 'Singles', 'One title at a time — say how you liked it'],
  ['battle', 'Pairs', 'Two titles you rated the same way — pick the one you enjoyed more']
];

/** @param {string | null | undefined} mode */
export function modeName(mode) {
  return MODES.find(([key]) => key === mode)?.[1] ?? '';
}

export const PAIR_QUESTION = 'Which did you enjoy more?';

export const PAIR_SELECTION_COPY =
  'Pairs are picked at random from titles you rated the same way. For learning your taste, ' +
  'random works as well as anything cleverer. Choosing pairs cleverly only helps when the ' +
  "question is which of a few is best - that is what Sharpen my ranking on Rank and Tonight's " +
  'round do.';

export const LEARNING_CURVE_COPY =
  'Your suggestions get about three times more personal between 5 and 100 ratings. Aim for ' +
  '50-100 in your first sitting or two.';

// Words for the chip; `data-undo-kind` keeps the journal's raw kind.
export const UNDO_KIND_LABELS = {
  verdict: 'rating',
  not_seen: 'not seen',
  correction: 'not seen',
  skip: 'skip',
  duel: 'pick',
  tie: 'tie'
};

/** @param {string | null | undefined} kind */
export function undoKindLabel(kind) {
  if (!kind) return '';
  return UNDO_KIND_LABELS[kind] ?? kind.replace(/_/g, ' ');
}

// Counts are per kind (each kind is its own model), so a single-kind count names the kind.
export function ratingsLabel(n, kinds = []) {
  const count = Number(n) || 0;
  const noun = count === 1 ? 'rating' : 'ratings';
  const only = (kinds ?? []).length === 1 ? KIND_LABELS[kinds[0]] : null;
  return only ? `${count} ${only} ${noun}` : `${count} ${noun}`;
}

/** A class or verdict label as a member reads it: "liked" -> "Liked". */
export function sentenceCase(text) {
  const s = String(text ?? '');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** The upper end of §12's M2 exit criterion. */
export const LEARNING_TARGET = 100;

export const rate = $state({
  // True only until the first envelope lands: a later refresh must not blank the card on screen.
  loading: true,
  booted: false,
  busy: false,
  /**
   * Which answer is in flight (`verdict-2`, `duel-A`, `skip`...), so the tapped control says so.
   * @type {string | null}
   */
  pending: null,
  error: '',
  /** A refusal we can explain and recover from — a stale card, an Undo at the boundary. */
  notice: '',
  /** @type {null | {id:number,mode:string,kinds:string[],block:any}} */
  session: null,
  /** @type {any} the card on the table, or the card just answered while the reveal holds */
  card: null,
  /** The counter that belongs to `card` while a reveal is held; null otherwise. */
  frozenBlock: null,
  /** @type {any} the block an answer just finished, its own screen until the person moves on */
  done: null,
  holding: false,
  /** @type {null | {cause:string, text:string}} */
  drained: null,
  /** @type {any} `rate.balance.ClassBalance.as_dict()` */
  balance: null,
  undo: { available: false, kind: null, reason: 'empty' },
  /** @type {any} present only in the response to a verdict — never before the tap. */
  reveal: null,
  /** @type {any} §6.7, gated by the show_model preference at the render site. */
  ledger: null,
  /** @type {string[]} §6.7's lines for this write. */
  log: []
});

// Choosing a hit pins it with `head`, so the verdict is still given on §6.1's card.
export const finder = $state({
  q: '',
  /** @type {any[]} */
  items: [],
  busy: false,
  error: '',
  /** The query the items answer, so an empty list can say "nothing matched" honestly. */
  searched: ''
});

/** Below this many characters a search matches half the catalogue and helps nobody. */
export const FIND_MIN_CHARS = 2;

// The banner's pins, sent as repeated `?head=` parameters.
let head = [];
let pendingCard = null;
let holdTimer = null;
let shownAt = 0;
let findSeq = 0;

/** @param {(string|number)[]} ids */
export function setHead(ids) {
  // Title ids only: `Number(null)` is a finite 0, and a stray 0 is a 422.
  head = (ids ?? []).map(Number).filter((n) => Number.isInteger(n) && n > 0);
  return head;
}

export function pendingHead() {
  return [...head];
}

// The server serves a pin even over a skip, so an answered pin must leave or its card returns.
function consumePin(card) {
  const id = card?.type === 'sweep' ? card.title?.id : null;
  if (id != null && head.includes(id)) head = head.filter((t) => t !== id);
}

// The one runtime label: series per episode, and no zero hour or zero minutes.
export function runtimeLabel(title) {
  if (!title?.runtime_min) return null;
  if (title.kind === 'series') return `${title.runtime_min}m/ep`;
  const h = Math.floor(title.runtime_min / 60);
  const m = title.runtime_min % 60;
  return h && m ? `${h}h ${m}m` : h ? `${h}h` : `${m}m`;
}

// Built in JS: Svelte collapses the whitespace around an {#if}, gluing the separator.
export function metaLine(title) {
  return [title?.year ?? '—', runtimeLabel(title)].filter(Boolean).join(' · ');
}

/** A stable hue per title (FNV-1a), so the same film is the same colour everywhere. */
export function hueOf(text) {
  let x = 2166136261;
  const s = String(text ?? '');
  for (let i = 0; i < s.length; i++) {
    x ^= s.charCodeAt(i);
    x = Math.imul(x, 16777619);
  }
  return (x >>> 0) % 360;
}

export function sharePct(share) {
  return Math.round((Number(share) || 0) * 100);
}

export function undoMessage(undo) {
  if (!undo || undo.available) return '';
  if (undo.reason === 'block_boundary') {
    return 'Undo only goes back to the start of these 15';
  }
  return 'Nothing to undo yet';
}

/** The class the balance warning is about, as the server picks it: the most, the lowest on a tie. */
export function heavyClass(balance) {
  if (!balance?.warn) return '';
  const counts = balance.counts ?? [];
  return balance.labels?.[counts.indexOf(Math.max(...counts))] ?? '';
}

/** Before the balance check arms, the widget says when it will (decision 491). */
export function armingLine(balance) {
  const armsAt = balance?.arms_at ?? 0;
  if (balance?.warn || !armsAt || (balance?.total ?? 0) >= armsAt) return '';
  return `A balance check starts at ${armsAt} ratings.`;
}

// Before the first fit the reveal is suppressed with the server's reason, never banded (proposal 153).
export function revealLine(reveal) {
  if (!reveal) return null;
  if (reveal.available) return { available: true, text: reveal.text, agreed: !!reveal.agreed };
  return { available: false, text: reveal.reason ?? 'no prediction yet', agreed: false };
}

/** Milliseconds the card was on screen before the tap: §4.2's `latency_ms`. */
export function latency() {
  return shownAt ? Math.max(0, Date.now() - shownAt) : null;
}

// Warm the next card's art during the reveal hold, inside §6's per-card budget.
export function preloadArt(card) {
  return [card?.title, card?.left, card?.right].filter(Boolean).map(preloadPoster);
}

function apply(res, { holdReveal = false, answer = false } = {}) {
  const answeredCard = rate.card;
  const answeredBlock = rate.session?.block ?? null;
  const next = res.session?.block ?? null;

  // The fifteenth answer rolls the block; its end screen stays until an Undo takes it back.
  if (answer && answeredBlock && next && next.index > answeredBlock.index) rate.done = answeredBlock;
  else if (rate.done && !(next && next.index > rate.done.index)) rate.done = null;

  rate.session = res.session ?? null;
  rate.balance = res.class_balance ?? null;
  rate.undo = res.undo ?? { available: false, kind: null, reason: 'empty' };
  rate.ledger = res.ledger ?? null;
  rate.log = res.log ?? [];
  rate.drained = res.drained ?? null;
  consumePin(res.card);

  clearTimeout(holdTimer);
  if (holdReveal && res.reveal && answeredCard) {
    // The reveal belongs to the card just rated, so that card and its counter stay put.
    rate.reveal = res.reveal;
    rate.card = answeredCard;
    rate.frozenBlock = answeredBlock;
    rate.holding = true;
    pendingCard = res.card ?? null;
    preloadArt(pendingCard);
    holdTimer = setTimeout(commit, HOLD_MS);
    return;
  }
  rate.reveal = null;
  rate.holding = false;
  rate.frozenBlock = null;
  pendingCard = null;
  rate.card = res.card ?? null;
  shownAt = Date.now();
}

/** End the reveal hold early — "it clears on any subsequent action" (proposal 42). */
export function commit() {
  if (!rate.holding) return false;
  clearTimeout(holdTimer);
  rate.holding = false;
  rate.reveal = null;
  rate.frozenBlock = null;
  rate.card = pendingCard;
  pendingCard = null;
  shownAt = Date.now();
  return true;
}

const STALE = new Set(['stale_card', 'no_card', 'wrong_card_type']);

async function onError(err) {
  const detail = err instanceof ApiError ? err.detail : null;
  const reason = detail && typeof detail === 'object' ? detail.reason : null;
  const message = (detail && typeof detail === 'object' && detail.message) || err.message;
  if (reason && STALE.has(reason)) {
    // The card moved on without us. Say so and re-read the table rather than guessing.
    rate.notice = message;
    await load({ quiet: true });
    return;
  }
  if (reason === 'empty' || reason === 'block_boundary') {
    rate.notice = undoMessage({ available: false, reason });
    await load({ quiet: true });
    return;
  }
  rate.error = message || 'something went wrong';
}

async function send(fn, { holdReveal = false, pending = null, answer = false } = {}) {
  if (rate.busy) return;
  rate.busy = true;
  rate.pending = pending;
  rate.notice = '';
  rate.error = '';
  try {
    const res = await fn();
    if (res) apply(res, { holdReveal, answer });
  } catch (err) {
    await onError(err);
  } finally {
    rate.busy = false;
    rate.pending = null;
  }
}

/** Open or resume and serve the card. Idempotent — a second GET returns the same card. */
export async function load({ quiet = false } = {}) {
  if (!quiet && !rate.booted) rate.loading = true;
  try {
    apply(await get(`/rate${qs({ head })}`));
    rate.error = '';
  } catch (err) {
    rate.error = err.message || 'could not open a rating session';
  } finally {
    rate.loading = false;
    rate.booted = true;
  }
}

const controls = (body) => post('/rate/session', { ...body, head });

/** Proposal 36: mode is sticky per user only after an explicit change. This is that change. */
export const setMode = (mode) => send(() => controls({ mode }));

export const setKinds = (kinds) => send(() => controls({ kinds }));

export const restart = () => send(() => controls({ restart: true }));

export function verdict(value) {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(
    () => post('/rate/verdict', { card_token: token, value, latency_ms: latency(), head }),
    { holdReveal: true, pending: `verdict-${value}`, answer: true }
  );
}

export function notSeen() {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(() => post('/rate/not-seen', { card_token: token, latency_ms: latency(), head }), {
    pending: 'not_seen',
    answer: true
  });
}

export function skip() {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(() => post('/rate/skip', { card_token: token, latency_ms: latency(), head }), {
    pending: 'skip',
    answer: true
  });
}

/**
 * The five-step answer: "Much more" is `decisive`, and the server never weights a TIE.
 *
 * @param {'A'|'B'|'TIE'} outcome
 */
export function duel(outcome, decisive = false) {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  const body = { card_token: token, outcome, decisive, latency_ms: latency(), head };
  return send(() => post('/rate/duel', body), {
    pending: `duel-${outcome}${decisive ? '-much' : ''}`,
    answer: true
  });
}

/** Not seen under one film of a pair: no duel row, and the counter does not move. */
export function correct(side) {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(() => post('/rate/correction', { card_token: token, side }), {
    pending: `correction-${side}`
  });
}

// Restores the exact card, even one taken during a reveal hold, so the hold is cleared, not committed.
export function undo() {
  clearTimeout(holdTimer);
  rate.holding = false;
  rate.reveal = null;
  rate.frozenBlock = null;
  pendingCard = null;
  return send(() => post('/rate/undo', {}));
}

/** Leave a block's end screen for the card the fifteenth answer already brought. */
export function continueRating() {
  rate.done = null;
  shownAt = Date.now();
}

/** A stray hold timer would fire into a destroyed component. */
export function reset() {
  clearTimeout(holdTimer);
  holdTimer = null;
  pendingCard = null;
  rate.holding = false;
  rate.reveal = null;
  rate.frozenBlock = null;
  rate.done = null;
  rate.pending = null;
  clearFinder();
}

export function clearFinder() {
  findSeq++;
  finder.q = '';
  finder.items = [];
  finder.busy = false;
  finder.error = '';
  finder.searched = '';
}

// A sequence number per keystroke, so a slow "he" never overwrites "heat".
export async function findTitles(q) {
  finder.q = q ?? '';
  const query = finder.q.trim();
  const seq = ++findSeq;
  if (query.length < FIND_MIN_CHARS) {
    finder.items = [];
    finder.searched = '';
    finder.error = '';
    finder.busy = false;
    return finder.items;
  }
  finder.busy = true;
  try {
    const res = await get(`/rate/search${qs({ q: query })}`);
    if (seq !== findSeq) return finder.items;
    finder.items = res?.items ?? [];
    finder.searched = query;
    finder.error = '';
  } catch (err) {
    if (seq === findSeq) finder.error = err.message || 'the search did not answer';
  } finally {
    if (seq === findSeq) finder.busy = false;
  }
  return finder.items;
}

// A pin the server could not serve says so rather than leaving the old card up.
export async function rateTitle(item) {
  if (!item || item.rated || rate.busy) return false;
  setHead([item.id]);
  clearFinder();
  rate.notice = '';
  await load({ quiet: true });
  const served = rate.card?.type === 'sweep' && rate.card?.title?.id === item.id;
  if (!served) {
    // Not left pinned, or a later tap would surface a card nobody asked for then.
    setHead([]);
    rate.notice = `${item.name} can't be rated right now.`;
  }
  return served;
}
