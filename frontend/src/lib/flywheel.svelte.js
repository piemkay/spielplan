/**
 * §6.6 Data's extraction queue: the selection, the quote it asks for, and the one rule that arms
 * Launch. Spec v2.1 §8.4, §6.6 Data; decisions 330, 441, 442 and 443.
 *
 * THE SERVER IS THE GATE, AND THIS MODULE ONLY DECIDES WHEN TO ASK IT. §8.4's "sees the cost
 * estimate, launches" is decision 441's reservation - the per-title estimate at the batch's own
 * providers and passes, times the titles, times both attempts - and every figure of it is
 * `flywheel/batch.quote`'s, arriving as decimal strings. No money is added, multiplied or compared
 * here: a JSON number is a binary float, the meter is exact on purpose (decision 325), and a total
 * computed in two places is two totals. The launch route prices the batch again inside its own
 * transaction whatever this page shows, so what this module owes is narrower and still load-bearing:
 * never let a quote for some OTHER selection arm the button.
 *
 * WHY A STALE QUOTE IS THE DEFECT TO DESIGN AGAINST. The quote is asked on every change and answers
 * out of order whenever the network does. A quote for two titles at one pass, landing after the
 * operator has ticked a hundred rows at two passes, says `launchable: true` about a batch that is
 * not the one on screen. `quoteKey` names the selection a quote was asked for and `launchState`
 * arms Launch only when that name is the current one; the server would refuse the bigger batch, but
 * a button that looks pressable over a figure that is not the batch's is plan C3's "warning after
 * the fact" in its other form.
 *
 * PURE FUNCTIONS, for `bundleImport.svelte.js`'s reason: the component keeps its runes and this
 * module keeps the rules.
 */

/**
 * The queue's three feeds as the rows label them (decision 328 struck the fourth). The two whose
 * producer is M6's are shown and selectable, and a launch naming one is refused by the server
 * naming M6 (decision 443), so the label says so before anybody presses.
 */
const KINDS = {
  thin_facet: 'thin facet',
  empty_predicate: 'empty predicate - produced from M6',
  uncovered_frontier: 'uncovered frontier - produced from M6'
};

/** @param {string} kind */
export function kindLabel(kind) {
  return KINDS[kind] ?? String(kind);
}

/**
 * The pass counts the batch picker offers. `llm/spend` refuses nothing at or above 1 and prices any
 * count, so these are the counts an operator plausibly runs rather than a limit; the stored plan's
 * own count is added when it is outside them, so the default is always a choice on the list.
 */
export const PASS_CHOICES = [1, 2, 3, 4, 5];

/** @param {number | null | undefined} stored */
export function passChoices(stored) {
  const n = Number(stored);
  return Number.isInteger(n) && n >= 1 && !PASS_CHOICES.includes(n)
    ? [...PASS_CHOICES, n].sort((a, b) => a - b)
    : PASS_CHOICES;
}

/**
 * A new selection with `id` toggled. A new Set rather than a mutation, so the component's `$state`
 * sees an assignment and every derivation over it re-runs.
 *
 * @param {Set<number>} selected
 * @param {number} id
 */
