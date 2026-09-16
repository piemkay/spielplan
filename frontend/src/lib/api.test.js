import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

import { ApiError, api, onUnauthenticated, qs } from './api.js';
import { session } from './session.svelte.js';

describe('qs', () => {
  it('drops empty values so the URL stays readable', () => {
    expect(qs({ a: 1, b: null, c: undefined, d: '', e: false, f: 'x' })).toBe('?a=1&f=x');
  });

  it('returns an empty string when nothing survives', () => {
    expect(qs({ a: null, b: '' })).toBe('');
  });

  it('expands an array into repeated parameters', () => {
    // FastAPI's `list[...]` expects `?kind=movie&kind=series`; a comma-joined value would
    // arrive as one nonsense literal.
    expect(qs({ kind: ['movie', 'series'] })).toBe('?kind=movie&kind=series');
  });

  it('keeps a single-element array repeated rather than scalar', () => {
    expect(qs({ kind: ['movie'] })).toBe('?kind=movie');
  });

  it('omits an empty array entirely, which the API then rejects', () => {
    // Deliberate: an empty selection must be unrepresentable in the URL, so it surfaces as a
    // 422 rather than a silent "everything" (§4.1 rule 5).
    expect(qs({ kind: [] })).toBe('');
  });

  it('encodes values that need it', () => {
    expect(qs({ q: 'a b&c' })).toBe('?q=a+b%26c');
  });

  it('keeps a zero, which is a real value', () => {
    expect(qs({ offset: 0 })).toBe('?offset=0');
  });
});

