/**
 * API client. One place that knows the wire format, so a route never hand-rolls a fetch.
 *
 * Session auth is an HttpOnly cookie (§3.2), so `credentials: 'include'` is the whole story
 * and there is no token to keep anywhere in JS.
 */

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
  /**
   * §3.2: "admin routes re-prompt after 24 h". The server says so with a header rather than a
   * different status, because to everything else this is an ordinary 401 — but the shell has
   * to tell "sign in again" apart from "you were signed out".
   */
  get needsAdminReauth() {
    return this.status === 401 && this.reauth;
  }
  get needsPasswordChange() {
    return this.status === 403 && /password change required/.test(this.message);
  }
}

/**
 * @param {string} path
 * @param {{method?: string, body?: any, signal?: AbortSignal}} [opts]
 */
export async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    method: opts.method ?? 'GET',
    credentials: 'include',
    headers: opts.body ? { 'content-type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal
  });

  if (res.status === 204) return null;

  let payload = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!res.ok) {
    const detail = payload && typeof payload === 'object' ? payload.detail : payload;
    const message =
      typeof detail === 'string' ? detail : (detail && detail.text) || res.statusText;
    throw new ApiError(
      res.status,
      message,
      detail,
      res.headers.get('x-spielplan-reauth') === 'admin'
    );
  }
  return payload;
}

export const get = (path, opts) => api(path, opts);
export const post = (path, body, opts) => api(path, { ...opts, method: 'POST', body });

/**
 * Build a query string, dropping empty values so the URL stays readable.
 *
 * An array becomes a repeated parameter (`?kind=movie&kind=series`) rather than a joined
 * string, which is what FastAPI's `list[...]` expects — and it makes the empty selection
 * unrepresentable rather than sending `?kind=`.
 */
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