export function toggle(selected, id) {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/**
 * The titles a selection counts: the sum of the selected rows' `est_titles`.
 *
 * A row written without the figure counts one, which is `flywheel/batch._count`'s reading - a
 * thin-facet row is one title and 0029's CHECK makes it name that title - so the quote on screen
 * counts what the launch will count rather than a free zero.
 *
 * @param {{id: number, est_titles?: number | null}[]} items
 * @param {Set<number>} selected
 */
export function titlesOf(items, selected) {
  let titles = 0;
  for (const item of items) {
    if (!selected.has(item.id)) continue;
    titles += item.est_titles == null ? 1 : Number(item.est_titles);
  }
  return titles;
}

/**
 * The plan a new page starts from: the stored plan's providers and passes (decision 324), or no
 * provider and one pass when the stored plan cannot be made - in which case `defaults.reason` is
 * the sentence saying why, and the quote will say it again.
 *
 * @param {{defaults?: {providers?: string[] | null, passes?: number | null, reason?: string | null}}}
 *   envelope
 */
export function defaultPlan(envelope) {
  const defaults = envelope?.defaults ?? {};
  return {
    providers: Array.isArray(defaults.providers) ? [...defaults.providers] : [],
    passes: Number.isInteger(defaults.passes) && defaults.passes >= 1 ? defaults.passes : 1
  };
}

/**
 * The providers ticked, in the envelope's order, so one selection has one spelling whichever order
 * the boxes were ticked in.
 *
 * @param {{name: string}[]} providers the envelope's provider list
 * @param {string[]} chosen
 */
export function orderedProviders(providers, chosen) {
  return providers.map((p) => p.name).filter((name) => chosen.includes(name));
}

/**
 * The name of one selection: what a quote was asked for, compared with what is on screen now.
 *
 * @param {{titles: number, providers: string[], passes: number}} params
 */
export function quoteKey(params) {
  return `${params.titles}|${params.providers.join(',')}|${params.passes}`;
}

/**
 * `GET /api/admin/flywheel/quote` for one selection, under `/api`.
 *
 * Spelled by hand rather than through `qs()`, which drops an empty value: with no provider ticked
 * the route still has to be asked, because its answer - the no-cap sentence, or the plan's refusal
 * for a batch naming nothing - is the reason Launch is dark, and `providers` is required there.
 */
export function quotePath(params) {
  const providers = encodeURIComponent(params.providers.join(','));
  return `/admin/flywheel/quote?titles=${params.titles}&providers=${providers}&passes=${params.passes}`;
}

/**
 * Keep a quote only when it answers the selection on screen now; otherwise null.
 *
 * @param {{titles: number, providers: string[], passes: number}} current
 * @param {{titles: number, providers: string[], passes: number}} requested
 * @param {object} body the route's answer
 */
export function acceptQuote(current, requested, body) {
  if (quoteKey(current) !== quoteKey(requested)) return null;
  return { key: quoteKey(requested), body };
}

/** What Launch says while the quote for this selection has not yet come back. */
export const PENDING = 'working out the total for this selection';

/**
 * Whether Launch may press, and the sentence beside it when it may not.
 *
 * Dark unless the quote in hand answers the selection on screen AND the server said launchable;
 * the reason is the server's own sentence whenever it gave one (no cap, the plan's refusal, an
 * unknown price, nothing selected, the reservation over the room left), shown whole.
 *
 * @param {{key: string, body: {launchable?: boolean, reason?: string | null}} | null} quoted
 * @param {string} currentKey quoteKey of the selection on screen
 * @param {boolean} busy a launch is in flight
 */
export function launchState(quoted, currentKey, busy) {
  if (!quoted || quoted.key !== currentKey) return { disabled: true, reason: PENDING };
  const reason = quoted.body?.reason ?? null;
  if (quoted.body?.launchable !== true) {
    return { disabled: true, reason: reason ?? 'the server did not say this batch may launch' };
  }
  return { disabled: busy, reason: null };
}

/**
 * The launch body: exactly the selected ids, the providers and the passes - and nothing a page
 * could price with (decision 441; `api/flywheel.Launch`).
 *
 * @param {Set<number>} selected
 * @param {{providers: string[], passes: number, titles?: number}} plan
 */
export function launchBody(selected, plan) {
  return {
    item_ids: [...selected].sort((a, b) => a - b),
    providers: [...plan.providers],
    passes: plan.passes
  };
}

/** A decimal string from the server as dollars, or the word for its absence; never arithmetic. */
export function dollars(amount, absent = 'unknown') {
  return amount == null ? absent : `$${amount}`;
}

/**
 * The room the month leaves, from whichever meter reading is in hand: the quote for the selection on
 * screen, else the queue read's own `meter` - the same `spend.meter` reading, taken when the page
 * loaded. "no cap" is said only by a reading whose cap IS null, never by the absence of a reading:
 * between a tap and its quote, and for good while the quote route fails, the card otherwise told a
 * household with a cap that it had none, on the one control that spends. [M5.6 review cycle 1,
 * M56-DATA-02; decisions 330 and 441]
 *
 * @param {{cap_usd?: string | null, remaining_usd?: string | null} | null} quoted
 * @param {{cap_usd?: string | null, remaining_usd?: string | null} | null} meter
 */
export function roomLeft(quoted, meter) {
  const reading = quoted ?? meter;
  if (!reading || !('cap_usd' in reading)) return 'unknown';
  if (reading.cap_usd == null) return 'no cap';
  return `${dollars(reading.remaining_usd)} of ${dollars(reading.cap_usd)}`;
}

/**
 * §8.4 as v2.1.3 amends it: a failure is in the queue "with its reason and a 'queued just now'
 * marker read off the row's own creation time" (decision 330, proposal 135's adjustment). Under a
 * minute it is exactly that; after it, the age in the coarsest unit that is still true, rounded
 * down so a row is never said to be older than it is. A row the server stamped a few seconds
 * ahead of this device's clock is just now too, not "in the future". Null for a row with no
 * readable time, so the card draws nothing rather than "NaN min ago". [M5.6 review cycle 1,
 * M56-DATA-01]
 *
 * @param {string | null | undefined} createdAt the row's `created_at`, as the route spells it
 * @param {number} now milliseconds since the epoch
 */
export function queuedMarker(createdAt, now) {
  const at = createdAt == null ? NaN : Date.parse(createdAt);
  if (!Number.isFinite(at)) return null;
  const minutes = Math.floor(Math.max(0, now - at) / 60_000);
  if (minutes < 1) return 'queued just now';
  if (minutes < 60) return `queued ${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `queued ${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  return `queued ${days} days ago`;
}
