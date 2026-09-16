/**
 * API client. One place that knows the wire format, so a route never hand-rolls a fetch.
 *
 * Session auth is an HttpOnly cookie (§3.2), so `credentials: 'include'` is the whole story
 * and there is no token to keep anywhere in JS.
 *
 * Three rules live here rather than in the surfaces, for the same reason the wire format does:
 * how long a request may take before it is a failure rather than a wait (finding 16), what a
 * pydantic refusal reads like (finding 17), and what a lost session does to the shell
 * (finding 14). Each of the three was a per-surface omission before M4.15 — which is to say it
 * was absent everywhere, because no surface owns the wire.
 *
 * The one thing this module writes is §3.2's admin re-prompt flag. The import of the session
 * store below is circular — `session.svelte.js` calls `get`/`post` from here — which ESM
 * resolves because neither module touches the other at evaluation time, only inside a call.
 */

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
 * The ordinary deadline (finding 16, fe-lc-01).
 *
 * Every surface's single-flight guard is a boolean cleared in a `finally` — `rate.svelte.js`'s
 * `busy`, `tonight.svelte.js`'s — so a request that never settles parks the surface at busy for
 * ever: every later tap returns early, nothing is sent, and the error line stays empty, which
 * reads as an app that is alive and has stopped answering. A phone leaving the house mid-request
 * is the ordinary way that happens, not the exotic one.
 *
 * Ten seconds is `api/deps.py`'s number for the same judgement on the other side of the wire
 * (§7.3's playback poll runs every 60 s, so a client told after ten seconds retries on its own),
 * and there are no automatic retries here on purpose: the verdict, duel and answer routes are
 * not idempotent by design, so a retry the person did not ask for would double a vote.
 */
const TIMEOUT_MS = 10_000;

/**
 * Decision 269: three routes legitimately run for minutes, and their deadline is declared here
 * rather than passed at their call sites.
 *
 * The importer's COPY and a Jellyfin sync over a whole library are the two long jobs §2's box
 * runs, and `api/deps.py` says in its own comment why neither has a server-side command timeout.
 * Ten seconds would abort both from the client half a minute into honest work. The list lives in
 * this module because a deadline the wire enforces cannot be lost by a rewrite of the caller —
 * `BundleImport.svelte` and the Connectors page are both being rewritten as this ships — and
 * because how long a route may take is a property of the route, not of whoever calls it today.
 */
const LONG_TIMEOUT_MS = 600_000;
const LONG_ROUTES = [
  '/admin/bundle/validate',
  '/admin/bundle/import',
  '/admin/connectors/jellyfin/sync'
];

/**
 * Decision 269, amended: a route that talks to a THIRD-PARTY server inherits that server's
 * budget, and ten seconds is shorter than it.
 *
 * The list above was read off the two long jobs §2's box runs, which is a list of CALLERS that
 * take minutes. It misses a whole class that is not long and is still over ten seconds:
 * `connectors/jellyfin.py`'s `JellyfinClient` carries `timeout: float = 15.0` and
 * `connectors/registry.py`'s `make_client` never overrides it, so every admin route that reaches
 * the media server has a floor of fifteen seconds whenever the connect is silently dropped — a
 * firewalled LAN address, a box asleep, the ordinary §2 shape — and `check()` spends that budget
 * twice, on `server_info()` and then on `users()`.
 *
 * At ten seconds the client gave up first, which on two of these routes is worse than a wait.
 * `PUT /connectors/jellyfin` writes the credentials BEFORE it probes (`api/admin.py`'s
 * `put_jellyfin` calls `save_jellyfin` and only then `_store_probed_version`), so §6.6's card
 * said "the network did not answer" about a save that had already landed. And
 * `POST /connectors/jellyfin/test` answers HTTP 200 with `{ok: false, error, status}` — the
 * diagnosis IS the success payload — so aborting it does not delay the verdict, it destroys it:
 * the one control §6.6 gives a household for "is my media server reachable" could not report the
 * unreachable server it exists to report.
 *
 * Forty-five seconds is the server's own floor with the round trip on top, and not a minute
 * more. These are ceilings, not waits, and they are deliberately not on the list above: 600 s IS
 * a wait once the appliance itself is the thing that stopped answering, which is finding 16's own
 * failure rather than a fix for it. `/connectors/jellyfin/sync` stays long and is matched first,
 * because a sync over a whole library is the job and not the hop.
 *
 * A pattern rather than an `under()` prefix because one of them is not a prefix: the per-member
 * link (`POST /admin/users/{id}/jellyfin`, `api/admin.py`'s `link_jellyfin`) carries an account
 * id in the middle and reaches `authenticate_by_name` on the same fifteen seconds, from the same
 * page's Link button. [decision 269; §6.6, §7.1; review cycle 2: M415-C2-API-01]
 */
const CONNECTOR_TIMEOUT_MS = 45_000;
const CONNECTOR_ROUTES = /^\/admin\/(?:connectors\/jellyfin|users\/[^/]+\/jellyfin)(?:\/|$)/;

/**
 * §6.8's register applied to the failure nobody wrote copy for: a stated sentence, so the
 * `onError` and `fail` paths the surfaces already have render something a person can act on
 * instead of the empty string an unsettled promise leaves behind.
 */
const NO_ANSWER = 'the network did not answer — try again';

/**
 * The same register for the shape below that nobody wrote copy for either.
 *
 * `wireMessage`'s last resort was `res.statusText`, which the comment forty lines down already
 * argues is the empty string in §2's deployment — the household reaches this app over Traefik and
 * Cloudflare, and HTTP/2 carries no reason phrase. So the repair that closed three wire shapes
 * left the fall-through exactly as it found it: every surface gates its error line on truthiness,
 * so an edge that answers 502 with no body at all produced no line and no banner. Silence is the
 * failure NO_ANSWER was minted three lines up to end, reached by a second path.
 * [§6.8; review cycle 2: M415-C2-API-02]
 */
const UNSTATED = 'the appliance refused this and did not say why';

/**
 * The routes where a 401 is the answer to the question and not the loss of a session
 * (finding 14).
 *
 * The first EIGHT are the anonymous doors, read off `api/auth.py` and `api/passkeys.py`: signing
 * in, switching profile, signing out, `/auth/me` at boot, the passkey login ceremony in both its
 * halves, and first boot in both of its. Eight entries for six doors, because `under()` is a
 * prefix test and two of them are already covered by a shorter entry: the options call is listed
 * for the reader rather than for the match. `/auth/passkey/register` is deliberately absent —
 * registration runs inside a session, so a 401 there is a real loss.
 *
 * The last three are authenticated routes that verify the account password and answer 401
 * "wrong current password": `api/auth.py`'s `change_password`, `set_pin` and `reauth`. They are
 * not on the plan's list, which was read off the anonymous doors; without them a typo in §3.2's
 * 24-hour re-prompt or in the account PIN box would sign a member out of a session that is
 * perfectly alive, which is a worse failure than the one being fixed.
 *
 * By NAME and not by line, because a coordinate into another file is a citation nothing can keep
 * true — `test_no_backend_comment_cites_the_tonight_client_by_line_number` settles the rule for
 * the other direction of this same wire. The three numbers this comment shipped with, `:306`,
 * `:380` and `:416`, were correct when it was written and were falsified by THIS milestone's own
 * seven-line edit to that router, in the same uncommitted change set; they landed on a comment
 * fragment, a bare docstring terminator and a sentence about PIN lockout counters. They also
 * named the wrong two routes, having been listed in file order and read back in another: `:380`
 * is `reauth` and `:416` is `set_pin`, not the `/pin` and `/reauth` the line said.
 * [review cycle 3: M415-C3-API-05]
 *
 * The setup entries are TWO ROUTES, not the prefix the plan wrote, because read against
 * `api/setup.py` the prefix was inverted. The two named here cannot answer 401 at all:
 * `/setup/state` takes `OptionalUser`, whose `_optional_user` catches the refusal `current_user`
 * raises precisely so that a missing or dead cookie is an answer rather than a 401, and
 * `/setup/admin` carries no user dependency and answers 409. The two the prefix also covered are
 * the only ones that can: `/setup/connectors` is `AdminUser` and `/setup/onboarding/complete` is
 * `ActiveUser`, and both reach `api/deps.py`'s `current_user` and its "not signed
 * in". The second is reachable from an ordinary member surface — `push.js`'s
 * `completeOnboarding()` is the onboarding card's "Not now" button, rendered on `/account` — so a
 * member whose session the hourly prune had taken met finding 14's exact failure on the one route
 * class this list exists to exclude. Named rather than dropped: the reason is a property of those
 * two routes, and a bare deletion invites the next reader to re-widen it.
 */
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

/**
 * The one route in this backend whose 401 is a THIRD-PARTY server refusing a credential the
 * household just typed, rather than anything about the session holding the page.
 *
 * §3.3 is explicit that "authentication is **never** delegated to Jellyfin", so a refusal from
 * the media server can never be a statement about this app's session — and `api/admin.py`'s
 * `link_jellyfin` re-raises exactly that as 401 "Jellyfin refused that sign-in", because
 * `connectors/jellyfin.py`'s `authenticate_by_name` turns the media server's own refusal into a
 * `JellyfinError`. Every other 401 in this app is `api/deps.py`'s session refusal or one of the
 * account credentials named above; this one is somebody ELSE's password, typed into the two
 * boxes §3.3's per-member link puts on `/admin/users`, while the admin's own cookie stands
 * untouched.
 *
 * Unexempted it reached the seam, and the seam did what the list above says in its own words is
 * "worse than the one being fixed": a mistyped Jellyfin password ran `clearUser()`, `resetRank()`
 * and a `goto('/login')`, telling the household it had been signed out of a session that was
 * perfectly alive. The route was already read once in this module for its DEADLINE and not for
 * its status — it is the second half of `CONNECTOR_ROUTES` above.
 *
 * A pattern rather than an `under()` prefix for the same reason that list is one: the account id
 * sits in the middle of the path. POST only, and that is the load-bearing half — `DELETE` on the
 * same path is `api/admin.py`'s `unlink_jellyfin`, which makes no foreign call at all, so its
 * only 401 is `AdminUser` reaching `api/deps.py`'s `current_user`. That is a real session loss,
 * and exempting it would swallow the one thing this seam exists for.
 * [§3.3, §7.1; decision 282; review cycle 3: M415-C3-API-01]
 */
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

/**
 * What the server said, in the order the wire formats are distinguishable (finding 17).
 *
 * FastAPI's `HTTPException` puts a string in `detail`; this app's importer puts an object with a
 * `text`; and pydantic's validation failure puts a **list** of `{type, loc, msg, ctx}`, which
 * matched neither case — so every refused field reached the household as the HTTP reason phrase,
 * or as the empty string. Two of those are reachable from ordinary controls with no client-side
 * guard: Tonight's guests box (Svelte's binding puts `null` there when it is emptied, and
 * `api/tonight.py` bounds it) and the account PIN box against `api/auth.py`'s digits-only
 * pattern.
 *
 * The last `loc` segment is the field name WHERE THERE IS ONE — `["body", "guests"]` — and
 * pydantic's `msg` is already a sentence, so "guests: Input should be a valid integer" costs no
 * translation table: one place knows the wire format, and a per-route map of pydantic error
 * types would be a second place that has to be kept in step with the models.
 *
 * "Where there is one" is the correction, and it names two shapes rather than hedging. `loc`
 * opens with the SOURCE, so a body that is missing or is not an object at all answers `["body"]`
 * and a bad element inside a list-valued query answers `["query", "kind", 0]`. Read as a field
 * name, those label the household's copy "body: Field required" and "0: Input should be a valid
 * integer" — a label naming the wire rather than anything on the screen, which is the same
 * category of sentence as "Unprocessable Entity" and "not signed in" that this branch and the
 * seam below exist to stop a household reading (§6.8). Neither is reachable from this client's
 * callers today: its bodyless POSTs all reach handlers that declare no body parameter, and every
 * list-valued parameter it sends is built from literals or filtered before `qs()` sees it. So
 * this is hardening plus a corrected sentence, not the repair of a message anybody has met — and
 * a comment that states a rule the code does not hold is the defect either way.
 * [§6.8; review cycle 3: M415-C3-API-06]
 *
 * `message` beside `text` in the last branch, because `text` is the importer's key and `message`
 * is this backend's: eleven of the twelve object-shaped `detail`s it raises are
 * `{reason, message}` (`api/rank.py`, `api/rate.py`, `api/tonight.py`), and one is the importer's
 * `{report, text}`. The module knew the rare shape and not the common one, so every one of those
 * eleven fell through to `res.statusText` — which §2's deployment makes the empty string, since
 * the household reaches this app over Traefik and Cloudflare and HTTP/2 carries no reason phrase.
 * Three stores each re-implement this read today (`rate.svelte.js`, `rank.svelte.js`,
 * `tonight.svelte.js`), which is the duplication "one place knows the wire format" exists to end;
 * they are M4.9's, M4.10's and M4.12's to collapse, and this is the half that is this module's.
 */
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
 * The shell's one reaction to losing the session (finding 14, fe-04).
 *
 * Before this there was no interceptor at all: each store mapped its own 401 into its own red
 * banner, so a rotated `SESSION_SECRET`, a restored backup or the hourly prune showed
 * `api/deps.py`'s "not signed in" on Rate, then on Rank, then on Home — with the person's name
 * still in the chip and Log out the only way forward. The sharpest evidence that nothing was
 * listening: `needsAdminReauth` and `needsPasswordChange` had no production caller at all, so an
 * admin resetting a member's password left that member's live tab printing §3.1's sentence on
 * every surface with no link to `/account/password`.
 *
 * One handler, registered once by the shell, and `goto` stays on the shell's side of the seam so
 * this module keeps no DOM and no router. The `ApiError` is thrown afterwards either way, so no
 * caller changes.
 *
 * @type {((reason: 'signed-out' | 'password-change') => void) | null}
 */
let sessionLost = null;

/**
 * Register the shell's handler; pass null to clear it. One registration, because two shells do
 * not exist and a second would mean a surface had quietly taken over the decision.
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
  // The deadline belongs to the route: `opts.timeoutMs` is the escape hatch for a caller that
  // knows better, and the long jobs and the connector hops each carry their own default
  // (decision 269, amended).
  const deadline = opts.timeoutMs ?? routeDeadline(path);
  // Hoisted out of the `fetch` call below because the seam reads it too: one route's 401 means
  // something different on POST than on DELETE (`FOREIGN_CREDENTIAL`).
  const method = opts.method ?? 'GET';
  const clock = new AbortController();
  let expired = false;
  const timer = setTimeout(() => {
    expired = true;
    clock.abort();
  }, deadline);
  // Composed with the caller's signal rather than replacing it: a surface that cancels its own
  // in-flight read must still be able to, and that abort stays its own AbortError instead of
  // becoming a sentence about the network, which is not what happened.
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
      // Only a body this module could PARSE is a sentence this app wrote. §2 puts Traefik and
      // Cloudflare in front of the appliance, and an edge answers a 502 with its own HTML page —
      // which the string branch of `wireMessage` handed through whole, so a gateway's markup was
      // rendered as the reason under the household's own name. An unparseable body is a fact
      // about the wire, not copy. [§6.8; review cycle 2: M415-C2-API-02]
      const wire = payload && typeof payload === 'object' ? payload.detail : payload;
      const detail = parsed ? wire : null;
      const message = wireMessage(detail, res.statusText);
      const reauth = res.headers.get('x-spielplan-reauth') === 'admin';
      // sec-05: `needsAdminReauth` was parsed here and consumed nowhere, so a tab left open past
      // §3.2's 24 hours showed raw 401s from every admin fetch while `/auth/me` — read once at
      // boot — still said the clock was clear. The header is the only place the server says so
      // outside that one route, and the shell's banner reads this flag.
      if (reauth && session.user) session.user.admin_reauth_required = true;
      const err = new ApiError(res.status, message, detail, reauth);
      // The seam, and the first production reading of these two getters. The re-prompt is
      // excluded by `!reauth` because §3.2's admin is still signed in — navigating would throw
      // away a live session to ask for the same password — and the credential routes are
      // excluded because a 401 there is the server answering the question that was asked.
      //
      // `session.user` is the third exclusion and the one no route list can express: a 401 can
      // only END a session the shell believed it HELD. On a first boot every authenticated route
      // answers 401 honestly — §3.1's "the app boots with /data/artifacts and artifact_bundle
      // empty, serving the setup wizard" is a box on which no session and no account have ever
      // existed — so the four reads Home fires as it mounts each arrived here as a sign-out, and
      // the household's first admin was carried past the wizard to a sign-in form for an account
      // nobody can have been given. `guard()` cannot undo it either: /login is one of the shell's
      // PUBLIC destinations, so once the seam has landed there nothing routes back to §3.1's
      // wizard. Finding 14's own cases are untouched, because each of them strikes a tab whose
      // `/auth/me` has already answered — a rotated SESSION_SECRET, a restored backup and the
      // hourly prune all leave the name in the chip, and that name IS a `session.user` standing
      // here. [decision 282; §3.1, §3.2]
      //
      // And `FOREIGN_CREDENTIAL` is the fourth, for a 401 no route list of THIS app's doors can
      // express: the per-member Jellyfin link answers with the media server's refusal of a
      // password that is not this app's at all (§3.3). [review cycle 3: M415-C3-API-01]
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
    // Only this module's own deadline becomes a sentence; status 0 because no status came back,
    // which also keeps `isUnauthenticated` false — a phone that lost the network has not been
    // signed out, and the seam above must not fire for it.
    if (expired) throw new ApiError(0, NO_ANSWER);
    throw err;
  } finally {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', relay);
  }
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
