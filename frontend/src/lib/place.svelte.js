// Place with questions (decision 528): the search travels in a sealed token this module never opens;
// each answer comes back as the next pair or as where the title now sits.

import { post } from '$lib/api.js';

export const place = $state({
  /** @type {any} the pair on the table */
  pair: null,
  /** @type {any} where the search put the title, once it ends */
  done: null,
  busy: false,
  /** @type {string | null} the answer in flight, in the pair card's words */
  pending: null,
  error: ''
});

export function reset() {
  place.pair = null;
  place.done = null;
  place.busy = false;
  place.pending = null;
  place.error = '';
}

async function send(path, body, pending = null) {
  if (place.busy) return;
  place.busy = true;
  place.pending = pending;
  place.error = '';
  try {
    const payload = await post(path, body);
    place.pair = payload.done ? null : payload;
    place.done = payload.done ? payload : null;
  } catch (err) {
    place.error = err.message;
  } finally {
    place.busy = false;
    place.pending = null;
  }
}

export function start(titleId, kind) {
  reset();
  return send('/rank/place', { title_id: titleId, kind });
}

/** The five-step answer: "Much more" is decisive; the server never weights a tie. */
export function answer(outcome, much) {
  const token = place.pair?.token;
  return send('/rank/place/answer', { token, outcome, decisive: much }, `duel-${outcome}${much ? '-much' : ''}`);
}

/** The neighbour is not seen: the next question asks about the one beside it. */
export function notSeen() {
  return send('/rank/place/skip', { token: place.pair?.token }, 'correction-right');
}

/** The narrowing bar: the window still possible, as places in the tier and as a share of it. */
export function narrowing({ tier, progress: { low, high, size, asked, estimate } }) {
  return {
    where: `Somewhere between #${low} and #${high} of ${size} in ${tier}`,
    count: `Question ${asked + 1} of about ${estimate}`,
    from: (low - 1) / size,
    span: (high - low + 1) / size
  };
}

export function resultLine({ tier, above, below, asked }) {
  const where =
    above && below
      ? `Between ${above.name} and ${below.name}`
      : below
        ? `At the top of ${tier}, above ${below.name}`
        : above
          ? `At the bottom of ${tier}, below ${above.name}`
          : `The only one in ${tier}`;
  return asked ? `${where} — ${asked} ${asked === 1 ? 'question' : 'questions'}` : where;
}
