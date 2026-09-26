// The number comes before the setting (decision 450): cards only propose, `propose` previews
// without writing, and only `confirm` writes, carrying the previewed figure. Every edit drops the
// stale preview first, and a late preview for an older change is discarded (`seq`).

import { api, get, post } from '$lib/api.js';

export const PROVIDER_LABELS = { anthropic: 'Anthropic', openai: 'OpenAI', gemini: 'Gemini' };

export const SOURCE_LABELS = { tmdb: 'TMDB', omdb: 'OMDb', trakt: 'Trakt' };

export const KEYLESS_LABELS = {
  wikidata: 'Wikidata',
  wikipedia: 'Wikipedia',
  tvmaze: 'TVmaze',
  rottentomatoes: 'Rotten Tomatoes',
  metacritic: 'Metacritic'
};

// Test routes wait on third-party hosts (45s reads plus pacing), past `api.js`'s default deadline.
const TEST_TIMEOUT_MS = 60_000;

export const spend = $state({
  llm: /** @type {any} */ (null),
  sources: /** @type {any} */ (null),
  /** In `PUT /admin/llm`'s grammar: absent keeps, null unsets. */
  proposal: /** @type {any} */ (null),
  /** `{change, preview, seq}`, once the preview has answered for exactly `proposal`. */
  pending: /** @type {any} */ (null),
  asking: false,
  /** The one write in flight: 'confirm', 'cap', or `key-<name>`. */
  busy: '',
  /** The sentence a 409 refused the last confirm with, shown over the fresh figure. */
  refused: '',
  error: '',
  capError: '',
  llmError: '',
  sourcesError: '',
  /** Bumped whenever a proposal ends, so a card's typed-but-uncommitted field starts over. */
  epoch: 0
});

let seq = 0;

const message = (/** @type {any} */ err) => err?.message || String(err);

/** Leaves any proposal alone: saves call this too. */
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

// Module state outlives a navigation, so arrive with a fresh read and no proposal.
export async function openSpendGuard() {
  cancel();
  spend.llm = null;
  spend.sources = null;
  spend.error = '';
  spend.capError = '';
  await Promise.all([load(), loadSources()]);
}

// `undefined` removes a field ("keep stored"), unlike null ("unset"); `providers` merges per field.
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
  // An empty change is no proposal: its confirm would be a 422, so it is the same as Cancel.
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

// Drop the figure at the first keystroke: iOS Safari may not fire `change` before a tapped Confirm.
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

// On 409 `api.js` passes on only the refusal's detail, so ask the preview again (it writes nothing).
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

// In force at once with no preview, since the cap enables no provider. Zero means spend nothing.
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

// Sends only fields holding text, and empties every field whatever happens (§14.3).
export async function saveKey(name, fields) {
  /** @type {Record<string, string>} */
  const body = {};
  // Trimmed: a stray space must not replace a working key (the route trims too).
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

export async function testConnector(name) {
  try {
    const path = `/admin/connectors/${name}/test`;
    return await post(path, undefined, { timeoutMs: TEST_TIMEOUT_MS });
  } catch (err) {
    return { ok: false, status: err?.status ?? null, error: message(err) };
  }
}

// Not `toFixed(2)`: the server sends exact decimal strings, and $0.032175 must not become $0.03.
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

// A dated table price says when it ends and what it becomes (decision 343).
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
