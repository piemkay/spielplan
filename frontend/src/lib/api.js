// The one place that knows the wire format. The session import is circular; ESM resolves it
// because neither module touches the other at evaluation time.

import { session } from './session.svelte.js';

export class ApiError extends Error {
  /** @param {number} status @param {string} message @param {any} [detail] @param {boolean} [reauth] */
  constructor(status, message, detail, reauth = false) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.reauth = reauth;
  }
  get isUnauthenticated() {
    return this.status === 401;
  }
  /** The server marks §3.2's 24h admin re-prompt with a header on an ordinary 401. */
  get needsAdminReauth() {
    return this.status === 401 && this.reauth;
  }
  get needsPasswordChange() {
    return this.status === 403 && /password change required/.test(this.message);
  }
}

// A request that never settles parks a surface's busy flag for ever. No automatic retries: the
// verdict, duel and answer routes are not idempotent, so a retry would double a vote.
const TIMEOUT_MS = 10_000;

// Routes that legitimately run for minutes (the import, a library sync); the deadline is the route's.
const LONG_TIMEOUT_MS = 600_000;
const LONG_ROUTES = [
  '/admin/bundle/validate',
  '/admin/bundle/import',
  '/admin/connectors/jellyfin/sync'
];

// Routes that reach the media server: its client allows 15s per call and a probe spends that
// twice, so 10s would abort a save that already landed or a test whose 200 is the diagnosis.
const CONNECTOR_TIMEOUT_MS = 45_000;
const CONNECTOR_ROUTES = /^\/admin\/(?:connectors\/jellyfin|users\/[^/]+\/jellyfin)(?:\/|$)/;

// A stated sentence, where an unsettled request would leave an empty string.
const NO_ANSWER = 'the network did not answer — try again';

// HTTP/2 through the edge carries no reason phrase, so an empty 502 would otherwise say nothing.
const UNSTATED = 'the appliance refused this and did not say why';

// Routes whose 401 answers the question rather than ending a session: the anonymous doors and
// the three that verify the account password. Not a `/setup` prefix: one route under it can 401.
const CREDENTIAL_ROUTES = [
  '/auth/login',
  '/auth/switch',
  '/auth/logout',
  '/auth/me',
  '/auth/passkey/login',
  '/auth/passkey/login/options',
  '/setup/state',
  '/setup/admin',
  '/auth/password',
  '/auth/pin',
  '/auth/reauth'
];

// The per-member Jellyfin link's 401 is the media server refusing a password someone typed,
// never this session (§3.3). POST only: DELETE on the same path can 401 for real.
const FOREIGN_CREDENTIAL = /^\/admin\/users\/[^/]+\/jellyfin$/;

