// Writes name the server's `card_token`, never a title, and nothing here caches a model belief
// between cards (§6.1). Every response already carries the next card, which goes up at once while
// the echo names the film just placed.

import { ApiError, get, post, qs } from '$lib/api.js';
import { preloadPoster, ready } from '$lib/art.js';
import { haptic, ms } from '$lib/motion.js';

/** How long "{film} · {word}" stands in for the next card's meta line (decision 550). */
export const ECHO_MS = 1600;
const PRESS_MS = 150;

/** The title button's word for the kind on the table. */
export const KIND_TITLES = { movie: 'Films', series: 'Series' };

export const PAIR_QUESTION = 'Which did you enjoy more?';

/** A label as a member reads it: "liked" -> "Liked". */
export function sentenceCase(text) {
  const s = String(text ?? '');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const NO_UNDO = { available: false, kind: null, name: null };

export const rate = $state({
  // True only until the first envelope lands: a later refresh must not blank the card on screen.
  loading: true,
  booted: false,
  busy: false,
  /** What is in flight (`place-{tier}`, `not_seen`, `undo`, `kind`), so its control can say so. */
  pending: null,
  error: '',
  /** A refusal we can explain and recover from: a stale card, nothing left to undo. */
  notice: '',
  /** @type {null | {done: boolean, earlier_ratings: number, rated_before: number}} */
  setup: null,
  /** @type {null | {kinds: string[], kind: string, block: any}} */
  session: null,
  /** @type {any} the card on the table */
  card: null,
  /** True when Undo brought the card on the table back, so it comes in from the left. */
  back: false,
  /** @type {null | {rated_before: number, noun: string}} the block just ended, until Rate 15 more */
  done: null,
  /** @type {null | {line: string}} */
  drained: null,
  /** @type {{available: boolean, kind: string | null, name: string | null}} */
  undo: NO_UNDO,
  /** @type {null | {title_id: number, name: string, tier: number, word: string, model?: any}} */
  echo: null,
  /** @type {any} §6.7, gated by the show_model preference where the payload is built. */
  ledger: null,
  /** @type {string[]} */
  log: []
});

// The banner's pins, sent as repeated `?head=` parameters.
let head = [];
let echoTimer = null;
let shownAt = 0;
// Rate 15 more dismissed the end screen: a re-read of the same end does not bring it back.
let doneSeen = false;

/** @param {(string|number)[]} ids */
export function setHead(ids) {
  // Title ids only: `Number(null)` is a finite 0, and a stray 0 is a 422.
  head = (ids ?? []).map(Number).filter((n) => Number.isInteger(n) && n > 0);
  return head;
}

export function pendingHead() {
  return [...head];
}

// A pin is served until it is answered, so one on the table leaves the list.
function consumePin(card) {
  const id = card?.title?.id;
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

/** Milliseconds the card was on screen before the tap: §4.2's `latency_ms`. */
export function latency() {
  return shownAt ? Math.max(0, Date.now() - shownAt) : null;
}

/**
 * Fetch the card's art (its film and every shelf's posters) and the server's `preload` for the
 * card after it. Returns the card's own images, which the swap waits on.
 */
export function preloadArt(res) {
  for (const src of res?.preload ?? []) {
    if (typeof src === 'string' && typeof Image !== 'undefined') new Image().src = src;
  }
  const card = res?.card;
  return [card?.title, ...(card?.shelves ?? []).flatMap((shelf) => shelf.films ?? [])]
    .filter(Boolean)
    .map(preloadPoster);
}

function apply(res, { write = false, back = false } = {}) {
  if (write) doneSeen = false;
  rate.setup = res.setup ?? null;
  rate.session = res.session ?? null;
  rate.undo = res.undo ?? NO_UNDO;
  rate.ledger = res.ledger ?? null;
  rate.log = res.log ?? [];
  rate.drained = res.drained ?? null;
  rate.done = doneSeen ? null : (res.done ?? null);
  consumePin(res.card);
  if (res.echo) {
    rate.echo = res.echo;
    echoTimer = setTimeout(clearEcho, ECHO_MS);
  }
  rate.back = back;
  rate.card = res.card ?? null;
  shownAt = Date.now();
}

function clearEcho() {
  clearTimeout(echoTimer);
  rate.echo = null;
}

// Each is the table moving on without us (or a set-up still owed): say so and re-read it.
const REREAD = new Set(['stale_card', 'no_card', 'nothing_to_undo', 'not_set_up']);

async function onError(err) {
  const detail = err instanceof ApiError ? err.detail : null;
  const reason = detail && typeof detail === 'object' ? detail.reason : null;
  const message = (detail && typeof detail === 'object' && detail.message) || err.message;
  if (reason && REREAD.has(reason)) {
    rate.notice =
      reason === 'nothing_to_undo' ? 'Nothing to undo yet' : reason === 'not_set_up' ? '' : message;
    await load({ quiet: true });
    return;
  }
  rate.error = message || 'something went wrong';
}

/**
 * @param {() => Promise<any>} fn
 * @param {{pending: string, floor?: number, answer?: boolean}} opts
 */
async function send(fn, { pending, floor = 0, answer = false }) {
  if (rate.busy) return;
  if (answer) haptic();
  rate.busy = true;
  rate.pending = pending;
  rate.notice = '';
  rate.error = '';
  clearEcho();
  // An answer's card goes no sooner than its press has played (decision 530).
  const held = ms(floor) ? new Promise((done) => setTimeout(done, floor)) : null;
  try {
    const res = await fn();
    if (res) {
      // Never a blank poster: the next art decodes first, for at most 150 ms.
      const decoded = ready(preloadArt(res), 150);
      if (decoded) await decoded;
      if (held) await held;
      apply(res, { write: true, back: pending === 'undo' });
    }
  } catch (err) {
    await onError(err);
  } finally {
    rate.busy = false;
    rate.pending = null;
  }
}

/** Open or resume and serve the card; before the set-up, the closed state. Idempotent. */
export async function load({ quiet = false } = {}) {
  if (!quiet && !rate.booted) rate.loading = true;
  try {
    const res = await get(`/rate${qs({ head })}`);
    preloadArt(res);
    apply(res);
    rate.error = '';
  } catch (err) {
    rate.error = err.message || 'could not open Rate';
  } finally {
    rate.loading = false;
    rate.booted = true;
  }
}

/** One tap on a shelf: the film goes on that step of the person's ladder (§6.1). */
export function place(tier) {
  const token = rate.card?.token;
  if (!token) return;
  return send(() => post('/rate/place', { card_token: token, tier, latency_ms: latency(), head }), {
    pending: `place-${tier}`,
    floor: PRESS_MS,
    answer: true
  });
}

export function notSeen() {
  const token = rate.card?.token;
  if (!token) return;
  return send(() => post('/rate/not-seen', { card_token: token, latency_ms: latency(), head }), {
    pending: 'not_seen',
    floor: PRESS_MS,
    answer: true
  });
}

/** Takes back the last placement or Not seen in the block; its film slides back in. */
export function undo() {
  return send(() => post('/rate/undo', {}), { pending: 'undo' });
}

/** Rate is one kind at a time, switched from the title. */
export function setKind(kind) {
  if (rate.session?.kind === kind) return;
  return send(() => post('/rate/session', { kinds: [kind], head }), { pending: 'kind' });
}

/** Leave a block's end screen for the card the fifteenth answer already brought. */
export function continueRating() {
  clearEcho();
  doneSeen = true;
  rate.done = null;
  shownAt = Date.now();
}

/** A stray echo timer would fire into a destroyed component. */
export function reset() {
  clearEcho();
  doneSeen = false;
  rate.done = null;
  rate.pending = null;
}
