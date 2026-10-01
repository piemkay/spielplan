// The ladder's set-up (decision 547): one step per tier from the best down, every pick held here until
// Finish. A step's list leaves out the films earlier steps took; a search hit joins the step it was
// found on, moving off an earlier one, and Undo puts it back.

import { get, post, qs } from '$lib/api.js';
import { homeKept, strongEnd } from '$lib/home.svelte.js';

export const PAGE = 12;

/** @typedef {{ id: number, name: string, original_name?: string | null, original_language?: string | null, year?: number | null, poster_path?: string | null, seen: boolean }} Film */
/** @typedef {{ picks: Film[], found: Film[], took: { from: number, index: number, film: Film }[], films: Film[], more: boolean }} Draft */

export const setup = $state({
  /** @type {'loading' | 'steps' | 'done' | 'set' | 'error'} `set`: the ladder was set up before */
  status: 'loading',
  /** @type {{ tier: number, word: string, hint: string }[]} best first */
  steps: [],
  at: 0,
  /** @type {Draft[]} one per step */
  drafts: [],
  loading: false,
  busy: false,
  error: '',
  /** @type {any} the finish's answer, for the done screen */
  result: null,
  /** @type {Film[] | null} the search's films; null with no query, empty under two letters */
  hits: null
});

/** @returns {Draft} */
const blank = () => ({ picks: [], found: [], took: [], films: [], more: false });

let listSeq = 0;
let searchSeq = 0;

export function reset() {
  Object.assign(setup, {
    status: 'loading', steps: [], at: 0, drafts: [], loading: false, busy: false, error: '',
    result: null, hits: null
  });
  listSeq++;
  searchSeq++;
}

export async function start() {
  reset();
  try {
    const state = await get('/ladder/setup');
    if (state.done) {
      setup.status = 'set';
      return;
    }
    setup.steps = state.steps;
    setup.drafts = state.steps.map(blank);
    setup.status = 'steps';
    await more();
  } catch (err) {
    setup.status = 'error';
    setup.error = err.message;
  }
}

/** The ids every step before `at` holds: this step's list leaves them out. */
export function excluded(at = setup.at) {
  return setup.drafts.slice(0, at).flatMap((draft) => draft.picks.map((film) => film.id));
}

/** The step's grid: its search finds first, then its list. */
export function cells(at = setup.at) {
  const draft = setup.drafts[at];
  if (!draft) return [];
  const found = new Set(draft.found.map((film) => film.id));
  return [...draft.found, ...draft.films.filter((film) => !found.has(film.id))];
}

export const picked = (film, at = setup.at) =>
  setup.drafts[at]?.picks.some((pick) => pick.id === film.id) ?? false;

/** Where a film sits on the ladder so far: a step index, or -1. */
export const stepOf = (id) => setup.drafts.findIndex((draft) => draft.picks.some((f) => f.id === id));

export const total = () => setup.drafts.reduce((n, draft) => n + draft.picks.length, 0);

/** The next page of the step's films; a film already listed is not listed twice. */
export async function more() {
  const at = setup.at;
  const draft = setup.drafts[at];
  const step = setup.steps[at];
  if (!draft || !step) return;
  const seq = ++listSeq;
  setup.loading = true;
  setup.error = '';
  try {
    const page = await get(
      `/ladder/setup/films${qs({
        step: step.tier,
        offset: draft.films.length,
        limit: PAGE,
        exclude: excluded(at).join(',')
      })}`
    );
    if (seq !== listSeq) return;
    const listed = new Set(draft.films.map((film) => film.id));
    draft.films.push(...page.films.filter((film) => !listed.has(film.id)));
    draft.more = page.more;
  } catch (err) {
    if (seq === listSeq) setup.error = err.message;
  } finally {
    if (seq === listSeq) setup.loading = false;
  }
}

/** @param {Film} film */
export function toggle(film) {
  const draft = setup.drafts[setup.at];
  const index = draft.picks.findIndex((pick) => pick.id === film.id);
  if (index >= 0) draft.picks.splice(index, 1);
  else draft.picks.push(film);
}

