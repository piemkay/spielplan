// The server is the gate: every figure is `flywheel/batch.quote`'s decimal string, never computed
// here. What this module owes is that a quote for another selection never arms Launch (`quoteKey`).

// The two feeds M6 produces are selectable, but the server refuses to launch them (decision 443).
const KINDS = {
  thin_facet: 'thin facet',
  empty_predicate: 'empty predicate - produced from M6',
  uncovered_frontier: 'uncovered frontier - produced from M6'
};

/** @param {string} kind */
export function kindLabel(kind) {
  return KINDS[kind] ?? String(kind);
}

// Plausible counts, not a limit; the stored plan's own count is added when it is outside them.
export const PASS_CHOICES = [1, 2, 3, 4, 5];

/** @param {number | null | undefined} stored */
export function passChoices(stored) {
  const n = Number(stored);
  return Number.isInteger(n) && n >= 1 && !PASS_CHOICES.includes(n)
    ? [...PASS_CHOICES, n].sort((a, b) => a - b)
    : PASS_CHOICES;
}

/**
 * A new Set, not a mutation, so the component's `$state` sees an assignment.
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
 * A row with no `est_titles` counts one, as `flywheel/batch._count` does.
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
 * The stored plan (decision 324), or no provider and one pass when it cannot be made.
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
 * In the envelope's order, so one selection has one spelling.
 *
 * @param {{name: string}[]} providers the envelope's provider list
 * @param {string[]} chosen
 */
export function orderedProviders(providers, chosen) {
  return providers.map((p) => p.name).filter((name) => chosen.includes(name));
}

/** @param {{titles: number, providers: string[], passes: number}} params */
export function quoteKey(params) {
  return `${params.titles}|${params.providers.join(',')}|${params.passes}`;
}

// By hand, not through `qs()`, which drops an empty value: `providers` is required even when empty.
export function quotePath(params) {
  const providers = encodeURIComponent(params.providers.join(','));
  return `/admin/flywheel/quote?titles=${params.titles}&providers=${providers}&passes=${params.passes}`;
}

/**
 * @param {{titles: number, providers: string[], passes: number}} current
 * @param {{titles: number, providers: string[], passes: number}} requested
 * @param {object} body the route's answer
 */
export function acceptQuote(current, requested, body) {
  if (quoteKey(current) !== quoteKey(requested)) return null;
  return { key: quoteKey(requested), body };
}

export const PENDING = 'working out the total for this selection';

/**
 * Dark unless the quote in hand answers the selection on screen and the server said launchable.
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
 * Nothing a page could price with (decision 441).
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

/** Never arithmetic: the server's decimal strings are exact. */
export function dollars(amount, absent = 'unknown') {
  return amount == null ? absent : `$${amount}`;
}

/**
 * From the quote, else the queue read's `meter`; "no cap" only from a reading whose cap is null,
 * never from the absence of a reading.
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
 * Rounded down, so a row never reads older than it is; a server clock slightly ahead is still
 * "just now", and an unreadable time draws nothing.
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
