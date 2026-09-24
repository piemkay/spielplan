/**
 * §6.6's spend guard and the cards around it: one state the Connectors page's meter, extraction,
 * provider and source cards share. Spec v2.1 §6.6, §9, §14.3; decisions 324, 325, 339, 343, 436,
 * 450-453.
 *
 * THE NUMBER COMES BEFORE THE SETTING, AND THIS MODULE IS WHERE THE ORDER LIVES (decision 450).
 * §6.6 asks for a "per-title cost estimate before enabling", which is a claim about sequence rather
 * than about a figure: an optimistic save with a rollback shows the number after the setting
 * persisted, and a drain tick between the two spends at it (plan A2). So a card only ever PROPOSES
 * a change -- `propose` asks `POST /admin/llm/preview`, which writes nothing -- and only `confirm`
 * writes, carrying the `per_title_usd` string that preview answered as `accepted_estimate`. The
 * server keeps the same order on its side (a stale or missing figure is 409 and stores nothing);
 * this module keeps it on this one, so no control can reach the write without a figure on screen
 * for exactly the change it would store:
 *
 *   - every edit replaces the proposal and drops the preview it had, synchronously and before any
 *     request, so a Confirm pressed while the next figure is on its way sends nothing;
 *   - a preview that answers after a later edit is discarded rather than shown against the wrong
 *     change, which is what the sequence number is for;
 *   - `cancel` forgets the proposal and makes no request at all. "Saying no costs nothing" is plan
 *     §7's check 2, and a cancel that asked the server anything would be a cancel that could fail.
 *
 * ONE PROPOSAL FOR THE PAGE, NOT ONE PER CARD. A provider's model and price override move the
 * estimate exactly as the extraction assignment does -- decision 452 refuses them on the key route
 * for that reason -- so every card amends the same proposal and it is previewed whole. A card that
 * previewed on its own would show a figure for a plan the next card's confirm then changed.
 *
 * KEYS ARE WRITE-ONLY, AND THE FIELD IS EMPTIED WHATEVER HAPPENS. `saveKey` sends only what was
 * typed -- an empty field keeps the stored key, the Jellyfin card's idiom (decision 452) -- and
 * clears every field it was handed in a `finally`, so a refused save does not leave a pasted key in
 * a form for the next person at the screen (§14.3). No response carries a key back; the cards
 * render the booleans the routes answer.
 */

import { api, get, post } from '$lib/api.js';

/** The cards' names for `llm/client.PROVIDERS`, the only three decision 343 prices. */
export const PROVIDER_LABELS = { anthropic: 'Anthropic', openai: 'OpenAI', gemini: 'Gemini' };

/** §6.6's "TMDB / OMDb / Trakt keys", spelled as the services spell themselves. */
export const SOURCE_LABELS = { tmdb: 'TMDB', omdb: 'OMDb', trakt: 'Trakt' };

/** Stage 2's keyless five (`GET /admin/connectors`' `keyless`), named for the one line that says
 *  which failures no card on this page can fix (proposal 137's point, plan C2). */
export const KEYLESS_LABELS = {
  wikidata: 'Wikidata',
  wikipedia: 'Wikipedia',
  tvmaze: 'TVmaze',
  rottentomatoes: 'Rotten Tomatoes',
  metacritic: 'Metacritic'
};

/**
 * The Test buttons' deadline, and why it is not `api.js`'s ten seconds. Each of these routes waits
 * on a third-party host through `acquire.fetch.Fetcher`, whose read alone may take the client's 45
 * seconds (`llm/client.probe` says so of the provider list read) behind the host's pacing, so
 * decision 269's amendment applies to them as it does to Jellyfin's: a button whose diagnosis IS
 * the 200 answer must not be aborted by the page before the server has one. `opts.timeoutMs` is
 * the escape hatch `api()` names for a caller that knows the route's budget.
 */
const TEST_TIMEOUT_MS = 60_000;

export const spend = $state({
  /** `GET /admin/llm`: the provider cards, the stored settings, the meter and the stored plan. */
  llm: /** @type {any} */ (null),
  /** `GET /admin/connectors`: the keyed sources as booleans and the keyless five. */
  sources: /** @type {any} */ (null),
  /** The change being composed, in `PUT /admin/llm`'s grammar (absent keeps, null unsets). */
  proposal: /** @type {any} */ (null),
  /** `{change, preview, seq}`, once the preview has answered for exactly `proposal`. */
  pending: /** @type {any} */ (null),
  /** Whether a preview is on its way, for the panel's "asking" line. */
  asking: false,
  /** The one write in flight: 'confirm', 'cap', or `key-<name>`. */
  busy: '',
  /** The sentence a 409 refused the last confirm with, shown over the fresh figure. */
  refused: '',
  /** A preview or confirm that failed for any other reason. */
  error: '',
  capError: '',
  llmError: '',
  sourcesError: '',
  /** Bumped whenever a proposal ends, so a card's typed-but-uncommitted field starts over. */
  epoch: 0
});