/** The path as the router sees it: `qs()` appends a query to plenty of these. */
function route(path) {
  const cut = path.search(/[?#]/);
  return cut === -1 ? path : path.slice(0, cut);
}

/** Exactly that route, or something below it — `/auth/pin` covers `/auth/pin/x`, not `/auth/pinx`. */
function under(path, prefixes) {
  const p = route(path);
  return prefixes.some((prefix) => p === prefix || p.startsWith(`${prefix}/`));
}

/** The deadline the route owns, longest first: the sync is under both and is the job, not the hop. */
function routeDeadline(path) {
  if (under(path, LONG_ROUTES)) return LONG_TIMEOUT_MS;
  if (CONNECTOR_ROUTES.test(route(path))) return CONNECTOR_TIMEOUT_MS;
  return TIMEOUT_MS;
}

// FastAPI's string `detail`, pydantic's list of `{loc, msg}`, or an object with `text` (the
// importer) or `message` (this backend's `{reason, message}`); the edge sends no reason phrase.
function wireMessage(detail, statusText) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const stated = detail
      .map((entry) => {
        const loc = Array.isArray(entry?.loc) ? entry.loc : [];
        const last = loc[loc.length - 1];
        // Two segments at least, and the last one a string: the first segment is the source
        // rather than a field, and a number is a position inside a list-valued one.
        const field = loc.length > 1 && typeof last === 'string' ? last : '';
        const msg = entry?.msg ?? '';
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (stated.length) return stated.join('; ');
  }
  return (detail && (detail.text || detail.message)) || statusText || UNSTATED;
}

/**
 * The shell's one reaction to losing the session; `goto` stays on the shell's side so this module
 * keeps no router. The `ApiError` is thrown either way.
 *
 * @type {((reason: 'signed-out' | 'password-change') => void) | null}
 */
let sessionLost = null;

/**
 * Register the shell's handler; pass null to clear it.
 *
 * @param {((reason: 'signed-out' | 'password-change') => void) | null} fn
 */
export function onUnauthenticated(fn) {
  sessionLost = fn;
}

/**
 * @param {string} path
 * @param {{method?: string, body?: any, signal?: AbortSignal, timeoutMs?: number}} [opts]
 */
export async function api(path, opts = {}) {
  // `opts.timeoutMs` is the escape hatch for a caller that knows the route's budget.
  const deadline = opts.timeoutMs ?? routeDeadline(path);
  // Hoisted: the seam reads it too, since one route's 401 means something else on POST.
  const method = opts.method ?? 'GET';
  const clock = new AbortController();
  let expired = false;
  const timer = setTimeout(() => {
    expired = true;
    clock.abort();
  }, deadline);
  // Composed with the caller's signal, so a caller's own abort stays an AbortError.
  const relay = () => clock.abort();
  if (opts.signal) {
    if (opts.signal.aborted) relay();
    else opts.signal.addEventListener('abort', relay);
  }

  try {
    const res = await fetch(`/api${path}`, {
      method,
      credentials: 'include',
      headers: opts.body ? { 'content-type': 'application/json' } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: clock.signal
    });

    if (res.status === 204) return null;

    let payload = null;
    let parsed = false;
    const text = await res.text();
    if (text) {
      try {
        payload = JSON.parse(text);
        parsed = true;
      } catch {
        payload = text;
      }
    }

    if (!res.ok) {
      // Only a parsed body is this app's sentence; an edge's HTML error page is not copy.
      const wire = payload && typeof payload === 'object' ? payload.detail : payload;
      const detail = parsed ? wire : null;
      const message = wireMessage(detail, res.statusText);
      const reauth = res.headers.get('x-spielplan-reauth') === 'admin';
      // The header is the server's only other signal of the re-prompt; the shell's banner reads this flag.
      if (reauth && session.user) session.user.admin_reauth_required = true;
      const err = new ApiError(res.status, message, detail, reauth);
      // A 401 ends a session only if the shell held one (a first boot answers 401 everywhere), and not
      // for the re-prompt, a credential route or the foreign Jellyfin link.
      if (sessionLost) {
        const foreign = method === 'POST' && FOREIGN_CREDENTIAL.test(route(path));
        const answersACredential = under(path, CREDENTIAL_ROUTES) || foreign;
        if (err.isUnauthenticated && !reauth && session.user && !answersACredential) {
          sessionLost('signed-out');
        } else if (err.needsPasswordChange) {
          sessionLost('password-change');
        }
      }
      throw err;
    }
    return payload;
  } catch (err) {
    // Only our own deadline becomes a sentence; status 0 keeps `isUnauthenticated` false.
    if (expired) throw new ApiError(0, NO_ANSWER);
    throw err;
  } finally {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', relay);
  }
}

export const get = (path, opts) => api(path, opts);
export const post = (path, body, opts) => api(path, { ...opts, method: 'POST', body });

// Arrays become repeated parameters, as FastAPI's `list[...]` expects.
export function qs(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) continue;
    if (Array.isArray(value)) {
      for (const v of value) search.append(key, v);
    } else {
      search.append(key, value);
    }
  }
  const out = search.toString();
  return out ? `?${out}` : '';
}
