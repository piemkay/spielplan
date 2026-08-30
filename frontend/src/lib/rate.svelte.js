/**
 * The Rate surface's client. Spec v2.1 §6.1, §6.7, §6.8, §13; decision-doc proposals 34–53,
 * 153 and decision 35.
 *
 * Everything this surface knows about the wire lives here, and three of the rules it encodes
 * are load-bearing enough to state out loud:
 *
 *   * **The card is the server's.** A write names `card_token`, never a title, so a double
 *     tap, a back button or a second device arrives as a 409 with a reason rather than as a
 *     second row. We refuse to invent a token and we never answer a card we are only
 *     *showing* — see `commit()`.
 *   * **No model belief before the tap.** §6.1 (Cosley 2003) is enforced by the server's
 *     allow-list, and this module does not undo it: nothing here caches a score, a tier or a
 *     prediction between cards, and `reveal` is only ever the thing the verdict response
 *     handed back. Synthesising one client-side would defeat the whole point of withholding it.
 *   * **The counter is the Undo depth.** Decision 35: "the depth matches the counter the user
 *     is already reading". `session.block.counter` and `undo.available` come from the same
 *     response, so the number on screen and the number Undo obeys cannot drift apart.
 *
 * The one piece of timing that is ours rather than the server's is the reveal hold: proposal
 * 42 attaches the reveal to *the card just rated* for ~1.2 s. The response to a verdict already
 * carries the next card ("next card preloaded", §6 preamble), so we hold the answered card and
 * its counter on screen and swap in the preloaded one when the hold ends — no request happens
 * at the swap, which is what keeps the <2 s budget while the reveal still has somewhere to live.
 */

import { ApiError, get, post, qs } from '$lib/api.js';

/** Proposal 42: "~1.2 s or until the next card". */
export const HOLD_MS = 1200;

/** §4.1 rule 5's partition, in the words the surface uses. */
export const KIND_LABELS = { movie: 'film', series: 'series' };

/** §6.1's three modes; Mix is the default and every entry point lands on it (proposal 36). */
export const MODES = [
  ['mix', 'alternates sweep and battle'],
  ['sweep', 'one title at a time'],
  ['battle', 'two posters, pick one']
];

/**
 * Proposal 47: the decisive toggle "carries that copy on itself as a one-line why".
 * §6.1 fixes the sentence and §5.2 the weights.
 */
export const DECISIVE_COPY = 'a decisive pick teaches more than a hesitant one';

/** Proposal 53: "Random pairs." turns a defence into a statement. §6.1 supplies the rest. */
export const PAIR_SELECTION_COPY =
  'Random pairs. For profiles no selection rule beats random — the clever ones only pay ' +
  'off in the tier queue.';

/** §6.1's learning curve. Proposal 49: the copy is the caption, the position is the point. */
export const LEARNING_CURVE_COPY =
  'Personal signal roughly triples from 5 to 100 labels. Aim for 50–100 in the first ' +
  'sitting or two.';

/** §12's M2 exit criterion, which proposal 49 makes legible to the person doing the labelling. */
export const LEARNING_TARGET = 100;