let seq = 0;

const message = (/** @type {any} */ err) => err?.message || String(err);

/** Read `GET /admin/llm` into the cards. Leaves any proposal alone: saves call this too. */
export async function load() {
  try {
    spend.llm = await get('/admin/llm');
    spend.llmError = '';
  } catch (err) {
    spend.llmError = message(err);
  }
}

export async function loadSources() {
  try {
    spend.sources = await get('/admin/connectors');
    spend.sourcesError = '';
  } catch (err) {
    spend.sourcesError = message(err);
  }
}

/**
 * The page's arrival: a fresh read and no proposal. Module state outlives a navigation, and a
 * preview left pending from a visit ten minutes ago is a figure nobody is looking at any more.
 */
export async function openSpendGuard() {
  cancel();
  spend.llm = null;
  spend.sources = null;
  spend.error = '';
  spend.capError = '';
  await Promise.all([load(), loadSources()]);
}

/**
 * `patch` merged over `base`, in the proposal's grammar: top-level fields replace, `providers`
 * merges per provider and per field, and `undefined` removes a field from the proposal (which is
 * "keep what is stored", not null's "unset"). A provider left with no field is dropped.
 */
export function amend(base, patch) {
  const next = { ...(base ?? {}) };
  for (const [key, value] of Object.entries(patch)) {
    if (key !== 'providers') {
      if (value === undefined) delete next[key];
      else next[key] = value;
      continue;
    }
    const providers = { ...(next.providers ?? {}) };
    for (const [name, fields] of Object.entries(value ?? {})) {
      const merged = { ...(providers[name] ?? {}) };
      for (const [field, v] of Object.entries(fields)) {
        if (v === undefined) delete merged[field];
        else merged[field] = v;
      }
      if (Object.keys(merged).length) providers[name] = merged;
      else delete providers[name];
    }
    if (Object.keys(providers).length) next.providers = providers;
    else delete next.providers;
  }
  return next;
}

async function ask(change, refused) {
  const snapshot = $state.snapshot(change);
  // An empty change previews the stored plan and its confirm is a 422, so it is no proposal at
  // all: amending the last edited field back out of the proposal is the same gesture as Cancel.
  if (!snapshot || !Object.keys(snapshot).length) {
    cancel();
    return null;
  }
  const mine = ++seq;
  spend.proposal = snapshot;
  spend.pending = null;
  spend.refused = refused;
  spend.error = '';
  spend.asking = true;
  try {
    const preview = await post('/admin/llm/preview', snapshot);
    if (mine !== seq) return null;
    spend.pending = { change: snapshot, preview, seq: mine };
    return preview;
  } catch (err) {
    if (mine === seq) spend.error = message(err);
    return null;
  } finally {
    if (mine === seq) spend.asking = false;
  }
}

/** Replace the proposal with `change` and ask what it would cost. Writes nothing (decision 450). */
export function propose(change) {
  return ask(change, '');
}

/**
 * An edit has started that has not been proposed yet -- a key typed into a model or price field.
 * The figure on screen is for the value that was there before, so it stops being confirmable now
 * rather than when the field is left: iOS Safari does not move focus to a tapped button, so a
 * field's `change` is not certain to fire before a Confirm under the same thumb. No request.
 */
export function invalidate() {
  if (!spend.pending && !spend.asking) return;
  seq++;
  spend.pending = null;
  spend.asking = false;
}

/** Ask again for the proposal as it stands, when an edit was begun and then left unchanged. */
export function reask() {
  if (spend.proposal && !spend.pending && !spend.asking) return ask(spend.proposal, '');
  return null;
}

/** Forget the proposal. No request: the stored configuration was never touched (plan §7 check 2). */
export function cancel() {
  seq++;
  spend.proposal = null;
  spend.pending = null;
  spend.refused = '';
  spend.asking = false;
  spend.epoch++;
}

/**
 * Store the pending change with the figure it was shown (decision 450). Sends nothing unless a
 * preview has answered for the proposal as it stands and named no blocked provider.
 *
 * On 409 the server has attached the fresh preview, but `api.js` hands a refusal on as its
 * `detail` and nothing beside it, so the preview is asked again for the same change -- which
 * writes nothing -- and shown under the refusal's own sentence. The client persists nothing of
 * the refused figure either way.
 */
export async function confirm() {
  const pending = spend.pending;
  if (!pending || pending.seq !== seq || spend.busy === 'confirm') return false;
  if (pending.preview?.blocked) return false;
  const change = $state.snapshot(pending.change);
  const accepted = pending.preview?.estimate?.per_title_usd;
  if (typeof accepted !== 'string') return false;
  spend.busy = 'confirm';
  spend.error = '';
  try {
    spend.llm = await api('/admin/llm', {
      method: 'PUT',
      body: { ...change, accepted_estimate: accepted }
    });
    cancel();
    return true;
  } catch (err) {
    if (err?.status !== 409) {
      spend.error = message(err);
      return false;
    }
    spend.busy = '';
    await ask(change, message(err));
    return false;
  } finally {
    if (spend.busy === 'confirm') spend.busy = '';
  }
}