/** A search hit joins this step picked, first in its grid, moving off any earlier step. @param {Film} film */
export function hit(film) {
  const draft = setup.drafts[setup.at];
  for (let from = 0; from < setup.at; from++) {
    const picks = setup.drafts[from].picks;
    const index = picks.findIndex((pick) => pick.id === film.id);
    if (index >= 0) {
      picks.splice(index, 1);
      draft.took.push({ from, index, film });
    }
  }
  if (!picked(film)) draft.picks.push(film);
  draft.found = [film, ...draft.found.filter((f) => f.id !== film.id)];
  setup.hits = null;
  searchSeq++;
}

export function next() {
  if (setup.at >= setup.steps.length - 1) return;
  setup.at += 1;
  setup.drafts[setup.at] = blank();
  setup.hits = null;
  searchSeq++;
  return more();
}

/** Back one step as it was: this step's picks go, and the films it took go back where they were. */
export function undo() {
  if (setup.at === 0) return;
  const draft = setup.drafts[setup.at];
  for (const { from, index, film } of [...draft.took].reverse()) {
    const picks = setup.drafts[from].picks;
    if (!picks.some((pick) => pick.id === film.id)) picks.splice(Math.min(index, picks.length), 0, film);
  }
  setup.drafts[setup.at] = blank();
  setup.at -= 1;
  setup.hits = null;
  listSeq++;
  searchSeq++;
  setup.loading = false;
}

/** What Finish sends: every pick with the tier of the step it is on. */
export function payload() {
  return setup.drafts.flatMap((draft, i) =>
    draft.picks.map((film) => ({ title_id: film.id, tier: setup.steps[i].tier }))
  );
}

export async function finish() {
  if (setup.busy || !total()) return;
  setup.busy = true;
  setup.error = '';
  try {
    setup.result = await post('/ladder/setup/finish', { picks: payload() });
    setup.status = 'done';
    // Home's kept payload still carries the notice.
    homeKept.payload = null;
  } catch (err) {
    // A 409 also answers a swapped or broken bundle; only this one means the ladder exists.
    if (err.detail?.reason === 'already_set_up') setup.status = 'set';
    else setup.error = err.message;
  } finally {
    setup.busy = false;
  }
}

/** Any film by name as the library's search finds it, looser matches left out; two letters at least. */
export async function search(q) {
  const seq = ++searchSeq;
  const text = q.trim();
  if (text.length < 2) {
    setup.hits = text ? [] : null;
    return;
  }
  try {
    const res = await get(`/titles${qs({ kind: 'movie', q: text, limit: 16 })}`);
    if (seq !== searchSeq) return;
    setup.hits = res.items.slice(0, strongEnd(res.items, text)).map((t) => ({
      id: t.id,
      name: t.name,
      original_name: t.original_name ?? null,
      original_language: t.original_language ?? null,
      year: t.year ?? null,
      poster_path: t.poster_path ?? null,
      seen: t.seen_state === 'seen'
    }));
  } catch (err) {
    if (seq === searchSeq) setup.error = err.message;
  }
}

export const readyLine = (n) =>
  `${films(n)} ${n === 1 ? 'is' : 'are'} on it. From now on, one tap puts each film you rate on it.`;

// §6.1: the learning curve, counted in ratings (decision 491).
export const LEARNING_LINE = 'Your suggestions get about three times more personal between 5 and 100 ratings.';

/** The done screen's last line: each clause only when its count is above zero. */
export function historyLine(earlier, before) {
  const parts = [];
  if (earlier > 0) parts.push(`Your ${earlier} earlier ${earlier === 1 ? 'rating is' : 'ratings are'} kept as history.`);
  if (before > 0) {
    parts.push(
      `The ${before} ${before === 1 ? 'other' : 'others'} you rated before come${before === 1 ? 's' : ''} back on Rate, one at a time, to find ${before === 1 ? 'its' : 'their'} step.`
    );
  }
  return parts.join(' ');
}

export const films = (n) => `${n} ${n === 1 ? 'film' : 'films'}`;