describe('api', () => {
  // finding 26 (fes-06): the stub used to be installed anonymously - `vi.stubGlobal('fetch',
  // vi.fn())` - so every `fetch.mockReturnValue` below read a mock method off the DOM's `fetch`
  // type, which does not have one. That was 14 of the 28 errors `npm run check` reported on a
  // clean tree, and a checker that always fails is a checker nobody reads. The same stub held
  // in a local of its own carries the type the checker needs; the test is otherwise unchanged.
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    // The seam is one module-level registration, so a handler left behind by one test would
    // fire inside the next one.
    onUnauthenticated(null);
    // And `session` is a module-level singleton for the same reason it is one in the app. The
    // seam reads `session.user` now (decision 282), so a person left signed in by one case
    // decides the branch the next one takes.
    session.user = null;
  });

  // The precondition the seam keys on, and the precondition every case in finding 14 has: a tab
  // whose `/auth/me` has already answered, which is what puts the name in the chip. [decision 282]
  const SIGNED_IN = { id: 7, name: 'Ada', role: 'member', must_change_password: false };

  const respond = (status, body, ok = status < 400, headers = {}) =>
    Promise.resolve({
      ok,
      status,
      statusText: 'x',
      // A real Response always has headers, and the client reads one of them (§3.2's admin
      // re-prompt travels in `X-Spielplan-Reauth`). A double without them fails everywhere.
      headers: new Headers(headers),
      text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
    });

  // The failure finding 16 (fe-lc-01) names is a socket that is open and silent, not one that
  // refuses: the phone that leaves the house mid-request. It honours the abort because a real
  // fetch does, which is the only way a client-side deadline ends anything at all.
  const never = (_url, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () =>
        reject(new DOMException('The operation was aborted.', 'AbortError'))
      );
    });

  const NO_ANSWER = 'the network did not answer — try again';

  it('sends the session cookie', async () => {
    fetchMock.mockReturnValue(respond(200, { ok: true }));
    await api('/health');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/health',
      expect.objectContaining({ credentials: 'include' })
    );
  });

  it('serialises a body and sets the content type', async () => {
    fetchMock.mockReturnValue(respond(200, {}));
    await api('/x', { method: 'POST', body: { a: 1 } });
    const [, opts] = fetchMock.mock.calls[0];
    expect(opts.body).toBe('{"a":1}');
    expect(opts.headers['content-type']).toBe('application/json');
  });

  it('returns null on 204 without trying to parse it', async () => {
    fetchMock.mockReturnValue(Promise.resolve({ ok: true, status: 204 }));
    await expect(api('/x')).resolves.toBeNull();
  });

  it('raises ApiError with the server detail', async () => {
    fetchMock.mockReturnValue(respond(422, { detail: 'select at least one kind' }, false));
    await expect(api('/titles')).rejects.toMatchObject({
      status: 422,
      message: 'select at least one kind',
    });
  });

  it('names the field and the reason when pydantic refuses one', async () => {
    // finding 17 (feroutes-422-renders-as-http-reason-phrase): FastAPI's 422 detail is a LIST of
    // {type, loc, msg, ctx} and matched neither case the client had, so Tonight's empty guests
    // field - Svelte's binding puts `null` there - reached the household as the HTTP reason
    // phrase. §6.8's register is a stated reason; "Unprocessable Entity" is not one.
    fetchMock.mockReturnValue(
      respond(
        422,
        {
          detail: [
            {
              type: 'int_parsing',
              loc: ['body', 'guests'],
              msg: 'Input should be a valid integer',
              ctx: {},
            },
          ],
        },
        false
      )
    );
    const err = await api('/tonight/round', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe('guests: Input should be a valid integer');
  });

  it('joins several refused fields into one sentence', async () => {
    fetchMock.mockReturnValue(
      respond(
        422,
        {
          detail: [
            { loc: ['body', 'pin'], msg: 'String should match pattern' },
            { loc: ['body', 'guests'], msg: 'Input should be a valid integer' },
          ],
        },
        false
      )
    );
    const err = await api('/auth/pin', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe(
      'pin: String should match pattern; guests: Input should be a valid integer'
    );
  });

  it('says the reason without a label when the whole body is what was refused', async () => {
    // `loc` opens with the SOURCE, so a missing or non-object body answers `["body"]` and there
    // is no field in it at all. Labelled from the last segment, that read "body: Field required"
    // - a label naming the wire rather than anything on the screen, which is the category of
    // sentence §6.8 spends this branch avoiding. [review cycle 3: M415-C3-API-06]
    fetchMock.mockReturnValue(
      respond(422, { detail: [{ type: 'missing', loc: ['body'], msg: 'Field required' }] }, false)
    );
    const err = await api('/tonight/round', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe('Field required');
  });

  it('does not label a refused list element with its position', async () => {
    // `kind: list[Literal[...]]` refuses element 0 with `loc: ["query", "kind", 0]`, and an
    // index is not a field name: "0: Input should be 'movie' or 'series'" names a position in a
    // query string the household never typed. [review cycle 3: M415-C3-API-06]
    fetchMock.mockReturnValue(
      respond(
        422,
        {
          detail: [
            {
              type: 'literal_error',
              loc: ['query', 'kind', 0],
              msg: "Input should be 'movie' or 'series'",
            },
          ],
        },
        false
      )
    );
    const err = await api('/titles?kind=nonsense').catch((e) => e);
    expect(err.message).toBe("Input should be 'movie' or 'series'");
  });

  it("states this backend's own refusal envelope rather than the HTTP reason phrase", async () => {
    // `{reason, message}` is what eleven of the twelve object-shaped `detail`s this backend
    // raises look like (api/rank.py, api/rate.py, api/tonight.py); `{report, text}` is the
    // importer's one. The module knew the rare shape and not the common one, so all eleven fell
    // through to `res.statusText` - which over §2's Traefik/Cloudflare ingress is HTTP/2, and
    // h2 carries no reason phrase at all.
    fetchMock.mockReturnValue(
      respond(409, { detail: { reason: 'round_closed', message: 'the round is already closed' } }, false)
    );
    const err = await api('/tonight/sessions/1/answer', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.message).toBe('the round is already closed');
    // The importer's key still wins where both could be read, because the Data tab renders the
    // report beside it and `text` is the sentence written for that card.
    const both = { text: 'row 4 is malformed', message: 'x' };
    fetchMock.mockReturnValue(respond(422, { detail: both }, false));
    const importErr = await api('/admin/bundle/import', { method: 'POST', body: {} }).catch((e) => e);
    expect(importErr.message).toBe('row 4 is malformed');
  });

  it('ends a request that never answers with a sentence, not a parked surface', async () => {
    // finding 16 (fe-lc-01): every surface's single-flight guard is a boolean cleared in a
    // `finally`, so a request that never settles parks `busy` at true for ever - every later
    // tap returns early, nothing is sent, and `rate.error` stays ''. The deadline is what makes
    // that `finally` run, and the sentence is what the existing onError path then renders.
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    const pending = api('/rate/next').catch((e) => e);
    await vi.advanceTimersByTimeAsync(10_000);
    const err = await pending;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe(NO_ANSWER);
    expect(err.status).toBe(0);
  });

  it('gives the three long routes minutes rather than the ordinary ten seconds', async () => {
    // Decision 269: an import, a validate and a Jellyfin sync legitimately run for minutes, and
    // the deadline that covers them is declared in api.js rather than at three call sites - a
    // deadline the wire enforces cannot be lost by a rewrite of the caller.
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    let settled = false;
    const pending = api('/admin/bundle/import', { method: 'POST', body: {} }).catch((e) => e);
    pending.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(600_000);
    expect((await pending).message).toBe(NO_ANSWER);
  });

  it('gives a route that reaches Jellyfin the budget the media server itself has', async () => {
    // Decision 269, amended. `connectors/jellyfin.py` gives its httpx client 15 s and
    // `registry.make_client` never overrides it, so a silently dropped connect - a firewalled LAN
    // address, a box asleep - burns fifteen seconds before the SERVER can answer, and `check()`
    // spends that budget twice. At ten the client gave up first: §6.6's Test button, whose whole
    // purpose is to report an unreachable media server, answers HTTP 200 with
    // `{ok: false, error}`, so aborting it does not delay the diagnosis - it destroys it.
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    let settled = false;
    const pending = api('/admin/connectors/jellyfin/test', { method: 'POST' }).catch((e) => e);
    pending.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(settled, 'ten seconds is shorter than the floor this route inherits').toBe(false);
    await vi.advanceTimersByTimeAsync(15_000);
    expect((await pending).message).toBe(NO_ANSWER);
  });

  it('gives every route of that class the same deadline, and leaves the other two alone', async () => {
    // The save writes the credentials BEFORE it probes (`api/admin.py`'s `put_jellyfin`), so an
    // abort at ten seconds said "the network did not answer" about a write that had landed; the
    // per-member link carries an account id in the MIDDLE of its path, which is why the match is
    // a pattern rather than an `under()` prefix. The sync stays long because a sync over a whole
    // library is the job and not the hop, and an ordinary surface read keeps finding 16's ten.
    vi.useFakeTimers();
    /** @type {[string, number][]} */
    const budgets = [
      ['/admin/connectors/jellyfin', 45_000],
      ['/admin/connectors/jellyfin/users', 45_000],
      ['/admin/users/7/jellyfin', 45_000],
      ['/admin/connectors/jellyfin/sync', 600_000],
      ['/rate/next', 10_000]
    ];
    for (const [path, deadline] of budgets) {
      fetchMock.mockImplementation(never);
      let done = false;
      const pending = api(path).catch((e) => e);
      pending.then(() => {
        done = true;
      });
      await vi.advanceTimersByTimeAsync(deadline - 1);
      expect(done, `${path} gave up before its own deadline`).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      expect((await pending).message, path).toBe(NO_ANSWER);
    }
  });

  it('lets a caller set the deadline for itself', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(never);
    const pending = api('/rate/next', { timeoutMs: 50 }).catch((e) => e);
    await vi.advanceTimersByTimeAsync(50);
    expect((await pending).message).toBe(NO_ANSWER);
  });

  // §2's deployment is what makes this the interesting double: the household reaches this app
  // over Traefik and Cloudflare, and HTTP/2 carries no reason phrase, so `res.statusText` is the
  // empty string on the wire this app actually ships on. Every other case in this file sets it to
  // 'x', which is a value the deployment cannot produce and which hid the fall-through.
  const overH2 = (status, body) =>
    Promise.resolve({
      ok: false,
      status,
      statusText: '',
      headers: new Headers(),
      text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body))
    });

  it('states a sentence when the wire says nothing at all', async () => {
    // Every surface gates its error line on truthiness, so an empty message is not a red line
    // with nothing in it - it is no line at all, which is the silence NO_ANSWER was minted to
    // end, reached by a second path (§6.8).
    fetchMock.mockReturnValue(overH2(502, ''));
    const err = await api('/rate/next').catch((e) => e);
    expect(err.message).toBe('the appliance refused this and did not say why');
  });

  it("does not render an edge's own error page as the app's reason", async () => {
    // Traefik and Cloudflare answer a 502 with their own HTML. Handed through as `detail`, it was
    // returned whole by the string branch below - a gateway's markup rendered as the reason,
    // under the household's name.
    fetchMock.mockReturnValue(overH2(502, '<html><head><title>502 Bad Gateway</title></head></html>'));
    const err = await api('/rate/next').catch((e) => e);
    expect(err.message).not.toContain('<html');
    expect(err.message).toBe('the appliance refused this and did not say why');
    expect(err.detail, 'an unparseable body is a fact about the wire, not this app speaking').toBeNull();
  });

  it('says something rather than nothing for a shape it does not recognise', async () => {
    // A list of bare strings matches neither the pydantic branch (no `msg`) nor the object one.
    fetchMock.mockReturnValue(overH2(400, { detail: ['nope', 'also nope'] }));
    expect((await api('/x').catch((e) => e)).message).toBe(
      'the appliance refused this and did not say why'
    );
  });

  it('still prefers anything the appliance did state', async () => {
    fetchMock.mockReturnValue(overH2(409, { detail: { reason: 'closed', message: 'the round is closed' } }));
    expect((await api('/rank/x').catch((e) => e)).message).toBe('the round is closed');
  });

  it("composes with the caller's own signal rather than replacing it", async () => {
    // The deadline must not take the `signal` option away from a caller that cancels its own
    // in-flight read: an abort the caller asked for stays the caller's AbortError, because it
    // is not a network failure and there is nothing to say to the household about it.
    fetchMock.mockImplementation(never);
    const caller = new AbortController();
    const pending = api('/rate/next', { signal: caller.signal }).catch((e) => e);
    caller.abort();
    const err = await pending;
    expect(err).not.toBeInstanceOf(ApiError);
    expect(err.name).toBe('AbortError');
  });

  it("tells §3.2's admin re-prompt apart from an ordinary sign-out", async () => {
    // Both are 401. Only the header says "sign in again" rather than "you are signed out",
    // and the shell renders a different thing for each.
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    const reauth = await api('/admin/users').catch((e) => e);
    expect(reauth.isUnauthenticated).toBe(true);
    expect(reauth.needsAdminReauth).toBe(true);

    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    const plain = await api('/auth/me').catch((e) => e);
    expect(plain.isUnauthenticated).toBe(true);
    expect(plain.needsAdminReauth).toBe(false);
  });

  it("raises §3.2's re-prompt flag on the user, so a long-open tab shows the banner", async () => {
    // sec-05: `needsAdminReauth` was parsed and consumed nowhere. `/auth/me` is read once at
    // boot, so a tab left open past the 24 hours kept a `session.user` saying the clock was
    // clear while every admin fetch came back 401 — the shell showed raw errors and no way out.
    session.user = {
      id: 1,
      name: 'admin',
      role: 'admin',
      must_change_password: false,
      admin_reauth_required: false
    };
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    await api('/admin/users').catch(() => {});
    expect(session.user.admin_reauth_required).toBe(true);

    // An ordinary 401 must not raise it: that is a sign-out, and the banner would offer a
    // password box for a session that no longer exists.
    session.user = {
      id: 1,
      name: 'admin',
      role: 'admin',
      must_change_password: false,
      admin_reauth_required: false
    };
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await api('/auth/me').catch(() => {});
    expect(session.user.admin_reauth_required).toBe(false);

    // And it must survive nobody being signed in at all, which is every anonymous fetch the
    // login page makes.
    session.user = null;
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    await expect(api('/admin/users')).rejects.toBeInstanceOf(ApiError);
  });

  it('flags an unauthenticated error so the shell can redirect', async () => {
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    const err = await api('/auth/me').catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.isUnauthenticated).toBe(true);
  });

  it('recognises the forced password change, which is a 403 the UI must not treat as denial', async () => {
    fetchMock.mockReturnValue(
      respond(403, { detail: 'password change required before this account can be used' }, false)
    );
    const err = await api('/titles').catch((e) => e);
    expect(err.needsPasswordChange).toBe(true);
  });

  it('survives a non-JSON error body', async () => {
    fetchMock.mockReturnValue(respond(500, '<html>gateway</html>', false));
    const err = await api('/x').catch((e) => e);
    expect(err.status).toBe(500);
    expect(err).toBeInstanceOf(ApiError);
  });

  it('carries the structured detail an import failure returns', async () => {
    // The bundle importer answers 422 with {report, text}; the Data tab renders the report,
    // so the client must not flatten it to a string.
    fetchMock.mockReturnValue(respond(422, { detail: { report: { ok: false }, text: 'x' } }, false));
    const err = await api('/admin/bundle/import', { method: 'POST', body: {} }).catch((e) => e);
    expect(err.detail.report.ok).toBe(false);
  });

  it('calls the session-loss handler once for a 401 on a surface route', async () => {
    // finding 14 (fe-04): nothing above api.js reacted to a 401, so a rotated SESSION_SECRET,
    // a restored backup or the hourly prune printed deps.py's "not signed in" on Rate, then
    // Rank, then Home, with the person's name still in the chip and Log out the only way out.
    // The seam is here because this is the one place that knows the wire; `goto` stays in the
    // shell so this module stays DOM-free.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(respond(401, { detail: 'session expired' }, false));
    await expect(api('/rate/next')).rejects.toBeInstanceOf(ApiError);
    expect(seen).toEqual(['signed-out']);
  });

  it('leaves the credential routes alone, where a 401 is the answer and not a sign-out', async () => {
    // THE PRECONDITION IS THE ASSERTION. `session.user` is the third conjunct at the seam and
    // `&&` short-circuits, so a case that runs with nobody signed in asserts decision 282 a
    // second time and this list zero times: `seen` would be empty however the exclusions were
    // edited. Mutation says so - with this line absent, deleting `!under(path,
    // CREDENTIAL_ROUTES)` from api.js left all 39 cases in this file green, and /auth/password,
    // /auth/switch and /auth/logout are covered at no other layer (19-phone-shell drives only
    // /auth/pin and the reauth header, and every other e2e hit on these routes is a
    // `request.post` that never enters this module). [review cycle 3: M415-C3-API-02]
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    for (const path of [
      '/auth/login',
      '/auth/switch',
      '/auth/logout',
      '/auth/me',
      '/auth/passkey/login',
      '/auth/passkey/login/options',
      '/setup/state',
      '/setup/admin'
    ]) {
      fetchMock.mockReturnValue(respond(401, { detail: 'wrong password' }, false));
      await api(path, { method: 'POST', body: {} }).catch(() => {});
    }
    expect(seen).toEqual([]);
  });

  it('signs a member out when one of the setup routes that CAN 401 does', async () => {
    // The prefix `/setup` exempted all four of api/setup.py's mounts, and read against that
    // router it was inverted: the two it meant - `/state` (OptionalUser, which swallows the
    // refusal) and `/admin` (no user dependency) - cannot answer 401 at all, while the two it
    // also swallowed are the only ones that can. `/setup/onboarding/complete` is the reachable
    // one: `push.js`'s completeOnboarding() is the onboarding card's "Not now" button, rendered
    // on an ordinary /account visit, so a member whose session the hourly prune took read
    // api/deps.py's "not signed in" under their own name - finding 14's exact failure, on the
    // one route class the exemption exists to exclude.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    for (const path of ['/setup/onboarding/complete', '/setup/connectors']) {
      fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
      await api(path, { method: 'POST', body: {} }).catch(() => {});
    }
    expect(seen).toEqual(['signed-out', 'signed-out']);
  });

  it('leaves the three routes that answer 401 for a mistyped current password alone', async () => {
    // auth.py's `change_password`, `set_pin` and `reauth` each verify the account password and
    // raise 401 "wrong current password". They are authenticated routes, so the plan's list read
    // off the anonymous doors does not name them - and without them a typo in §3.2's 24-hour
    // re-prompt or in the account PIN box would sign the member out of a session that is
    // perfectly alive. A 401 on a credential is the answer to the credential.
    //
    // By name rather than by the coordinates this comment shipped with, for the reason api.js
    // now gives beside the list itself: `:306`, `:380` and `:416` were true at HEAD and were
    // falsified by this milestone's own edit to that router. And `session.user`, for the reason
    // the case above gives: without it the seam short-circuits and this case asserts nothing.
    // [review cycle 3: M415-C3-API-02, M415-C3-API-05]
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    for (const path of ['/auth/password', '/auth/pin', '/auth/reauth']) {
      fetchMock.mockReturnValue(respond(401, { detail: 'wrong current password' }, false));
      await api(path, { method: 'POST', body: {} }).catch(() => {});
    }
    expect(seen).toEqual([]);
  });

  it('leaves the per-member Jellyfin link alone, because that 401 is another server refusing', async () => {
    // §3.3: "authentication is **never** delegated to Jellyfin", so a refusal from the media
    // server can never be a statement about this app's session. `api/admin.py`'s `link_jellyfin`
    // raises exactly that - 401 "Jellyfin refused that sign-in" - when an admin mistypes the
    // Jellyfin password in one of the two boxes the Users and Connectors pages put on a member's
    // Link row. Unexempted it ran `clearUser(); resetRank(); goto('/login')` over a session that
    // was perfectly alive: this module already knew the route, having read it once for its 45 s
    // deadline and not for its status. [review cycle 3: M415-C3-API-01]
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(
      respond(401, { detail: 'Jellyfin refused that sign-in: 401 Unauthorized' }, false)
    );
    await api('/admin/users/7/jellyfin', { method: 'POST', body: {} }).catch(() => {});
    expect(seen).toEqual([]);
  });

  it('still signs the admin out when the DELETE on that same path is refused', async () => {
    // The other half of the exemption, and the reason it is keyed on the METHOD. `unlink_jellyfin`
    // makes no foreign call at all, so its only 401 is `AdminUser` reaching `api/deps.py`'s
    // `current_user` - a real session loss, on the one path where the exemption would otherwise
    // swallow it. [review cycle 3: M415-C3-API-01]
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await api('/admin/users/7/jellyfin', { method: 'DELETE' }).catch(() => {});
    expect(seen).toEqual(['signed-out']);
  });

  it('signs nobody out of a session that never existed, which is what a first boot is', async () => {
    // The browser gate's first failure, and the reason the rule is not a route list. §3.1 boots a
    // bundle-less box into the setup wizard, and on that box EVERY authenticated route answers
    // 401 honestly: no session and no account have ever existed. The four reads Home fires as it
    // mounts - the finish prompt, the grid, the facets and the shelves - each reached the seam as
    // a sign-out, so the household's first admin was carried past the wizard to a sign-in form
    // for an account nobody can have been given, and `guard()` could not route back because
    // /login is one of the shell's PUBLIC destinations. A 401 can only end a session the shell
    // believed it held. [decision 282]
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = null;
    for (const path of [
      '/prompts/finish',
      '/titles?kind=movie&limit=60&offset=0',
      '/facets?kind=movie'
    ]) {
      fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
      await api(path).catch(() => {});
    }
    // The surface still gets its error: the seam decides what the SHELL does, and no caller
    // changes shape because of who is or is not signed in.
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await expect(api('/home?kind=movie')).rejects.toBeInstanceOf(ApiError);
    expect(seen).toEqual([]);
  });

  it("does not sign the admin out of §3.2's 24-hour re-prompt", async () => {
    // The re-prompt is a 401 carrying `X-Spielplan-Reauth: admin` (`api/deps.py`'s `admin_user`)
    // and the admin is still signed in: the shell's banner is the only signal, and a redirect to
    // /login would throw away a live session to ask for the same password.
    //
    // "Still signed in" is the state this case has to BE in, and it was not: without a
    // `session.user` the seam short-circuits before `!reauth` is read, so deleting that conjunct
    // from api.js left this case green. A fresh object rather than the shared `SIGNED_IN`,
    // because the seam WRITES through it - `session.user.admin_reauth_required = true` - and the
    // shared const would carry that flag into every case below.
    // [review cycle 3: M415-C3-API-02]
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = { ...SIGNED_IN };
    fetchMock.mockReturnValue(
      respond(401, { detail: 'admin re-authentication required' }, false, {
        'x-spielplan-reauth': 'admin'
      })
    );
    await api('/admin/users').catch(() => {});
    expect(seen).toEqual([]);
  });

  it('treats a 401 on passkey registration as the session loss it is', async () => {
    // Registration runs inside a session; only the login half is anonymous.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    session.user = SIGNED_IN;
    fetchMock.mockReturnValue(respond(401, { detail: 'not signed in' }, false));
    await api('/auth/passkey/register/options', { method: 'POST', body: {} }).catch(() => {});
    expect(seen).toEqual(['signed-out']);
  });

  it('routes §3.1\'s forced password change to the handler rather than a denial banner', async () => {
    // `needsPasswordChange` had no production caller at all (finding 14's sharpest evidence), so
    // an admin resetting a member's password left that member's live tab printing deps.py:173's
    // sentence on every surface with no link to /account/password.
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    fetchMock.mockReturnValue(
      respond(403, { detail: 'password change required before this account can be used' }, false)
    );
    const err = await api('/titles').catch((e) => e);
    expect(seen).toEqual(['password-change']);
    expect(err.needsPasswordChange).toBe(true);
  });

  it('leaves an ordinary 403 to the surface that asked', async () => {
    const seen = [];
    onUnauthenticated((reason) => seen.push(reason));
    fetchMock.mockReturnValue(respond(403, { detail: 'admin role required' }, false));
    await api('/admin/users').catch(() => {});
    expect(seen).toEqual([]);
  });
});