/**
 * Decision 452's cap, written in place and in force at once: it enables no provider, so it needs
 * no preview. A finite number of at least zero, zero being "spend nothing" (decision 325); anything
 * else is refused here without a request, and there is no way back to "no cap" from this card.
 * A pending preview is asked again, because its "left of this month's cap" moved with it.
 */
export async function saveCap(amount) {
  if (typeof amount !== 'number' || !Number.isFinite(amount) || amount < 0) {
    spend.capError = 'the cap is a number of dollars, 0 or more; 0 spends nothing';
    return false;
  }
  spend.busy = 'cap';
  spend.capError = '';
  try {
    const answer = await api('/admin/llm/cap', { method: 'PUT', body: { cap_usd: amount } });
    if (spend.llm && answer?.meter) spend.llm.meter = answer.meter;
  } catch (err) {
    spend.capError = message(err);
    return false;
  } finally {
    if (spend.busy === 'cap') spend.busy = '';
  }
  await load();
  if (spend.proposal) await ask(spend.proposal, '');
  return true;
}

/**
 * A card's credentials through `PUT /admin/connectors/{name}` (decision 452): only the fields that
 * hold text are sent, and every field in `fields` is emptied afterwards whether the save landed,
 * was refused or never left. Answers `{ok, error}` for the card to show beside its own field.
 *
 * A provider key can lift a pending preview's `blocked` (a keyless provider in the plan), so the
 * provider cards are read again and the pending figure asked again.
 */
export async function saveKey(name, fields) {
  /** @type {Record<string, string>} */
  const body = {};
  // Trimmed, and a field of whitespace is an empty one: a stray space and Save replaced a working
  // key with the space while the card still said "(stored)", and a key copied with a trailing
  // space was stored with it (decision 452; the route trims too). [M5.7 review cycle 1, M57-KEYS-C1-02]
  for (const [field, value] of Object.entries(fields)) {
    const typed = typeof value === 'string' ? value.trim() : '';
    if (typed) body[field] = typed;
  }
  try {
    if (!Object.keys(body).length) return { ok: false, error: '' };
    spend.busy = `key-${name}`;
    await api(`/admin/connectors/${name}`, { method: 'PUT', body });
  } catch (err) {
    return { ok: false, error: message(err) };
  } finally {
    for (const field of Object.keys(fields)) fields[field] = '';
    if (spend.busy === `key-${name}`) spend.busy = '';
  }
  if (name in PROVIDER_LABELS) {
    await load();
    if (spend.proposal) await ask(spend.proposal, '');
  } else {
    await loadSources();
  }
  return { ok: true, error: '' };
}

/** §6.6's test button for a provider or source card: `{ok, status, error}`, as the route answers. */
export async function testConnector(name) {
  try {
    const path = `/admin/connectors/${name}/test`;
    return await post(path, undefined, { timeoutMs: TEST_TIMEOUT_MS });
  } catch (err) {
    return { ok: false, status: err?.status ?? null, error: message(err) };
  }
}

/**
 * A dollar figure as the server spelled it, with a dollar sign and at least two decimals.
 *
 * Not `toFixed(2)`: the routes send money as the string of its own digits because `llm_call.usd`
 * is exact (decision 325), and a per-title figure of $0.032175 rounded to "$0.03" is a figure the
 * confirm did not carry. Trailing zeros past the cents go; nothing else does. `unknown` stays a
 * word (decision 343), and null stays null for the caller to say what it means.
 */
export function usd(amount) {
  if (amount === null || amount === undefined) return null;
  if (amount === 'unknown') return 'unknown';
  const text = String(amount);
  const n = Number(text);
  if (!Number.isFinite(n)) return text;
  const plain = /e/i.test(text) ? n.toFixed(10) : text;
  const [whole, frac = ''] = plain.split('.');
  const kept = frac.replace(/0+$/, '').padEnd(2, '0');
  return `$${whole}.${kept}`;
}

/**
 * Decision 343's caption for one price basis: the model, the rates per million tokens, whether
 * they are the shipped table's or the admin's override, and -- for a dated table price -- the day
 * it ends and what it becomes, so a figure accepted in December says it is not January's.
 */
export function basisLine(basis) {
  if (!basis || basis === 'unknown') return 'price unknown';
  const source = basis.source === 'override' ? 'admin override' : 'shipped table';
  const rates = `${usd(basis.input)} in / ${usd(basis.output)} out per 1M tokens`;
  let line = `${basis.model} · ${rates} · ${source}`;
  if (basis.valid_until) {
    line += basis.then
      ? ` · valid until ${basis.valid_until}, then ${usd(basis.then.input)} / ${usd(basis.then.output)}`
      : ` · valid until ${basis.valid_until}, then unpriced (stage 6 parks until one is set)`;
  }
  return line;
}