export const rate = $state({
  // Only true until the first envelope lands. A later refresh must not flip it: blanking the
  // surface mid-session would throw away the card the person is looking at.
  loading: true,
  booted: false,
  busy: false,
  error: '',
  /** A refusal we can explain and recover from — a stale card, an Undo at the boundary. */
  notice: '',
  /** @type {null | {id:number,mode:string,kinds:string[],decisive:boolean,block:any}} */
  session: null,
  /** @type {any} the card on the table, or the card just answered while the reveal holds */
  card: null,
  /** The counter that belongs to `card` while a reveal is held; null otherwise. */
  frozenBlock: null,
  holding: false,
  /** @type {null | {text:string}} */
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

/** §6.0's pending-verdicts banner pins titles to the front with repeated `?head=` parameters. */
let head = [];
let pendingCard = null;
let holdTimer = null;
let shownAt = 0;

/** @param {(string|number)[]} ids */
export function setHead(ids) {
  // `Number(null)` is 0 and 0 is finite, so "is it a number" is not the test — "is it a title
  // id" is. A stray 0 in the list is a 422 from the route, which reads as the banner being
  // broken rather than as one bad segment in a URL.
  head = (ids ?? []).map(Number).filter((n) => Number.isInteger(n) && n > 0);
  return head;
}

// --- pure helpers, all of them rendered somewhere and all of them testable ------------------

/** @param {string[]} kinds */
export function kindLabel(kinds) {
  const names = (kinds ?? []).map((k) => KIND_LABELS[k] ?? k);
  if (!names.length) return '';
  return names.join(' + ');
}

/**
 * §6.1's counter, with proposal 46's partition and the card type it is serving:
 * "7 / 15 this block · film · sweep". Decision 35 makes this the number Undo is measured in,
 * so it is built from the server's own `counter` string rather than recomputed here.
 */
export function counterLine(block, kinds) {
  if (!block) return '';
  const parts = [`${block.counter} this block`];
  const kind = kindLabel(kinds);
  if (kind) parts.push(kind);
  if (block.serving) parts.push(block.serving);
  return parts.join(' · ');
}

/** Runtime in the shape the rest of the app uses (see PosterCard). */
export function runtimeLabel(title) {
  if (!title?.runtime_min) return null;
  if (title.kind === 'series') return `${title.runtime_min}m/ep`;
  return `${Math.floor(title.runtime_min / 60)}h ${title.runtime_min % 60}m`;
}

/**
 * Proposal 40's meta line, in the data voice. Genre is not on the wire for this card, so the
 * line is year and runtime; it is built in JS rather than in markup because Svelte collapses
 * the whitespace around an `{#if}` and turns "1995 · 2h 50m" into "1995· 2h 50m".
 */
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

/**
 * Decision 35: at the block boundary the chip "disables visibly, not silently". The server
 * sends the reason; this is the sentence for it, and there is no third branch — an Undo that
 * is available needs no explanation.
 */
export function undoMessage(undo) {
  if (!undo || undo.available) return '';
  if (undo.reason === 'block_boundary') {
    return 'undo reaches back to the start of this block of 15 and no further';
  }
  return 'nothing to undo in this block';
}

/**
 * Proposal 153: before the first fit the reveal is *suppressed*, not banded — "a guess drawn
 * from someone else's thresholds is not a prediction about this user". The server says so;
 * we render its reason rather than a class.
 */
export function revealLine(reveal) {
  if (!reveal) return null;
  if (reveal.available) return { available: true, text: reveal.text, agreed: !!reveal.agreed };
  return { available: false, text: reveal.reason ?? 'no prediction yet', agreed: false };
}

/** Milliseconds the card was on screen before the tap (proposal 51, §4.2's `latency_ms`). */
export function latency() {
  return shownAt ? Math.max(0, Date.now() - shownAt) : null;
}

// --- the envelope --------------------------------------------------------------------------

function apply(res, { holdReveal = false } = {}) {
  const answeredCard = rate.card;
  const answeredBlock = rate.session?.block ?? null;

  rate.session = res.session ?? null;
  rate.balance = res.class_balance ?? null;
  rate.undo = res.undo ?? { available: false, kind: null, reason: 'empty' };
  rate.ledger = res.ledger ?? null;
  rate.log = res.log ?? [];
  rate.drained = res.drained ?? null;

  clearTimeout(holdTimer);
  if (holdReveal && res.reveal && answeredCard) {
    // The reveal belongs to the card just rated, so that card and its counter stay put.
    rate.reveal = res.reveal;
    rate.card = answeredCard;
    rate.frozenBlock = answeredBlock;
    rate.holding = true;
    pendingCard = res.card ?? null;
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
    rate.notice = message;
    await load({ quiet: true });
    return;
  }
  rate.error = message || 'something went wrong';
}

async function send(fn, { holdReveal = false } = {}) {
  if (rate.busy) return;
  rate.busy = true;
  rate.notice = '';
  rate.error = '';
  try {
    const res = await fn();
    if (res) apply(res, { holdReveal });
  } catch (err) {
    await onError(err);
  } finally {
    rate.busy = false;
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

/** Proposal 46: the Rate surface carries the film/series partition itself. */
export const setKinds = (kinds) => send(() => controls({ kinds }));

/** §6.1's persistent decisive toggle — the backend stores it, so it outlives the session. */
export const setDecisive = (decisive) => send(() => controls({ decisive }));

export const restart = () => send(() => controls({ restart: true }));

export function verdict(value) {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(
    () => post('/rate/verdict', { card_token: token, value, latency_ms: latency(), head }),
    { holdReveal: true }
  );
}

export function notSeen() {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(() => post('/rate/not-seen', { card_token: token, latency_ms: latency(), head }));
}

export function skip() {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(() => post('/rate/skip', { card_token: token, latency_ms: latency(), head }));
}

/**
 * @param {'A'|'B'|'TIE'} outcome
 * @param {{decisive?: boolean}} [opts] proposal 51's long-press: one answer may override the
 *   persistent toggle without moving it.
 */
export function duel(outcome, opts = {}) {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  const body = { card_token: token, outcome, latency_ms: latency(), head };
  if (opts.decisive !== undefined) body.decisive = opts.decisive;
  return send(() => post('/rate/duel', body));
}

/** §6.1's corrections row. Writes no duel row and does not advance the counter. */
export function correct(side) {
  const token = rate.card?.token;
  if (!token || rate.holding) return;
  return send(() => post('/rate/correction', { card_token: token, side }));
}

/**
 * Decision 35. Pops the last observation of any kind and restores the exact card that produced
 * it — including, deliberately, one taken during a reveal hold, which is why this clears the
 * hold instead of committing it.
 */
export function undo() {
  clearTimeout(holdTimer);
  rate.holding = false;
  rate.reveal = null;
  rate.frozenBlock = null;
  pendingCard = null;
  return send(() => post('/rate/undo', {}));
}

/** Tests and route teardown; a stray hold timer would fire into a destroyed component. */
export function reset() {
  clearTimeout(holdTimer);
  holdTimer = null;
  pendingCard = null;
  rate.holding = false;
  rate.reveal = null;
  rate.frozenBlock = null;
}
